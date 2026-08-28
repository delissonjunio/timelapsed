"""The recognition daemon.

A separate process from the capture daemon on purpose. The capture loop sleeps
`interval - elapsed` and already warns when a cycle eats 80% of the interval;
running inference inside it would spend the capture budget directly. Separate
also means separate systemd limits, and means recognition can be stopped without
stopping capture.

It reads the stills the capture daemon already wrote, so it adds no NVR load and
no extra storage beyond its own index and crops.
"""
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.logging import RichHandler

from timelapsed.analysis.identities import IdentityMatcher
from timelapsed.analysis.index import AnalysisIndex, from_epoch, to_epoch
from timelapsed.analysis.models import BodyEmbedder, ObjectDetector, PlateReader
from timelapsed.analysis.pipeline import FrameAnalyzer
from timelapsed.config import get_config, validate_config
from timelapsed.image_capture_library import ImageCaptureLibrary
from timelapsed.nvr_footage import NVRFootageClient, SegmentIndexer
from timelapsed.schema import Config

logger = logging.getLogger(__name__)

# How long to wait when there is nothing new to analyse. Frames arrive every
# `capture_interval`, so there is no point spinning faster than that.
IDLE_SLEEP_SECONDS = 5
# Retention runs on this cadence, not every pass.
PRUNE_INTERVAL = timedelta(hours=6)
# How often to ask the NVR what footage it holds. The busiest channels keep
# ~11 days of recordings, so even hourly would never miss anything; this is
# about how stale the footage lane may be, not about losing segments.
SEGMENT_SYNC_INTERVAL = timedelta(minutes=15)
# Frames handled between watermark writes. A crash loses at most this much work,
# and it keeps the index from taking a write per frame during a long backfill.
WATERMARK_EVERY = 25

shutting_down = False


def _request_shutdown(signal_number, _frame) -> None:
    global shutting_down
    logger.info("Signal %s received, finishing the current frame", signal_number)
    shutting_down = True


def build_analyzer(config: Config, index: AnalysisIndex) -> FrameAnalyzer:
    models = config.analysis_model_root
    threads = config.analysis_threads

    detector = ObjectDetector(models / "yolox_tiny.onnx", threads=threads)

    body_embedder = matcher = None
    if config.analysis_reid_enabled:
        body_embedder = BodyEmbedder(models / "reid.onnx", threads=threads)
        matcher = IdentityMatcher(
            index,
            kind="body",
            threshold=config.analysis_reid_threshold,
            window=config.analysis_reid_window,
        )

    plate_reader = None
    if config.analysis_plate_channels:
        plate_reader = PlateReader(
            models / "plate_detect.onnx", models / "plate_ocr.onnx", threads=threads
        )

    return FrameAnalyzer(
        index=index,
        crops_root=config.analysis_crop_root,
        detector=detector,
        score_threshold=config.analysis_score_threshold,
        body_embedder=body_embedder,
        plate_reader=plate_reader,
        identity_matcher=matcher,
        plate_channels=tuple(config.analysis_plate_channels),
        plate_confidence=config.analysis_plate_confidence,
    )


def run_once(config: Config, library: ImageCaptureLibrary, index: AnalysisIndex, analyzer: FrameAnalyzer) -> int:
    """One pass over every channel. Returns how many frames were analysed."""
    horizon = datetime.now(tz=timezone.utc) - config.capture_interval
    analysed = 0

    for channel in config.channels:
        if shutting_down:
            break
        watermark = index.watermark(channel)
        frames = library.images_after(
            channel,
            from_epoch(watermark) if watermark is not None else None,
            horizon,
            config.analysis_batch_size,
        )
        if not frames:
            continue

        logger.debug("Channel %s: %d frames to analyse", channel, len(frames))
        for position, (path, taken_at) in enumerate(frames, start=1):
            if shutting_down:
                break
            try:
                analyzer.analyse(channel, path, taken_at)
            except FileNotFoundError:
                # Reclaim can delete a still between listing it and opening it.
                logger.debug("Still %s vanished before analysis", path)
            except Exception:
                logger.exception("Failed to analyse %s, skipping", path)
            analysed += 1
            if position % WATERMARK_EVERY == 0:
                index.set_watermark(channel, to_epoch(taken_at))

        index.set_watermark(channel, to_epoch(frames[-1][1]))

    return analysed


