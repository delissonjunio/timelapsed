import logging
import multiprocessing
import random
import signal
import time
from bisect import bisect_left
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Sequence

from rich.logging import RichHandler

from timelapsed.config import get_config, validate_config
from timelapsed.image_capture_library import ImageCaptureLibrary
from timelapsed.image_processor import generate_timelapse
from timelapsed.nvr_capture_agent import NVRCaptureAgent
from timelapsed.schema import Cadence, Config

logger = logging.getLogger(__name__)

MAX_CHANNEL_STARTUP_JITTER_SECONDS = 1.4
PRUNE_INTERVAL = timedelta(hours=1)
# How many missing windows one submission will try to fill. The window that just
# closed is always among them; the rest is backlog, and filling it slowly keeps a
# first run after an outage from turning into an hours-long render queue.
MAX_WINDOWS_PER_RENDER = 4
# Furthest back to look for gaps when nothing else bounds it. Retention normally
# does: stills that have been pruned cannot be rendered from.
MAX_BACKFILL_HORIZON = timedelta(days=30)
# How long a render waits for its turn before going ahead anyway. A queue behind
# six channels of weekly renders is legitimately long, so this is not a deadline;
# it is there because a render killed mid-slot never gives the slot back, and a
# daemon that renders nothing again until restarted is worse than one that
# briefly runs two ffmpegs.
RENDER_SLOT_TIMEOUT_SECONDS = 3600


def apply_logging_config(config: Config) -> None:
    logging.basicConfig(
        level=config.logging_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
        force=True,
    )


@contextmanager
def _render_slot_held(render_slot, channel_id: str, cadence_name: str) -> Iterator[None]:
    """Hold one of the global render slots for the length of one ffmpeg run.

    A 1080p render peaks around 250 MB, so six channels rendering the same
    rollover at once is what walked a 2 GB guest into the OOM killer. Taken per
    window rather than per batch, so a channel with a backlog to fill still
    takes turns with everyone else.
    """
    if render_slot is None:
        yield
        return

    held = render_slot.acquire(block=False)
    if not held:
        logger.info(
            "Channel %s is waiting for a render slot before its %s render", channel_id, cadence_name
        )
        held = render_slot.acquire(timeout=RENDER_SLOT_TIMEOUT_SECONDS)
        if not held:
            logger.warning(
                "Channel %s waited %ds for a render slot; rendering %s without one",
                channel_id, RENDER_SLOT_TIMEOUT_SECONDS, cadence_name,
            )
    try:
        yield
    finally:
        if held:
            render_slot.release()


def _render_timelapse_entrypoint(
    config: Config,
    library: ImageCaptureLibrary,
    channel_id: str,
    cadence_name: str,
    windows: Sequence[tuple[datetime, datetime]],
    render_slot=None,
) -> None:
    """Process entrypoint for one cadence's outstanding renders, newest first."""
    apply_logging_config(config)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for start_time, end_time in windows:
        try:
            with _render_slot_held(render_slot, channel_id, cadence_name):
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
            logger.exception(
                "%s timelapse render failed for channel %s (window starting %s)",
                cadence_name, channel_id, start_time.isoformat(),
            )


def _count_within(sorted_times: Sequence[datetime], start: datetime, end: datetime) -> int:
    """How many of sorted_times fall in [start, end)."""
    return bisect_left(sorted_times, end) - bisect_left(sorted_times, start)


