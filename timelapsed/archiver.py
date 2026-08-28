"""The replica: every segment the NVR records, copied to local disk.

A separate daemon, like capture, analysis and the viewer, because its work is
different in kind: hours of sequential downloading that must never stall the
capture interval or an inference pass. It reads the footage mirror the analyzer
maintains and writes nothing anywhere except the archive tree -- the recognition
index is opened read-only, so the analyzer stays that database's one writer.

There is no table of what has been archived. The archive is indexed by its
filenames, the way the still library is: `{root}/{channel}/{YYYYMMDD}/
{start}_{end}_{name}.mp4`, where `name` is the device's own segment id. A file
existing is the fact of it being archived; a crash mid-fetch leaves only a
scratch file, cleared on startup. The times live in the filename so a segment
remains findable by moment long after the NVR has expired it and the mirror
row is all that remembers it existed -- or nothing does.

Fetches run oldest-first, deliberately: the device wraps its quota by deleting
oldest footage, so the oldest unarchived segment is always the one at risk.
Sequentially too -- parallel downloads were measured to buy nothing, the
~128 Mbit/s is the path, not the request.
"""
import logging
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from timelapsed.analysis.index import AnalysisIndex
from timelapsed.config import get_config
from timelapsed.nvr_footage import NVRFootageClient

logger = logging.getLogger(__name__)

# How long a segment's end must be in the past before it is fetched. A segment
# still being recorded keeps its start while its end walks forward; fetching it
# early would archive a truncation under a name the archiver then considers
# done. Event chains run to ~20 minutes on this device, so half an hour.
SETTLE = timedelta(minutes=30)
# How long to sleep when nothing is pending. The mirror only changes when the
# analyzer sweeps, which is every 15 minutes; there is nothing to hurry for.
IDLE_SLEEP_SECONDS = 60
# Wall-clock deadline for one download: a generous floor plus a 1 MB/s floor on
# throughput, against a link measured at ~16 MB/s.
DOWNLOAD_DEADLINE_FLOOR_SECONDS = 120.0
REMUX_TIMEOUT_SECONDS = 300
# More rows than any channel's history holds; the archiver wants all of them.
ALL_SEGMENTS = 1_000_000

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
SCRATCH_DIRECTORY_NAME = ".scratch"

shutting_down = False


def _request_shutdown(signal_number, _frame) -> None:
    global shutting_down
    logger.info("Signal %s received, finishing the current segment", signal_number)
    shutting_down = True


@dataclass(frozen=True)
class PendingSegment:
    channel: str
    name: str  # the device's segment id, from the playback URI's name=
    started_at: datetime
    ended_at: datetime
    size_bytes: int
    playback_uri: str


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(STAMP_FORMAT)


def parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, STAMP_FORMAT).replace(tzinfo=timezone.utc)


def segment_filename(started_at: datetime, ended_at: datetime, name: str) -> str:
    # The stamps carry no underscore, so the name -- which is full of them --
    # survives a split("_", 2) round trip.
    return f"{stamp(started_at)}_{stamp(ended_at)}_{name}.mp4"


def parse_segment_filename(stem: str) -> tuple[datetime, datetime, str]:
    """(started_at, ended_at, device segment name). Raises ValueError on junk."""
    started, ended, name = stem.split("_", 2)
    if not name:
        raise ValueError(f"No segment name in {stem!r}")
    return parse_stamp(started), parse_stamp(ended), name


def uri_segment_name(playback_uri: str) -> str | None:
    values = parse_qs(urlparse(playback_uri).query).get("name")
    return values[0] if values else None


