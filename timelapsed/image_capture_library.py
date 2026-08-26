import errno
import fcntl
import glob
import logging
import os
import shutil
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Sequence

logger = logging.getLogger(__name__)

TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S_%Z"

TargetName = Literal["image", "timelapse"]

RECLAIM_LOCK_NAME = ".reclaim.lock"


def _generate_timelapse_filename(cadence_name: str, recording_starts: datetime, recording_finishes: datetime) -> str:
    return (
        f"{cadence_name}_"
        + recording_starts.strftime(TIMESTAMP_FORMAT)
        + "-"
        + recording_finishes.strftime(TIMESTAMP_FORMAT)
    )


def parse_timelapse_filename(stem: str) -> tuple[str, datetime, datetime]:
    """Split a stored timelapse stem back into (cadence, start, end).

    Stems look like "weekly_20250601_120000_UTC-20250608_120000_UTC".
    """
    cadence_name, separator, window = stem.partition("_")
    if not separator or "-" not in window:
        raise ValueError(f"Not a timelapse filename: {stem!r}")

    starts, _, finishes = window.partition("-")
    return cadence_name, _parse_image_filename(starts), _parse_image_filename(finishes)


def _generate_image_filename(taken_at: datetime) -> str:
    return taken_at.strftime(TIMESTAMP_FORMAT)


