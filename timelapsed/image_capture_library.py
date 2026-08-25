import glob
import logging
import shutil
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Sequence

logger = logging.getLogger(__name__)

TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S_%Z"

TargetName = Literal["image", "timelapse"]


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

    def prune(self, channel_id: str, target_name: TargetName, retention: timedelta | None, now: datetime) -> int:
        """Delete files for a channel older than retention. Returns the number deleted.

        A retention of None means keep forever. Nothing captures storage growth
        automatically, so this is the only thing standing between a 5 second
        capture interval and a full disk.
        """
        if retention is None:
            return 0

        cutoff = now - retention
        deleted = 0
        for timestamp, path in self._timestamped_paths(channel_id, target_name):
            if timestamp >= cutoff:
                # Entries are sorted oldest first, so nothing after this is stale.
                break
            try:
                path.unlink()
                deleted += 1
            except OSError:
                logger.warning("Could not delete %s during pruning", path, exc_info=True)

        if deleted:
            logger.info(
                "Pruned %d %s file(s) older than %s for channel %s",
                deleted, target_name, str(retention), channel_id,
            )
        return deleted