def prune(config: Config, index: AnalysisIndex) -> None:
    now = datetime.now(tz=timezone.utc)
    detections_before = (
        to_epoch(now - config.analysis_detection_retention)
        if config.analysis_detection_retention
        else None
    )
    events_before = (
        to_epoch(now - config.analysis_event_retention)
        if config.analysis_event_retention
        else None
    )
    removed_detections, removed_events = index.prune(detections_before, events_before)
    if removed_detections or removed_events:
        logger.info(
            "Pruned %d detections and %d events from the index",
            removed_detections, removed_events,
        )

    # Crops are not covered by the library's reclaim, which only walks the
    # per-channel image and timelapse directories. Left alone they would eat
    # into the free-space floor and make reclaim delete stills instead.
    referenced = index.orphaned_crops()
    removed_files = 0
    for crop in config.analysis_crop_root.rglob("*.jpg"):
        relative = str(crop.relative_to(config.analysis_crop_root))
        if relative not in referenced:
            crop.unlink(missing_ok=True)
            removed_files += 1
    if removed_files:
        logger.info("Removed %d crop files no index row referenced", removed_files)


def run() -> None:
    config = get_config()
    logging.basicConfig(
        level=config.logging_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )
    for warning in validate_config(config):
        logger.warning(warning)

    if not config.analysis_enabled:
        logger.error("Analysis is disabled in the config ([analysis] enabled = false). Nothing to do.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    library = ImageCaptureLibrary(config.image_capture_library_root)
    index = AnalysisIndex(config.analysis_index_path)

    # Lives in this daemon because this daemon is the index's one writer. It
    # asks the NVR for its search index only -- a few pages of XML -- so it adds
    # nothing to the snapshot load the capture daemon already puts on it.
    footage = SegmentIndexer(
        NVRFootageClient(config.nvr_url, config.nvr_username, config.nvr_password),
        index,
        config.channels,
    )

    try:
        analyzer = build_analyzer(config, index)
    except FileNotFoundError as error:
        logger.error("%s", error)
        sys.exit(1)

    logger.info(
        "Analyzer watching channels %s (score >= %.2f, re-ID %s, plates on %s)",
        ", ".join(config.channels),
        config.analysis_score_threshold,
        "on" if config.analysis_reid_enabled else "off",
        ", ".join(config.analysis_plate_channels) or "no channels",
    )
    for channel, through in sorted(index.watermarks().items()):
        logger.info("  channel %s analysed through %s", channel, from_epoch(through))

    last_pruned_at = None
    last_swept_at = None
    while not shutting_down:
        started = time.monotonic()

        # Before the analysis pass, so a first start builds the footage map
        # while the frame backlog is still being chewed through, not after.
        try:
            now = datetime.now(tz=timezone.utc)
            if last_swept_at is None or (now - last_swept_at) >= SEGMENT_SYNC_INTERVAL:
                last_swept_at = now
                footage.sync_all(now)
        except Exception:
            logger.exception("NVR footage sweep failed, continuing")

        try:
            analysed = run_once(config, library, index, analyzer)
        except Exception:
            logger.exception("Analysis pass failed, continuing")
            analysed = 0

        # Commit what has gone quiet, and only that. Events still being seen
        # stay open across passes: closing them here would file one car sitting
        # in the driveway as a fresh event -- and a fresh, unvoted plate read --
        # every pass, which is a read every few seconds in live operation.
        try:
            if analysed:
                analyzer.tracker.close_stale()
        except Exception:
            logger.exception("Closing finished events failed, continuing")

        # Fold together the identities that online matching split apart. Runs
        # after the batch rather than per frame: it is a whole-set operation, and
        # a fragment created early in a pass often only becomes mergeable once
        # the pass has seen the poses in between.
        try:
            if analysed and analyzer.identity_matcher is not None:
                analyzer.identity_matcher.consolidate(config.analysis_reid_merge_threshold)
        except Exception:
            logger.exception("Identity consolidation failed, continuing")

        try:
            now = datetime.now(tz=timezone.utc)
            if last_pruned_at is None or (now - last_pruned_at) >= PRUNE_INTERVAL:
                last_pruned_at = now
                prune(config, index)
        except Exception:
            logger.exception("Index pruning failed, continuing")

        if analysed:
            elapsed = time.monotonic() - started
            logger.info(
                "Analysed %d frames in %.1fs (%.0f ms/frame)",
                analysed, elapsed, 1000 * elapsed / analysed,
            )
        elif not shutting_down:
            time.sleep(IDLE_SLEEP_SECONDS)

    # Open events hold un-voted plate reads; closing them commits those.
    analyzer.tracker.close_all()
    index.close()
    logger.info("Analyzer stopped")


if __name__ == "__main__":
    run()