def _parse_image_filename(filename: str) -> datetime:
    return datetime.strptime(filename, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


class ImageCaptureLibrary:
    """Filesystem-backed store for captured images and rendered timelapses.

    Layout:
        {root}/{channel_id}/image/YYYYMMDD_HHMMSS_UTC.jpg
        {root}/{channel_id}/timelapse/{start}-{end}.mp4

    The filename is the index: because timestamps are fixed-width UTC, sorting
    filenames lexicographically sorts them chronologically. There is no database.
    """

    root_path: Path

    def __init__(self, root_path: Path):
        self.root_path = root_path

    def _path_for_channel(self, channel_id: str, target_name: TargetName) -> Path:
        return self.root_path / channel_id / target_name

    def _timestamped_paths(self, channel_id: str, target_name: TargetName) -> list[tuple[datetime, Path]]:
        """All parseable files for a channel as (timestamp, path), sorted by timestamp."""
        channel_path = self._path_for_channel(channel_id, target_name)
        if not channel_path.is_dir():
            return []

        entries = []
        for file_path in channel_path.iterdir():
            if not file_path.is_file():
                continue
            try:
                if target_name == "timelapse":
                    _, timestamp, _ = parse_timelapse_filename(file_path.stem)
                else:
                    timestamp = _parse_image_filename(file_path.stem)
            except ValueError:
                continue
            entries.append((timestamp, file_path))

        entries.sort(key=lambda entry: entry[0])
        return entries

    def store_image(self, channel_id: str, file_format_extension: str, content: bytes, taken_at: datetime) -> Path:
        image_path = (
            self._path_for_channel(channel_id, "image")
            / f"{_generate_image_filename(taken_at)}.{file_format_extension}"
        )
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(content)
        return image_path

    def store_timelapse(
        self,
        channel_id: str,
        timelapse_path: Path,
        cadence_name: str,
        recording_starts: datetime,
        recording_finishes: datetime,
    ) -> Path:
        stored_path = (
            self._path_for_channel(channel_id, "timelapse")
            / f"{_generate_timelapse_filename(cadence_name, recording_starts, recording_finishes)}{timelapse_path.suffix}"
        )
        stored_path.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy(timelapse_path, stored_path)
        return stored_path

    def _retrieve_exact_image_path(self, channel_id: str, taken_at: datetime) -> Path | None:
        image_path_prefix = _generate_image_filename(taken_at)
        channel_path = self._path_for_channel(channel_id, "image")
        for found_image in glob.iglob(root_dir=channel_path, pathname=f"{image_path_prefix}.*"):
            return channel_path / found_image
        return None

    def retrieve_image(self, channel_id: str, taken_at: datetime, search_max_distance: timedelta | None) -> Path | None:
        """Find the image taken at taken_at, or the nearest one within search_max_distance."""
        if image := self._retrieve_exact_image_path(channel_id, taken_at):
            return image

        if search_max_distance is None:
            return None

        entries = self._timestamped_paths(channel_id, "image")
        if not entries:
            return None

        timestamps = [timestamp for timestamp, _ in entries]
        position = bisect_left(timestamps, taken_at)

        closest_path = None
        smallest_difference = search_max_distance
        for index in (position - 1, position, position + 1):
            if 0 <= index < len(entries):
                difference = abs(taken_at - timestamps[index])
                if difference <= smallest_difference:
                    closest_path = entries[index][1]
                    smallest_difference = difference

        return closest_path

    def retrieve_images_within(self, channel_id: str, start: datetime, end: datetime) -> Sequence[Path]:
        """Every image captured in the inclusive window [start, end], oldest first."""
        return [
            path
            for timestamp, path in self._timestamped_paths(channel_id, "image")
            if start <= timestamp <= end
        ]

    def prune(
        self,
        channel_id: str,
        target_name: TargetName,
        retention: timedelta | None,
        now: datetime,
        cadence_name: str | None = None,
    ) -> int:
        """Delete files for a channel older than retention. Returns the number deleted.

        A retention of None means keep forever. Nothing captures storage growth
        automatically, so this is the only thing standing between a 5 second
        capture interval and a full disk.

        `cadence_name` narrows a timelapse prune to one cadence, so hourly videos
        can expire quickly while weekly ones are kept indefinitely.
        """
        if retention is None:
            return 0

        cutoff = now - retention
        deleted = 0
        for timestamp, path in self._timestamped_paths(channel_id, target_name):
            if timestamp >= cutoff:
                # Entries are sorted oldest first, so nothing after this is stale.
                break
            if cadence_name is not None and parse_timelapse_filename(path.stem)[0] != cadence_name:
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError:
                logger.warning("Could not delete %s during pruning", path, exc_info=True)

        if deleted:
            logger.info(
                "Pruned %d %s file(s) older than %s for channel %s",
                deleted, cadence_name or target_name, str(retention), channel_id,
            )
        return deleted

    def free_bytes(self) -> int:
        """Bytes still available on the filesystem holding the library."""
        self.root_path.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root_path).free

    def _sorted_across_channels(
        self, channel_ids: Sequence[str], target_name: TargetName, cadence_name: str | None = None
    ) -> list[tuple[datetime, Path]]:
        entries: list[tuple[datetime, Path]] = []
        for channel_id in channel_ids:
            for timestamp, path in self._timestamped_paths(channel_id, target_name):
                if cadence_name is not None and parse_timelapse_filename(path.stem)[0] != cadence_name:
                    continue
                entries.append((timestamp, path))
        entries.sort(key=lambda entry: entry[0])
        return entries

    def _reclaim_tiers(
        self, channel_ids: Sequence[str], protected_window: timedelta, now: datetime
    ) -> list[tuple[str, list[tuple[datetime, Path]]]]:
        """What to sacrifice under disk pressure, least valuable first.

        The ordering is about what cannot be recovered. Stills older than the
        longest cadence window are free to drop: every render that could have
        used them has already run. Hourly clips are the most disposable history.
        Only then are stills a render still needs taken, because losing those
        degrades an upcoming video rather than destroying a finished one. Daily
        and weekly videos are the archive and go last.
        """
        stills = self._sorted_across_channels(channel_ids, "image")
        cutoff = now - protected_window
        return [
            ("stills past every render window", [e for e in stills if e[0] < cutoff]),
            ("hourly videos", self._sorted_across_channels(channel_ids, "timelapse", "hourly")),
            ("stills an upcoming render needs", [e for e in stills if e[0] >= cutoff]),
            ("daily videos", self._sorted_across_channels(channel_ids, "timelapse", "daily")),
            ("weekly videos", self._sorted_across_channels(channel_ids, "timelapse", "weekly")),
        ]

    def reclaim(
        self,
        channel_ids: Sequence[str],
        minimum_free_bytes: int,
        protected_window: timedelta,
        now: datetime,
    ) -> int:
        """Delete past the configured retention until the free-space floor is met.

        Retention alone cannot promise a floor: it bounds age, not bytes, so a
        larger camera count, a bigger image, or a longer video archive silently
        moves the steady state. This is the backstop that keeps the daemon
        writing when that happens. Returns the number of bytes reclaimed.

        Every channel worker calls this, so the work is serialised behind a lock
        file: whichever process gets there does the reclaiming and the rest skip
        the cycle rather than racing each other into over-deleting.
        """
        if minimum_free_bytes <= 0:
            return 0

        free = self.free_bytes()
        if free >= minimum_free_bytes:
            return 0

        lock_path = self.root_path / RECLAIM_LOCK_NAME
        lock_file = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    return 0  # Another worker is already reclaiming.
                raise

            # Re-read: the holder of the lock may have already fixed this.
            free = self.free_bytes()
            if free >= minimum_free_bytes:
                return 0

            needed = minimum_free_bytes - free
            logger.warning(
                "Only %.1f GB free, below the %.1f GB floor; reclaiming %.1f GB past retention",
                free / 1e9, minimum_free_bytes / 1e9, needed / 1e9,
            )

            reclaimed = 0
            for tier_name, candidates in self._reclaim_tiers(channel_ids, protected_window, now):
                if reclaimed >= needed:
                    break
                deleted = 0
                for _, path in candidates:
                    if reclaimed >= needed:
                        break
                    try:
                        size = path.stat().st_size
                        path.unlink()
                    except OSError:
                        continue  # Vanished under us, or not ours to delete.
                    reclaimed += size
                    deleted += 1
                if deleted:
                    logger.warning(
                        "Reclaimed %d %s (%.1f GB so far)", deleted, tier_name, reclaimed / 1e9
                    )

            if reclaimed < needed:
                logger.error(
                    "Reclaimed only %.1f GB of the %.1f GB needed: nothing left to delete. "
                    "The library cannot fit this configuration; shorten retention or grow the disk.",
                    reclaimed / 1e9, needed / 1e9,
                )
            return reclaimed
        finally:
            os.close(lock_file)