class SegmentArchiver:
    """Keeps the archive tree holding everything the footage mirror lists."""

    def __init__(
        self,
        client: NVRFootageClient,
        index: AnalysisIndex,
        root: Path,
        channels: list[str],
        retention: timedelta | None,
        minimum_free_bytes: int,
    ):
        self.client = client
        self.index = index
        self.root = root
        self.channels = channels
        self.retention = retention
        self.minimum_free_bytes = minimum_free_bytes
        # Device segment names already on disk. Seeded from a full scan, then
        # maintained incrementally -- fetches add, reclaim removes.
        self._archived: set[str] = set()
        # Names that failed this run. Retried only on restart: a segment the
        # device has expired fails identically forever, and retrying it every
        # pass would starve the fetches that can still succeed.
        self._failed: set[str] = set()

    # --- layout ---

    def _day_directory(self, channel: str, started_at: datetime) -> Path:
        return self.root / channel / started_at.astimezone(timezone.utc).strftime("%Y%m%d")

    def _scratch(self) -> Path:
        scratch = self.root / SCRATCH_DIRECTORY_NAME
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch

    def clear_scratch(self) -> None:
        shutil.rmtree(self.root / SCRATCH_DIRECTORY_NAME, ignore_errors=True)

    def scan(self) -> None:
        """Seed the archived-name set from what is actually on disk."""
        self._archived.clear()
        for archived in self.root.glob("*/*/*.mp4"):
            try:
                _, _, name = parse_segment_filename(archived.stem)
            except ValueError:
                continue
            self._archived.add(name)
        logger.info("Archive at %s holds %d segment(s)", self.root, len(self._archived))

    # --- choosing work ---

    def pending(self, now: datetime) -> list[PendingSegment]:
        """Every settled, unarchived segment the mirror lists, oldest first."""
        settled_before = now - SETTLE
        # Anything retention would delete tomorrow must not be fetched today,
        # or the deep channels (205 days on ch1) become a fetch/delete loop.
        cutoff = now - self.retention if self.retention else None

        found: list[PendingSegment] = []
        for channel in self.channels:
            for row in self.index.segments(channel=channel, limit=ALL_SEGMENTS):
                started_at = datetime.fromisoformat(row["starts"])
                ended_at = datetime.fromisoformat(row["finishes"])
                if ended_at > settled_before:
                    continue
                if cutoff is not None and ended_at < cutoff:
                    continue
                name = uri_segment_name(row["playback_uri"])
                if not name or name in self._archived or name in self._failed:
                    continue
                found.append(PendingSegment(
                    channel=channel,
                    name=name,
                    started_at=started_at,
                    ended_at=ended_at,
                    size_bytes=row["size_bytes"],
                    playback_uri=row["playback_uri"],
                ))
        found.sort(key=lambda segment: segment.started_at)
        return found

    # --- fetching ---

    def fetch(self, segment: PendingSegment) -> Path:
        """Download one segment, remux it, and move it into place atomically.

        Staged entirely in scratch on the archive's own filesystem, so the final
        rename is atomic and a kill at any point leaves nothing that looks like
        a finished segment.
        """
        scratch = self._scratch()
        raw = scratch / f"{segment.name}.ps"
        remuxed = scratch / f"{segment.name}.mp4"
        try:
            deadline = DOWNLOAD_DEADLINE_FLOOR_SECONDS + segment.size_bytes / 1_000_000
            started = time.monotonic()
            written = self.client.download(segment.playback_uri, raw, deadline)

            # MPEG-PS to MP4 is a copy, not a transcode; the timeout is there
            # because run() kills the child on expiry, and an ffmpeg wedged on
            # malformed PS must not wedge the archiver with it.
            subprocess.run(
                [
                    "ffmpeg", "-loglevel", "error", "-y", "-i", str(raw),
                    "-c", "copy", "-movflags", "+faststart", str(remuxed),
                ],
                check=True, capture_output=True, timeout=REMUX_TIMEOUT_SECONDS,
            )
            if not remuxed.stat().st_size:
                raise ValueError("Remux produced an empty file")

            destination = self._day_directory(segment.channel, segment.started_at)
            destination.mkdir(parents=True, exist_ok=True)
            final = destination / segment_filename(segment.started_at, segment.ended_at, segment.name)
            remuxed.replace(final)
            self._archived.add(segment.name)

            elapsed = time.monotonic() - started
            logger.debug(
                "Archived %s: %.1f MB in %.1fs", final.name, written / 1e6, elapsed
            )
            return final
        finally:
            raw.unlink(missing_ok=True)
            remuxed.unlink(missing_ok=True)

    # --- retention ---

    def reclaim(self, now: datetime) -> int:
        """Age out the archive, then hold the free-space floor. Returns removals.

        Age first: it is the configured intent. The floor second, deleting the
        oldest whole days across every channel, because when the disk is the
        constraint the oldest footage is always the least valuable -- it is the
        closest to what retention would have deleted anyway.
        """
        removed = 0
        if self.retention is not None:
            cutoff = now - self.retention
            for archived in sorted(self.root.glob("*/*/*.mp4")):
                try:
                    _, ended_at, name = parse_segment_filename(archived.stem)
                except ValueError:
                    continue
                if ended_at < cutoff:
                    archived.unlink(missing_ok=True)
                    self._archived.discard(name)
                    removed += 1

        while self.minimum_free_bytes and self._free_bytes() < self.minimum_free_bytes:
            days = sorted(
                (day for day in self.root.glob("*/*") if day.is_dir()),
                key=lambda day: day.name,
            )
            if not days:
                break
            oldest = days[0]
            for archived in oldest.glob("*.mp4"):
                try:
                    self._archived.discard(parse_segment_filename(archived.stem)[2])
                except ValueError:
                    pass
                removed += 1
            logger.warning(
                "Archive under its free-space floor; dropping %s/%s",
                oldest.parent.name, oldest.name,
            )
            shutil.rmtree(oldest, ignore_errors=True)

        # A day whose last file just aged out is an empty directory forever
        # otherwise. rmdir refuses non-empty ones, which is exactly the check.
        for day in self.root.glob("*/*"):
            if day.is_dir():
                try:
                    day.rmdir()
                except OSError:
                    pass
        return removed

    def _free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    # --- the pass ---

    def run_once(self, now: datetime) -> int:
        """One full pass: fetch everything pending, then enforce retention."""
        fetched = 0
        queue = self.pending(now)
        if queue:
            logger.info(
                "%d segment(s) to archive, oldest from %s",
                len(queue), queue[0].started_at.isoformat(),
            )
        for segment in queue:
            if shutting_down:
                break
            try:
                self.fetch(segment)
                fetched += 1
            except Exception:
                # One bad segment -- expired on the device, malformed PS --
                # must not stop the replica behind it.
                self._failed.add(segment.name)
                logger.exception(
                    "Failed to archive %s segment %s, skipping until restart",
                    segment.channel, segment.name,
                )
        self.reclaim(datetime.now(tz=timezone.utc))
        return fetched


