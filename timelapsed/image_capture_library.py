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

TargetName = Literal["image", "keyframe", "timelapse"]

RECLAIM_LOCK_NAME = ".reclaim.lock"
# Render scratch space, kept inside the library so staged frames can be
# hardlinked instead of copied. Dot-prefixed and holding neither an `image` nor
# a `timelapse` directory, so nothing mistakes it for a channel.
SCRATCH_DIRECTORY_NAME = ".render"


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


def _nearest_within(
    entries: Sequence[tuple[datetime, Path]], taken_at: datetime, search_max_distance: timedelta
) -> Path | None:
    """The entry closest to taken_at, or None when nothing is within the distance."""
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


class ImageCaptureLibrary:
    """Filesystem-backed store for captured images and rendered timelapses.

    Layout:
        {root}/{channel_id}/image/YYYYMMDD_HHMMSS_UTC.jpg
        {root}/{channel_id}/keyframe/YYYYMMDD_HHMMSS_UTC.jpg
        {root}/{channel_id}/timelapse/{cadence}_{start}-{end}.mp4

    The filename is the index: because timestamps are fixed-width UTC, sorting
    filenames lexicographically sorts them chronologically. There is no database.

    `keyframe` holds one promoted still per local day, hardlinked from `image`, and
    is what the monthly and progress renders read. It exists because those windows
    are far longer than the stills survive: a month of stills for six channels is
    ~380 GB, a month of keyframes is 42 MB.
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

    def store_keyframe(self, channel_id: str, source_path: Path, promoted_for: datetime) -> Path:
        """Promote a captured still into the long-lived keyframe track.

        Hardlinked, not copied: the keyframe track sits on the same filesystem as
        the stills, so this costs one inode and no bytes. When image retention
        unlinks the original eight days from now, this name keeps the inode alive
        -- which is the entire trick, because holding the stills instead would
        mean 32 days of them, ~380 GB for six channels on a 200 GB disk.

        Named for the instant it was promoted *for* -- local noon, expressed in
        UTC -- rather than for the still's real capture time. That makes promotion
        idempotent by filename, and puts frames exactly 24 hours apart so the
        video reads as one frame a day instead of as jitter.
        """
        keyframe_path = (
            self._path_for_channel(channel_id, "keyframe")
            / f"{_generate_image_filename(promoted_for)}{source_path.suffix}"
        )
        keyframe_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            os.link(source_path, keyframe_path)
        except FileExistsError:
            pass  # Already promoted. The only mutation here is idempotent by design.
        except OSError:
            shutil.copy(source_path, keyframe_path)  # Across a filesystem boundary.
        return keyframe_path

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

    def retrieve_image(
        self,
        channel_id: str,
        taken_at: datetime,
        search_max_distance: timedelta | None,
        entries: Sequence[tuple[datetime, Path]] | None = None,
    ) -> Path | None:
        """Find the image taken at taken_at, or the nearest one within search_max_distance.

        `entries` is the channel's stills as (timestamp, path), which a caller
        asking about several instants at once can pass in. Scanning is the whole
        cost here -- at a 10 second interval over 8 days of retention the still
        directory holds ~69,000 files -- so backfilling a week of keyframes wants
        one scan rather than seven.
        """
        if entries is None:
            if image := self._retrieve_exact_image_path(channel_id, taken_at):
                return image

            if search_max_distance is None:
                return None

            entries = self._timestamped_paths(channel_id, "image")

        return _nearest_within(entries, taken_at, search_max_distance or timedelta(0))

    def retrieve_images_within(
        self, channel_id: str, start: datetime, end: datetime, target_name: TargetName = "image"
    ) -> Sequence[Path]:
        """Every frame captured in the inclusive window [start, end], oldest first."""
        return [
            path
            for timestamp, path in self._timestamped_paths(channel_id, target_name)
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

    def prune_superseded(self, channel_id: str, cadence_name: str, keep_newest: int = 1) -> int:
        """Delete all but the newest videos of a cadence, judged by how far each reaches.

        For an anchored cadence every render covers everything the previous one
        did and more, so the older files are strict prefixes holding nothing the
        newest does not. Age-based retention cannot do this job at all: an
        anchored video's start is day one of the project, so pruning on age
        deletes the current video and keeps nothing.
        """
        entries = []
        for _, path in self._timestamped_paths(channel_id, "timelapse"):
            name, _, finishes = parse_timelapse_filename(path.stem)
            if name == cadence_name:
                entries.append((finishes, path))
        entries.sort(key=lambda entry: entry[0])

        deleted = 0
        for _, path in entries[: max(0, len(entries) - keep_newest)]:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                logger.warning("Could not delete superseded %s", path, exc_info=True)

        if deleted:
            logger.info(
                "Deleted %d superseded %s video(s) for channel %s", deleted, cadence_name, channel_id
            )
        return deleted

    def frame_entries(
        self, channel_id: str, target_name: TargetName = "image"
    ) -> list[tuple[datetime, Path]]:
        """Every stored frame as (capture time, path), oldest first.

        What `image_timestamps` throws the paths away from. Keyframe promotion
        needs both halves, and needs them out of a single scan.
        """
        return self._timestamped_paths(channel_id, target_name)

    def image_timestamps(self, channel_id: str, target_name: TargetName = "image") -> list[datetime]:
        """Every stored frame's capture time for a channel, oldest first.

        One directory scan answering "which windows have frames", so deciding
        what still needs rendering does not restat the library once per window.
        """
        return [timestamp for timestamp, _ in self._timestamped_paths(channel_id, target_name)]

    def rendered_windows(self, channel_id: str, cadence_name: str) -> list[tuple[datetime, datetime]]:
        """The (start, end) of every stored video of one cadence, oldest first.

        An anchored cadence is judged on how far its video reaches rather than on
        where it starts -- its start never moves -- so the end has to survive the
        lookup.
        """
        windows = []
        for _, path in self._timestamped_paths(channel_id, "timelapse"):
            name, starts, finishes = parse_timelapse_filename(path.stem)
            if name == cadence_name:
                windows.append((starts, finishes))
        return windows

    def images_after(
        self, channel_id: str, after: datetime | None, until: datetime, limit: int
    ) -> list[tuple[Path, datetime]]:
        """Stills captured after `after` and no later than `until`, oldest first.

        For consumers that walk the library forward from where they left off.
        `until` exists because store_image writes in place with no rename, so the
        newest file on disk can still be partially written; callers hold back a
        capture interval rather than reading a truncated JPEG.
        """
        channel_path = self._path_for_channel(channel_id, "image")
        if not channel_path.is_dir():
            return []

        # Compare stems as strings before parsing any of them. The format is
        # fixed-width UTC, so lexicographic order is chronological order, and a
        # caller walking forward discards almost everything it sees. Parsing
        # first instead costs ~550ms per pass on a full 8-day channel, which the
        # analyzer would then pay for every channel on every poll, forever.
        after_stem = _generate_image_filename(after) if after is not None else None
        until_stem = _generate_image_filename(until)

        stems = []
        for entry in os.scandir(channel_path):
            stem = entry.name.rsplit(".", 1)[0]
            if after_stem is not None and stem <= after_stem:
                continue
            if stem > until_stem:
                continue
            stems.append(entry.name)

        found = []
        for name in sorted(stems)[:limit]:
            try:
                timestamp = _parse_image_filename(name.rsplit(".", 1)[0])
            except ValueError:
                continue
            found.append((channel_path / name, timestamp))
        return found

    def rendered_window_starts(self, channel_id: str, cadence_name: str) -> list[datetime]:
        """The start time of every stored video of one cadence, oldest first."""
        return [starts for starts, _ in self.rendered_windows(channel_id, cadence_name)]

    def scratch_directory(self) -> Path:
        """Where a render stages its frames and writes its output.

        Deliberately inside the library rather than /tmp: same filesystem means
        `os.link` works, so staging a thousand frames costs no bytes and no copy
        time, and an in-flight render cannot fill a small root partition.
        """
        scratch_path = self.root_path / SCRATCH_DIRECTORY_NAME
        scratch_path.mkdir(parents=True, exist_ok=True)
        return scratch_path

    def clear_scratch(self) -> None:
        """Drop anything a killed render left staged. Startup only -- it does not
        distinguish live renders from dead ones."""
        shutil.rmtree(self.root_path / SCRATCH_DIRECTORY_NAME, ignore_errors=True)

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
        videos follow. The keyframe-sourced videos come next because they can be
        re-rendered from the keyframe track whenever -- progress before monthly,
        since a progress video is superseded on the next rollover anyway. Weekly
        goes last: its stills were pruned months ago, so once it is gone no amount
        of CPU brings it back.

        The keyframe track itself is deliberately absent. It is the one
        unrecoverable thing in the library -- a pruned still cannot be re-promoted
        -- and at ~500 MB a year against a steady state near 100 GB, sacrificing
        it buys under half a percent of the disk while destroying the beginning of
        the record, which is the part worth having. If the floor cannot be met
        without it, the floor cannot be met; the error at the end of `reclaim`
        says so.
        """
        stills = self._sorted_across_channels(channel_ids, "image")
        cutoff = now - protected_window
        return [
            ("stills past every render window", [e for e in stills if e[0] < cutoff]),
            ("hourly videos", self._sorted_across_channels(channel_ids, "timelapse", "hourly")),
            ("stills an upcoming render needs", [e for e in stills if e[0] >= cutoff]),
            ("daily videos", self._sorted_across_channels(channel_ids, "timelapse", "daily")),
            ("progress videos", self._sorted_across_channels(channel_ids, "timelapse", "progress")),
            ("monthly videos", self._sorted_across_channels(channel_ids, "timelapse", "monthly")),
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
                        status = path.stat()
                        path.unlink()
                    except OSError:
                        continue  # Vanished under us, or not ours to delete.
                    # A still that has been promoted to a keyframe is hardlinked,
                    # so dropping this name frees nothing -- the inode lives on
                    # under the other one. Counting those bytes would stop the
                    # loop short of the floor it just claimed to have reached.
                    reclaimed += status.st_size if status.st_nlink <= 1 else 0
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
