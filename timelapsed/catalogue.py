"""What the viewer serves, found on disk: clips, stills, archived segments.

Everything here answers by reading the filesystem at ask time. No databases
and no caching (the thumbnail bytes aside): the directories are small, the
filenames carry the metadata, and a stale answer is more annoying than a
directory scan is expensive.
"""
import logging
import re
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from timelapsed.archiver import parse_segment_filename
from timelapsed.image_capture_library import ImageCaptureLibrary, parse_timelapse_filename

logger = logging.getLogger(__name__)

THUMBNAIL_WIDTH = 384
THUMBNAIL_QUALITY = "6"  # ffmpeg -q:v, 2 best to 31 worst
THUMBNAIL_CACHE_SIZE = 64


class ThumbnailCache:
    """Downscaled camera stills, keyed by source path and mtime.

    The sidebar polls these every 30 seconds across every camera, and a 1080p
    still is ~230 KB, so serving them raw would be over a megabyte a refresh for
    a 170-pixel-wide box. ffmpeg is already a hard dependency for rendering, so
    it does the scaling; the cache means it runs once per new still rather than
    once per request.
    """

    def __init__(self, maximum_entries: int = THUMBNAIL_CACHE_SIZE):
        self._entries: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._maximum_entries = maximum_entries
        self._lock = threading.Lock()

    def get(self, source: Path) -> bytes | None:
        try:
            key = (str(source), source.stat().st_mtime_ns)
        except OSError:
            return None

        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key]

        thumbnail = self._render(source)
        if thumbnail is None:
            return None

        with self._lock:
            self._entries[key] = thumbnail
            self._entries.move_to_end(key)
            while len(self._entries) > self._maximum_entries:
                self._entries.popitem(last=False)
        return thumbnail

    @staticmethod
    def _render(source: Path) -> bytes | None:
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-loglevel", "error", "-i", str(source),
                    # min() so a still smaller than the tile is never upscaled
                    # into a file bigger than the original.
                    "-vf", f"scale='min({THUMBNAIL_WIDTH},iw)':-2",
                    "-q:v", THUMBNAIL_QUALITY, "-f", "image2", "-vcodec", "mjpeg", "pipe:1",
                ],
                capture_output=True, timeout=15, check=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            logger.warning("Could not build a thumbnail for %s: %s", source, error)
            return None
        return result.stdout or None


@dataclass(frozen=True)
class TimelapseEntry:
    channel_id: str
    cadence: str
    starts: datetime
    finishes: datetime
    path: Path

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size

    def as_dict(self) -> dict:
        return {
            "channel": self.channel_id,
            "cadence": self.cadence,
            "starts": self.starts.isoformat(),
            "finishes": self.finishes.isoformat(),
            "size_bytes": self.size_bytes,
            "url": f"/video/{self.channel_id}/{self.path.name}",
        }


class TimelapseCatalogue:
    """Reads the capture library from disk on every request.

    No caching: the directory is small (one file per cadence per period) and a
    stale list is more annoying than a directory scan is expensive.
    """

    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.library = ImageCaptureLibrary(root_path)

    def channels(self) -> list[str]:
        if not self.root_path.is_dir():
            return []
        return sorted(
            entry.name for entry in self.root_path.iterdir()
            if entry.is_dir() and (entry / "timelapse").is_dir()
        )

    def entries(self, channel_id: str | None = None, cadence: str | None = None) -> list[TimelapseEntry]:
        found = []
        for candidate_channel in self.channels():
            if channel_id is not None and candidate_channel != channel_id:
                continue

            for path in (self.root_path / candidate_channel / "timelapse").iterdir():
                if not path.is_file():
                    continue
                try:
                    entry_cadence, starts, finishes = parse_timelapse_filename(path.stem)
                except ValueError:
                    continue
                if cadence is not None and entry_cadence != cadence:
                    continue
                found.append(TimelapseEntry(candidate_channel, entry_cadence, starts, finishes, path))

        found.sort(key=lambda entry: entry.starts, reverse=True)
        return found

    def latest_still(self, channel_id: str) -> Path | None:
        """The most recent captured image for a channel, or None if it has none."""
        if channel_id not in self.channels_with_images():
            return None
        entries = self.library._timestamped_paths(channel_id, "image")
        return entries[-1][1] if entries else None

    def channels_with_images(self) -> list[str]:
        if not self.root_path.is_dir():
            return []
        return sorted(
            entry.name for entry in self.root_path.iterdir()
            if entry.is_dir() and (entry / "image").is_dir()
        )

    def resolve_video(self, channel_id: str, filename: str) -> Path | None:
        """Resolve a video path, refusing anything that escapes the library root."""
        base = (self.root_path / channel_id / "timelapse").resolve()
        try:
            candidate = (base / filename).resolve()
            candidate.relative_to(base)
        except (ValueError, OSError):
            return None
        return candidate if candidate.is_file() else None


class ArchiveCatalogue:
    """What the archiver has replicated, read straight off its filenames.

    No database and no caching: the archive is indexed by its filenames the way
    the still library is, and the only questions asked of it are tiny --
    "what covers this moment" is one or two day-directory listings.
    """

    def __init__(self, root: Path):
        self.root = root

    def segments(self, channel: str, start: datetime, end: datetime, limit: int = 500) -> list[dict]:
        """Archived segments overlapping [start, end], oldest first.

        Walks only the day directories the window touches, plus the day before
        the window opens -- a segment can straddle midnight, but never more
        than one (they run minutes, not days).
        """
        found = []
        day = start.date() - timedelta(days=1)
        while day <= end.date() and len(found) < limit:
            directory = self.root / channel / day.strftime("%Y%m%d")
            day += timedelta(days=1)
            if not directory.is_dir():
                continue
            for stored in directory.iterdir():
                if stored.suffix != ".mp4":
                    continue
                try:
                    started_at, ended_at, _ = parse_segment_filename(stored.stem)
                except ValueError:
                    continue
                if ended_at < start or started_at > end:
                    continue
                found.append({
                    "starts": started_at.isoformat(),
                    "finishes": ended_at.isoformat(),
                    "size_bytes": stored.stat().st_size,
                    "url": f"/archive/{channel}/{directory.name}/{stored.name}",
                })
        found.sort(key=lambda segment: segment["starts"])
        return found[:limit]

    def resolve(self, channel: str, day: str, filename: str) -> Path | None:
        """Resolve an archived file, refusing anything that escapes the root."""
        if not re.fullmatch(r"\d{8}", day) or not filename.endswith(".mp4"):
            return None
        base = self.root.resolve()
        try:
            candidate = (base / channel / day / filename).resolve()
            candidate.relative_to(base)
        except (ValueError, OSError):
            return None
        return candidate if candidate.is_file() else None