def pending_render_windows(
    library: ImageCaptureLibrary,
    config: Config,
    channel_id: str,
    cadence: Cadence,
    now: datetime,
    limit: int = MAX_WINDOWS_PER_RENDER,
    stills: Sequence[datetime] | None = None,
) -> list[tuple[datetime, datetime]]:
    """Complete periods of this cadence that have stills but no video, newest first.

    Renders are chosen by what is missing rather than by what just rolled over.
    An hour lost to a crash, a restart mid-ffmpeg or a render skipped because the
    previous one was still going is picked up on the next pass instead of being
    gone for good. The just-closed window is simply the newest missing one.

    Windows are aligned to the clock (the top of the hour, midnight, Monday) so a
    period has one canonical name however late the render fires.

    `stills` is the channel's capture times, which the caller can pass in when it
    is asking about several cadences at once -- at midnight all three are due, and
    that list is a directory scan of a week of frames.
    """
    if stills is None:
        stills = library.image_timestamps(channel_id)
    if not stills:
        return []

    rendered = library.rendered_window_starts(channel_id, cadence.name)
    bounds = [span for span in (config.image_retention, config.retention_for(cadence.name)) if span is not None]
    horizon = min(bounds) if bounds else MAX_BACKFILL_HORIZON

    # Periods are floored on the configured wall clock, so a "daily" is the local
    # day rather than the UTC one. Each bound is converted straight back to UTC:
    # `rendered` and `stills` are UTC, and the render is stored under a filename
    # stamped with %Z that parse_timelapse_filename splits on '-', which a zone
    # abbreviated "-03" would not survive.
    zone = config.render_timezone
    local_now = now.astimezone(zone)

    # The period containing `now` is still filling up, so the newest candidate is
    # the one before it.
    oldest_start = cadence.floor(max(local_now - horizon, stills[0].astimezone(zone)))
    period_start = cadence.floor(local_now) - cadence.window

    windows: list[tuple[datetime, datetime]] = []
    while period_start >= oldest_start and len(windows) < limit:
        # Stepping by the window walks local wall clock, so on a DST transition
        # day the period is an hour short or long of `cadence.window`. Nothing
        # is skipped, the video is just that much shorter or longer.
        start = period_start.astimezone(timezone.utc)
        end = (period_start + cadence.window).astimezone(timezone.utc)
        already_rendered = _count_within(rendered, start, end) > 0
        if not already_rendered and _count_within(stills, start, end) >= config.timelapse_min_frames:
            windows.append((start, end))
        period_start -= cadence.window

    return windows


