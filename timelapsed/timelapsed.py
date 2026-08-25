import logging
import multiprocessing
import random
import signal
import time
from datetime import datetime, timedelta, timezone

from rich.logging import RichHandler

from timelapsed.config import get_config, validate_config
from timelapsed.image_capture_library import ImageCaptureLibrary
from timelapsed.image_processor import generate_timelapse
from timelapsed.nvr_capture_agent import NVRCaptureAgent
from timelapsed.schema import Cadence, Config

logger = logging.getLogger(__name__)

MAX_CHANNEL_STARTUP_JITTER_SECONDS = 1.4
PRUNE_INTERVAL = timedelta(hours=1)


def apply_logging_config(config: Config) -> None:
    logging.basicConfig(
        level=config.logging_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
        force=True,
    )


def _render_timelapse_entrypoint(
    config: Config,
    library: ImageCaptureLibrary,
    channel_id: str,
    cadence_name: str,
    start_time: datetime,
    end_time: datetime,
) -> None:
    """Process entrypoint for a single render. Runs outside the capture loop."""
    apply_logging_config(config)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        generate_timelapse(
            library,
            channel_id,
            cadence_name,
            start_time,
            end_time,
            config.timelapse_video_duration,
            output_fps=config.timelapse_output_fps,
            min_frames=config.timelapse_min_frames,
        )
    except Exception:
        logger.exception("%s timelapse render failed for channel %s", cadence_name, channel_id)


class RenderScheduler:
    """Runs timelapse renders in background processes, one at a time per cadence.

    Rendering inline would stall capture for as long as ffmpeg runs, so each
    render gets its own process. If a render is still going when the next one is
    due, the new one is skipped rather than queued -- falling behind is better
    than piling up ffmpeg processes on a small VM.
    """

    def __init__(self, config: Config, library: ImageCaptureLibrary, channel_id: str):
        self.config = config
        self.library = library
        self.channel_id = channel_id
        self._processes: dict[str, multiprocessing.Process] = {}

    def submit(self, cadence: Cadence, start_time: datetime, end_time: datetime) -> bool:
        running = self._processes.get(cadence.name)
        if running is not None and running.is_alive():
            logger.warning(
                "Previous %s render for channel %s is still running; skipping this one",
                cadence.name, self.channel_id,
            )
            return False

        process = multiprocessing.Process(
            target=_render_timelapse_entrypoint,
            args=(self.config, self.library, self.channel_id, cadence.name, start_time, end_time),
            name=f"render-{cadence.name}-{self.channel_id}",
            daemon=False,
        )
        process.start()
        self._processes[cadence.name] = process
        logger.info(
            "Started %s timelapse render for channel %s (pid %s)",
            cadence.name, self.channel_id, process.pid,
        )
        return True

    def shutdown(self, timeout: float = 30.0) -> None:
        for cadence, process in self._processes.items():
            if process.is_alive():
                logger.info("Waiting for %s render on channel %s to finish", cadence, self.channel_id)
                process.join(timeout=timeout)
                if process.is_alive():
                    logger.warning("Killing unfinished %s render on channel %s", cadence, self.channel_id)
                    process.terminate()


def capture_continuously(channel_id: str, capture_agent: NVRCaptureAgent, library: ImageCaptureLibrary, config: Config) -> None:
    """Capture one channel forever: snapshot, store, render on rollover, prune, sleep."""
    apply_logging_config(config)

    shutting_down = False

    def request_shutdown(signum, _frame):
        nonlocal shutting_down
        logger.info("Channel %s received signal %d; stopping after this cycle", channel_id, signum)
        shutting_down = True

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    scheduler = RenderScheduler(config, library, channel_id)

    # Seeded with the current time so a restart does not immediately re-render
    # every cadence; each one fires on its next genuine rollover.
    last_run_at: dict[str, datetime] = {
        cadence.name: datetime.now(tz=timezone.utc) for cadence in config.timelapse_cadences
    }
    last_pruned_at: datetime | None = None

    while not shutting_down:
        now = datetime.now(tz=timezone.utc)

        try:
            image_data, extension = capture_agent.capture_image(channel_id, config.capture_resolution)
            library.store_image(channel_id, extension, image_data, now)
            logger.debug("Stored image for channel %s", channel_id)
        except Exception:
            logger.exception("Capture cycle failed for channel %s, continuing", channel_id)

        try:
            for cadence in config.timelapse_cadences:
                if cadence.is_due(now, last_run_at[cadence.name]):
                    last_run_at[cadence.name] = now
                    scheduler.submit(cadence, now - cadence.window, now)

            # Prune hourly: often enough to keep disk flat, cheap enough to ignore.
            if last_pruned_at is None or (now - last_pruned_at) >= PRUNE_INTERVAL:
                last_pruned_at = now
                library.prune(channel_id, "image", config.image_retention, now)
                library.prune(channel_id, "timelapse", config.timelapse_retention, now)
        except Exception:
            logger.exception("Timelapse scheduling failed for channel %s, continuing", channel_id)

        cycle_duration = datetime.now(tz=timezone.utc) - now
        if cycle_duration.total_seconds() > config.capture_interval.total_seconds() * 0.8:
            logger.warning(
                "Capture cycle for channel %s took >80%% of the capture interval (%.2fs > %.2fs)",
                channel_id, cycle_duration.total_seconds(), config.capture_interval.total_seconds(),
            )

        sleep_for = config.capture_interval.total_seconds() - cycle_duration.total_seconds()
        if sleep_for > 0 and not shutting_down:
            time.sleep(sleep_for)

    scheduler.shutdown()
    logger.info("Channel %s worker stopped", channel_id)


def run() -> None:
    config = get_config()
    apply_logging_config(config)

    for warning in validate_config(config):
        logger.warning("Configuration problem: %s", warning)

    library = ImageCaptureLibrary(config.image_capture_library_root)
    capture_agent = NVRCaptureAgent(config.nvr_url, config.nvr_username, config.nvr_password)

    logger.info(
        "Timelapsed starting: %d channel(s) [%s], cadences [%s], every %.0fs",
        len(config.channels),
        ", ".join(config.channels),
        ", ".join(cadence.name for cadence in config.timelapse_cadences),
        config.capture_interval.total_seconds(),
    )

    # One process per channel, not a fixed-size pool: these workers never return,
    # so a pool smaller than the channel count would silently never start the rest.
    workers: list[multiprocessing.Process] = []
    for channel_id in config.channels:
        worker = multiprocessing.Process(
            target=capture_continuously,
            args=(channel_id, capture_agent, library, config),
            name=f"capture-{channel_id}",
            daemon=False,
        )
        worker.start()
        workers.append(worker)

        # Stagger startup so every channel does not hit the NVR on the same tick.
        time.sleep(random.random() * MAX_CHANNEL_STARTUP_JITTER_SECONDS)

    logger.info("All timelapsed workers started")

    def forward_shutdown(signum, _frame):
        logger.info("Received signal %d; asking workers to stop", signum)
        for worker in workers:
            if worker.is_alive():
                worker.terminate()

    signal.signal(signal.SIGTERM, forward_shutdown)
    signal.signal(signal.SIGINT, forward_shutdown)

    for worker in workers:
        worker.join()

    logger.info("Timelapsed shut down")