def run() -> None:
    from rich.logging import RichHandler

    config = get_config()
    logging.basicConfig(
        level=config.logging_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )

    if config.archive_root is None:
        logger.error("No [archive] root is configured. Nothing to do.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    # The analyzer creates and migrates the index; this daemon only reads it.
    # At first boot it may simply not exist yet.
    while not config.analysis_index_path.exists():
        if shutting_down:
            return
        logger.info("Waiting for the analyzer to create %s", config.analysis_index_path)
        time.sleep(IDLE_SLEEP_SECONDS)

    config.archive_root.mkdir(parents=True, exist_ok=True)
    archiver = SegmentArchiver(
        client=NVRFootageClient(config.nvr_url, config.nvr_username, config.nvr_password),
        index=AnalysisIndex(config.analysis_index_path, read_only=True),
        root=config.archive_root,
        channels=config.channels,
        retention=config.archive_retention,
        minimum_free_bytes=config.archive_minimum_free_bytes,
    )
    archiver.clear_scratch()
    archiver.scan()

    logger.info(
        "Archiver replicating channels [%s] into %s (retention %s, floor %.0f GB)",
        ", ".join(config.channels),
        config.archive_root,
        f"{config.archive_retention.days}d" if config.archive_retention else "none",
        config.archive_minimum_free_bytes / 1e9,
    )

    while not shutting_down:
        try:
            fetched = archiver.run_once(datetime.now(tz=timezone.utc))
        except Exception:
            # The mirror table may not exist yet (analyzer running an older
            # build), the device may be away: nothing here is fatal, the next
            # pass simply asks again.
            logger.exception("Archive pass failed, continuing")
            fetched = 0

        if fetched:
            logger.info("Archived %d segment(s)", fetched)
        elif not shutting_down:
            time.sleep(IDLE_SLEEP_SECONDS)

    archiver.index.close()
    logger.info("Archiver stopped")


if __name__ == "__main__":
    run()