class RenderScheduler:
    """Runs timelapse renders in background processes, one at a time per cadence.

    Rendering inline would stall capture for as long as ffmpeg runs, so each
    render gets its own process. If a render is still going when the next one is
    due, the new one is skipped rather than queued -- piling up ffmpeg processes
    on a small VM is what this is trying to avoid. Nothing is lost by skipping:
    the window stays missing, and `pending_render_windows` offers it again.
    """

    def __init__(self, config: Config, library: ImageCaptureLibrary, channel_id: str, render_slot=None):
        self.config = config
        self.library = library
        self.channel_id = channel_id
        self.render_slot = render_slot
        self._processes: dict[str, multiprocessing.Process] = {}

    def submit(self, cadence: Cadence, windows: Sequence[tuple[datetime, datetime]]) -> bool:
        """Render these windows in one background process. No windows, no process."""
        if not windows:
            return False

        running = self._processes.get(cadence.name)
        if running is not None and running.is_alive():
            logger.warning(
                "Previous %s render for channel %s is still running; skipping this one",
                cadence.name, self.channel_id,
            )
            return False

        process = multiprocessing.Process(
            target=_render_timelapse_entrypoint,
            args=(
                self.config, self.library, self.channel_id, cadence.name, list(windows), self.render_slot,
            ),
            name=f"render-{cadence.name}-{self.channel_id}",
            daemon=False,
        )
        process.start()
        self._processes[cadence.name] = process
        logger.info(
            "Started %s timelapse render for channel %s: %d window(s) from %s (pid %s)",
            cadence.name, self.channel_id, len(windows), windows[-1][0].isoformat(), process.pid,
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


def capture_continuously(
    channel_id: str,
    capture_agent: NVRCaptureAgent,
    library: ImageCaptureLibrary,
    config: Config,
    render_slot=None,
) -> None:
    """Capture one channel forever: snapshot, store, render on rollover, prune, sleep."""
    apply_logging_config(config)

    shutting_down = False

    def request_shutdown(signum, _frame):
        nonlocal shutting_down
        logger.info("Channel %s received signal %d; stopping after this cycle", channel_id, signum)
        shutting_down = True

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    scheduler = RenderScheduler(config, library, channel_id, render_slot)

    # Seeded with the current time so a restart does not re-render a cadence that
    # is already up to date; each one fires on its next genuine rollover. Held on
    # the configured wall clock, since that is what the rollover checks compare.
    last_run_at: dict[str, datetime] = {
        cadence.name: datetime.now(tz=timezone.utc).astimezone(config.render_timezone)
        for cadence in config.timelapse_cadences
    }
    last_pruned_at: datetime | None = None

    # A restart is one of the ways a window goes missing -- renders are children
    # of this unit, so restarting mid-ffmpeg kills them -- so the first thing a
    # worker does is look for gaps rather than wait an hour to notice.
    startup = datetime.now(tz=timezone.utc)
    stills = library.image_timestamps(channel_id)
    for cadence in config.timelapse_cadences:
        scheduler.submit(
            cadence, pending_render_windows(library, config, channel_id, cadence, startup, stills=stills)
        )

    while not shutting_down:
        now = datetime.now(tz=timezone.utc)

        try:
            image_data, extension = capture_agent.capture_image(channel_id, config.capture_resolution)
            library.store_image(channel_id, extension, image_data, now)
            logger.debug("Stored image for channel %s", channel_id)
        except Exception:
            logger.exception("Capture cycle failed for channel %s, continuing", channel_id)

        # Rollovers are judged on the configured wall clock, so a "daily" closes
        # at local midnight. Only the decision moves: the windows themselves come
        # back from pending_render_windows in UTC.
        local_now = now.astimezone(config.render_timezone)

        try:
            due = [
                cadence for cadence in config.timelapse_cadences
                if cadence.is_due(local_now, last_run_at[cadence.name])
            ]
            if due:
                # Scanned once for all of them: at midnight every cadence is due.
                stills = library.image_timestamps(channel_id)
                for cadence in due:
                    last_run_at[cadence.name] = local_now
                    scheduler.submit(
                        cadence,
                        pending_render_windows(library, config, channel_id, cadence, now, stills=stills),
                    )

            # Prune hourly: often enough to keep disk flat, cheap enough to ignore.
            if last_pruned_at is None or (now - last_pruned_at) >= PRUNE_INTERVAL:
                last_pruned_at = now
                library.prune(channel_id, "image", config.image_retention, now)
                for cadence in config.timelapse_cadences:
                    library.prune(
                        channel_id, "timelapse", config.retention_for(cadence.name), now,
                        cadence_name=cadence.name,
                    )

            # Checked every cycle, not every prune: a disk that fills between
            # hourly prunes would otherwise lose an hour of captures. The check
            # itself is a statvfs, and the expensive part only runs below the floor.
            library.reclaim(
                config.channels, config.minimum_free_bytes, config.longest_cadence_window, now,
            )
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
    library.clear_scratch()
    capture_agent = NVRCaptureAgent(config.nvr_url, config.nvr_username, config.nvr_password)

    # Shared by every channel: ffmpeg, not the capture loop, is what this guest
    # runs out of memory on, so concurrency is capped across the daemon rather
    # than per channel.
    render_slot = multiprocessing.BoundedSemaphore(config.max_concurrent_renders)

    logger.info(
        "Timelapsed starting: %d channel(s) [%s], cadences [%s], every %.0fs, %d render(s) at a time",
        len(config.channels),
        ", ".join(config.channels),
        ", ".join(cadence.name for cadence in config.timelapse_cadences),
        config.capture_interval.total_seconds(),
        config.max_concurrent_renders,
    )

    # One process per channel, not a fixed-size pool: these workers never return,
    # so a pool smaller than the channel count would silently never start the rest.
    workers: list[multiprocessing.Process] = []
    for channel_id in config.channels:
        worker = multiprocessing.Process(
            target=capture_continuously,
            args=(channel_id, capture_agent, library, config, render_slot),
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
