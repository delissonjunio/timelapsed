"""What the server is actually doing, gathered in one pass.

The viewer can already answer "what did the cameras see". This answers the
questions you ask when something looks wrong: is the disk filling, is every
camera still writing frames, has the renderer fallen behind, and how far behind
is recognition -- with, when it is moving, how long until it catches up.

Everything here is derived from the same two places the rest of the project
keeps its state: the directory listing and the recognition index. There is no
new bookkeeping to keep in sync, which means the report cannot drift from what
is actually on disk. The one exception is `AnalysisProgress`, which remembers
recent watermark readings in memory purely so a *rate* can be quoted; losing it
on restart costs nothing but a minute of measurement.

Scanning is the expensive part -- eight days of stills at a five second interval
is ~138,000 files per channel -- so `SystemStatusCollector` caches the report for
a few seconds and the scan itself avoids parsing timestamps. Frame filenames are
`%Y%m%d_%H%M%S_%Z`, which is fixed-width and zero-padded, so lexicographic order
*is* chronological order: "how many frames since 14:00" is a string comparison,
and only the first and last names are ever parsed into datetimes.
"""
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from timelapsed.archiver import parse_segment_filename
from timelapsed.config import validate_config
from timelapsed.image_capture_library import (
    FRAME_STEM,
    SCRATCH_DIRECTORY_NAME,
    TIMESTAMP_FORMAT,
    ImageCaptureLibrary,
    parse_timelapse_filename,
)
from timelapsed.schema import Cadence, Config

logger = logging.getLogger(__name__)

# How long a report is served without any work at all. Past this, a request
# still gets the report it finds, at once, and starts one rescan in the
# background so the next request gets a newer one: on a full library the scan
# is a couple of seconds, and a poll that waited on it made the page feel hung.
# The page polls every twenty seconds, so while it is open the numbers on
# screen are at most a poll old and no poll ever waits.
REPORT_TTL_SECONDS = 12.0
# ...but a report older than this is not worth showing. Nobody has asked for a
# while -- the page was closed -- so the first request back scans in the
# foreground rather than open on stale numbers until the next poll.
REPORT_MAX_AGE_SECONDS = 60.0

# A camera is late when it has written nothing for this many capture intervals.
# Generous on purpose: one missed snapshot is a blip the daemon already retries,
# and the floor keeps a sub-second interval from being called stale constantly.
CAPTURE_STALE_INTERVALS = 6
CAPTURE_STALE_FLOOR = timedelta(minutes=2)

# The shortest span of watermark samples worth quoting a rate from. Under this
# the arithmetic is mostly measuring when the analyzer happened to commit its
# every-25-frames watermark write rather than how fast it is going.
MIN_RATE_SPAN_SECONDS = 45.0
# Samples older than this are dropped: an hour-old reading says nothing useful
# about whether the backlog is closing now.
RATE_HISTORY_SECONDS = 3600.0
# Under this the watermark has not moved at all, which is a stopped analyzer
# rather than a slow one. Not exactly zero: the rate is a division, and a
# watermark that crept forward by a second over ten minutes is still stopped.
STOPPED_RATE = 0.005

# The systemd units a normal deployment runs, in the order the page shows them.
KNOWN_UNITS = ("timelapsed", "timelapsed-analyzer", "timelapsed-archiver", "timelapsed-web")
UNIT_PROPERTIES = (
    "LoadState", "ActiveState", "SubState", "Result", "NRestarts",
    "MemoryCurrent", "ExecMainStartTimestamp", "ActiveEnterTimestamp",
)

# Windows the capture report quotes yields over.
RECENT_WINDOWS = {"hour": timedelta(hours=1), "day": timedelta(days=1)}

# What makes a directory under the library root a channel rather than something
# else that happens to live there.
TRACKS = ("image", "keyframe", "timelapse")


def _stem_at(moment: datetime) -> str:
    """The frame filename an instant would be stored under.

    The comparison key for the whole scan: because the format is fixed-width,
    "is this frame newer than that instant" is `stem >= _stem_at(instant)`.
    """
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def _moment_of(stem: str) -> datetime:
    return datetime.strptime(stem, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _seconds(span: timedelta | None) -> float | None:
    return span.total_seconds() if span is not None else None


@dataclass(frozen=True)
class FrameScan:
    """One directory of timestamp-named frames, counted in a single pass.

    `shared_bytes` is the part of `bytes` that is a hardlink to a file counted
    somewhere else. Keyframes are promoted stills, linked rather than copied, so
    adding a keyframe directory's size to its channel's still directory would
    count those bytes twice -- and the double count is exactly the thing the
    keyframe track was designed to avoid paying.
    """

    files: int = 0
    bytes: int = 0
    shared_bytes: int = 0
    oldest: datetime | None = None
    newest: datetime | None = None
    # Label from RECENT_WINDOWS (plus "watermark", when analysis asks) -> how
    # many frames, and how many bytes, fall at or after that cutoff.
    recent_files: dict[str, int] = field(default_factory=dict)
    recent_bytes: dict[str, int] = field(default_factory=dict)

    @property
    def unique_bytes(self) -> int:
        return self.bytes - self.shared_bytes

    def as_dict(self) -> dict:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "unique_bytes": self.unique_bytes,
            "shared_bytes": self.shared_bytes,
            "oldest": _iso(self.oldest),
            "newest": _iso(self.newest),
            "recent_files": dict(self.recent_files),
            "recent_bytes": dict(self.recent_bytes),
        }


def scan_frames(directory: Path, cutoffs: dict[str, datetime] | None = None) -> FrameScan:
    """Count, size and date a frame directory without parsing every filename.

    `cutoffs` names instants to count from; each becomes a string comparison
    against the frame stem, which is what keeps this affordable over a directory
    holding six figures of stills.
    """
    thresholds = {
        label: _stem_at(moment) for label, moment in (cutoffs or {}).items()
    }
    recent_files = {label: 0 for label in thresholds}
    recent_bytes = {label: 0 for label in thresholds}

    files = total = shared = 0
    oldest = newest = None

    try:
        listing = os.scandir(directory)
    except OSError:
        # A channel that has captured nothing has no directory yet, which is a
        # legitimate state rather than a failure -- it is what a camera added to
        # the config an hour ago looks like.
        return FrameScan(recent_files=recent_files, recent_bytes=recent_bytes)

    with listing:
        for entry in listing:
            stem = entry.name.rpartition(".")[0]
            if not FRAME_STEM.fullmatch(stem):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                # Pruning is allowed to delete a frame between the listing and
                # the stat. Skipping it is more accurate than failing the report.
                continue

            files += 1
            total += info.st_size
            if info.st_nlink > 1:
                shared += info.st_size
            if oldest is None or stem < oldest:
                oldest = stem
            if newest is None or stem > newest:
                newest = stem
            for label, threshold in thresholds.items():
                if stem >= threshold:
                    recent_files[label] += 1
                    recent_bytes[label] += info.st_size

    return FrameScan(
        files=files,
        bytes=total,
        shared_bytes=shared,
        oldest=_moment_of(oldest) if oldest else None,
        newest=_moment_of(newest) if newest else None,
        recent_files=recent_files,
        recent_bytes=recent_bytes,
    )


@dataclass(frozen=True)
class RenderedVideo:
    cadence: str
    starts: datetime
    finishes: datetime
    bytes: int
    written_at: datetime


def scan_timelapses(directory: Path) -> list[RenderedVideo]:
    """Every stored video in one channel's timelapse directory.

    Parsed in full, unlike the frame tracks: there is one file per period per
    cadence, so even years of hourly videos is a directory small enough that the
    exact answer costs less than working out a cheaper approximation.
    """
    found: list[RenderedVideo] = []
    try:
        listing = os.scandir(directory)
    except OSError:
        return found

    with listing:
        for entry in listing:
            try:
                cadence, starts, finishes = parse_timelapse_filename(Path(entry.name).stem)
                info = entry.stat(follow_symlinks=False)
            except (ValueError, OSError):
                continue
            found.append(
                RenderedVideo(
                    cadence=cadence,
                    starts=starts,
                    finishes=finishes,
                    bytes=info.st_size,
                    written_at=datetime.fromtimestamp(info.st_mtime, tz=timezone.utc),
                )
            )
    found.sort(key=lambda video: video.starts)
    return found


@dataclass(frozen=True)
class ArchiveChannelScan:
    files: int = 0
    bytes: int = 0
    oldest_start: datetime | None = None
    newest_end: datetime | None = None


def scan_archive_channel(directory: Path) -> ArchiveChannelScan:
    """One channel of the replica, counted without parsing every filename.

    The archive names files `{start}_{end}_{name}.mp4` with fixed-width stamps,
    so lexicographic min/max is chronological min/max and only the two boundary
    names are ever parsed -- the same trick the frame scan plays.
    """
    files = total = 0
    oldest = newest = None
    try:
        days = [entry.path for entry in os.scandir(directory) if entry.is_dir()]
    except OSError:
        return ArchiveChannelScan()

    for day in days:
        try:
            listing = os.scandir(day)
        except OSError:
            continue
        with listing:
            for entry in listing:
                if not entry.name.endswith(".mp4"):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                files += 1
                total += info.st_size
                stem = entry.name[:-4]
                if oldest is None or stem < oldest:
                    oldest = stem
                if newest is None or stem > newest:
                    newest = stem

    oldest_start = newest_end = None
    try:
        if oldest is not None:
            oldest_start = parse_segment_filename(oldest)[0]
        if newest is not None:
            newest_end = parse_segment_filename(newest)[1]
    except ValueError:
        pass
    return ArchiveChannelScan(
        files=files, bytes=total, oldest_start=oldest_start, newest_end=newest_end
    )


def scan_tree(root: Path) -> tuple[int, int]:
    """(files, bytes) under a directory tree. Used for the crop store."""
    files = total = 0
    for directory, _, names in os.walk(root, onerror=lambda _: None):
        for name in names:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
                files += 1
            except OSError:
                continue
    return files, total


def _path_group_bytes(path: Path) -> int:
    """A SQLite database's real footprint: the file plus its WAL sidecars.

    Under WAL a busy index carries a -wal that can be a sizeable fraction of the
    database, and reporting only the main file understates the disk it holds.
    """
    total = 0
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


class AnalysisProgress:
    """Watermark readings over time, so the page can quote a rate and an ETA.

    The index records how far analysis has reached but not how fast it got
    there, and a single reading cannot tell a backlog that is closing from one
    that is growing. Sampling on each collection is enough: the page polls every
    few seconds, and a couple of minutes of history gives a stable rate over a
    process that moves in bursts of `WATERMARK_EVERY` frames.

    In memory on purpose. This is a measurement, not a record; persisting it
    would mean a schema, a writer and a retention policy for something whose
    entire value is that it describes the last few minutes.
    """

    def __init__(self, history_seconds: float = RATE_HISTORY_SECONDS):
        self.history_seconds = history_seconds
        self._samples: list[tuple[float, dict[str, int]]] = []
        self._lock = threading.Lock()

    def record(self, wall_clock: float, watermarks: dict[str, int]) -> None:
        with self._lock:
            self._samples.append((wall_clock, dict(watermarks)))
            horizon = wall_clock - self.history_seconds
            self._samples = [sample for sample in self._samples if sample[0] >= horizon]

    def rate(self, channel: str) -> float | None:
        """Analysed seconds of footage per wall-clock second, or None if unknown.

        A rate of 1.0 means analysis is exactly keeping pace with the cameras.
        Above 1.0 it is closing a backlog; below 1.0 and behind, it is losing.
        """
        with self._lock:
            samples = [
                (at, marks[channel]) for at, marks in self._samples if channel in marks
            ]
        if len(samples) < 2:
            return None

        (first_at, first_mark), (last_at, last_mark) = samples[0], samples[-1]
        span = last_at - first_at
        if span < MIN_RATE_SPAN_SECONDS:
            return None
        return (last_mark - first_mark) / span


class SystemStatusCollector:
    """Builds the status report, and caches it briefly.

    One collector per server, holding the config it was built from plus the
    little state that has to survive between requests: the watermark samples,
    and the last report. The recognition reader is passed in per call rather
    than held, so this works identically with recognition off.
    """

    def __init__(
        self,
        config: Config,
        ttl_seconds: float = REPORT_TTL_SECONDS,
        max_age_seconds: float = REPORT_MAX_AGE_SECONDS,
    ):
        self.config = config
        self.library = ImageCaptureLibrary(config.image_capture_library_root)
        self.progress = AnalysisProgress()
        self.ttl_seconds = ttl_seconds
        self.max_age_seconds = max_age_seconds
        self._lock = threading.Lock()
        self._cached: dict | None = None
        # When the cached report's scan began, so its age counts the scan too.
        self._cached_at = 0.0
        self._refresher: threading.Thread | None = None

    def report(self, recognition=None, force: bool = False) -> dict:
        """The report, from the cache whenever there is one worth serving.

        Three ages. Under `ttl_seconds` the cached report comes back as is.
        Past that it still comes back at once -- the scan takes seconds on a
        full library and a poll must not wait on it -- and one background rescan
        starts, so the next request finds a newer report. Past
        `max_age_seconds`, or with `force`, the request scans in the foreground
        and waits for the answer.
        """
        with self._lock:
            previous = self._cached
            age = time.monotonic() - self._cached_at
            if previous is not None and not force and age < self.max_age_seconds:
                if age >= self.ttl_seconds:
                    self._refresh_in_background(recognition)
                cached = dict(previous)
                cached["cached"] = True
                cached["cache_age_seconds"] = round(age, 1)
                return cached

        started = time.monotonic()
        report = self._collect(recognition)
        self._store(report, started)
        return dict(report)

    def _refresh_in_background(self, recognition) -> None:
        """Start one rescan, unless one is already running. Called under the lock."""
        if self._refresher is not None and self._refresher.is_alive():
            return

        def refresh() -> None:
            started = time.monotonic()
            try:
                self._store(self._collect(recognition), started)
            except Exception:
                logger.exception("Could not rebuild the status report in the background")

        self._refresher = threading.Thread(target=refresh, name="status-refresh", daemon=True)
        self._refresher.start()

    def _store(self, report: dict, started: float) -> None:
        with self._lock:
            # A forced scan and a background one can overlap; whichever began
            # later describes the library later, and the other is discarded.
            if started >= self._cached_at:
                self._cached, self._cached_at = report, started

    def join_refresh(self, timeout: float | None = None) -> None:
        """Wait for a background rescan, if one is running, to land in the cache."""
        with self._lock:
            refresher = self._refresher
        if refresher is not None:
            refresher.join(timeout)

    # --- collection ---

    def _collect(self, recognition) -> dict:
        started = time.monotonic()
        now = datetime.now(tz=timezone.utc)
        config = self.config

        channels = self._channels()
        cutoffs = {label: now - span for label, span in RECENT_WINDOWS.items()}

        watermarks: dict[str, int] = {}
        if recognition is not None:
            try:
                watermarks = recognition.watermark_epochs()
            except Exception:
                logger.exception("Could not read analysis watermarks for the status page")
        self.progress.record(time.time(), watermarks)

        scans: dict[str, dict[str, FrameScan]] = {}
        videos: dict[str, list[RenderedVideo]] = {}
        for channel in channels:
            channel_root = config.image_capture_library_root / channel
            # The watermark joins the cutoffs so the analysis backlog is an
            # exact frame count out of the same scan, rather than a lag divided
            # by the nominal interval.
            image_cutoffs = dict(cutoffs)
            if channel in watermarks:
                # One second past it: the frame the watermark names has already
                # been analysed, so counting from the watermark itself would
                # report a permanent backlog of one.
                image_cutoffs["watermark"] = datetime.fromtimestamp(
                    watermarks[channel] + 1, tz=timezone.utc
                )
            scans[channel] = {
                "image": scan_frames(channel_root / "image", image_cutoffs),
                "keyframe": scan_frames(channel_root / "keyframe", cutoffs),
            }
            videos[channel] = scan_timelapses(channel_root / "timelapse")

        report = {
            "generated_at": _iso(now),
            "now": int(now.timestamp()),
            "cached": False,
            "cache_age_seconds": 0.0,
            "host": self._host(),
            "services": self._services(),
            "disk": self._disk(),
            "storage": self._storage(channels, scans, videos, recognition),
            "capture": self._capture(now, channels, scans),
            "renders": self._renders(now, channels, scans, videos),
            "retention": self._retention(now, channels, scans, videos),
            "growth": self._growth(now, channels, scans, videos),
            "analysis": self._analysis(now, channels, scans, watermarks, recognition),
            "archive": self._archive(now, channels, recognition),
            "config": self._config_summary(),
        }
        report["checks"] = self._checks(report)
        report["collected_in_ms"] = round((time.monotonic() - started) * 1000, 1)
        return report

    def _channels(self) -> list[str]:
        """Configured channels plus any the library holds, shortest id first.

        The union rather than the config alone: a channel dropped from the
        config still owns disk, and that disk is exactly what someone reading a
        storage page is trying to account for.

        A directory counts only once it holds one of the three tracks, which is
        the same test the viewer's catalogue applies. Not every subdirectory of
        the library root is a channel -- the default `analysis_root` is
        `{library}/index`, so a deployment with recognition on has the crop store
        and the SQLite file sitting right there -- and without this the page
        reported `index` as a retired camera holding zero stills.
        """
        on_disk = set()
        root = self.config.image_capture_library_root
        if root.is_dir():
            on_disk = {
                entry.name
                for entry in root.iterdir()
                if entry.is_dir()
                and entry.name != SCRATCH_DIRECTORY_NAME
                and any((entry / track).is_dir() for track in TRACKS)
            }
        return sorted(set(self.config.channels) | on_disk, key=lambda name: (len(name), name))

    # --- host ---

    @staticmethod
    def _host() -> dict:
        host: dict = {
            "hostname": platform.node(),
            "platform": platform.platform(terse=True),
            "python": sys.version.split()[0],
            "pid": os.getpid(),
            "threads": threading.active_count(),
            "cpus": os.cpu_count(),
            "uptime_seconds": None,
            "load_average": None,
            "memory_total_bytes": None,
            "memory_available_bytes": None,
            "process_rss_bytes": None,
        }

        # /proc is Linux-only and this project is also developed on macOS, so
        # every reading here is optional and its absence is reported as null
        # rather than as an error.
        try:
            host["uptime_seconds"] = float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            pass

        try:
            host["load_average"] = [round(value, 2) for value in os.getloadavg()]
        except (OSError, AttributeError):
            pass

        try:
            meminfo = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, value = line.partition(":")
                meminfo[key] = int(value.split()[0]) * 1024
            host["memory_total_bytes"] = meminfo.get("MemTotal")
            host["memory_available_bytes"] = meminfo.get("MemAvailable")
        except (OSError, ValueError, IndexError):
            pass

        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    host["process_rss_bytes"] = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            pass

        return host

    @staticmethod
    def _services() -> dict | None:
        """What systemd thinks of the three units, or None where it cannot be asked.

        Read-only and unauthenticated: `systemctl show` needs no privilege, and
        the unit names are constants, so nothing user-supplied reaches the
        command line.

        `unavailable` says why there is nothing to show, because there are two
        very different reasons and only reporting the absence made the difference
        invisible. The viewer's unit is sandboxed, and `systemctl` reaches the
        bus over a Unix socket: with AF_UNIX missing from its
        `RestrictAddressFamilies` every call fails instantly with "Failed to
        connect to bus", which looks exactly like a machine that has no systemd.
        """
        if shutil.which("systemctl") is None:
            return None

        units: dict[str, dict] = {}
        refused = None
        for unit in KNOWN_UNITS:
            try:
                result = subprocess.run(
                    ["systemctl", "show", unit, "--no-pager",
                     *(f"--property={name}" for name in UNIT_PROPERTIES)],
                    capture_output=True, text=True, timeout=5, check=False,
                )
            except (OSError, subprocess.SubprocessError) as error:
                return {"unavailable": f"systemctl could not be run: {error}"}

            properties = {}
            for line in result.stdout.splitlines():
                key, _, value = line.partition("=")
                properties[key] = value

            if properties.get("LoadState") is None:
                # No LoadState at all means the command produced nothing, which
                # is a failure to ask rather than an answer. First line of
                # stderr: it is systemd's own, and it is one short sentence.
                refused = (result.stderr or "").strip().splitlines()
                refused = refused[0] if refused else f"systemctl exited {result.returncode}"
                continue
            if properties["LoadState"] == "not-found":
                continue

            restarts = properties.get("NRestarts") or "0"
            memory = properties.get("MemoryCurrent") or ""
            units[unit] = {
                "active": properties.get("ActiveState"),
                "sub": properties.get("SubState"),
                "result": properties.get("Result"),
                # A unit that has never started reports these as literal
                # placeholders rather than as empty, so they are normalised out.
                "since": properties.get("ActiveEnterTimestamp") or None,
                "started": properties.get("ExecMainStartTimestamp") or None,
                "restarts": int(restarts) if restarts.isdigit() else 0,
                "memory_bytes": int(memory) if memory.isdigit() else None,
            }
        if units:
            return units
        return {"unavailable": refused} if refused else None

    # --- disk ---

    def _disk(self) -> dict:
        config = self.config
        # The library root may legitimately not exist yet on a fresh install, in
        # which case the filesystem that would hold it is still the right answer.
        root = config.image_capture_library_root
        probe = root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent

        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            return {"available": False}

        floor = config.minimum_free_bytes
        return {
            "available": True,
            "path": str(root),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_fraction": round(usage.used / usage.total, 4) if usage.total else None,
            "minimum_free_bytes": floor,
            # What is spendable before the reclaim starts deleting past
            # retention. Negative means it is already reclaiming.
            "headroom_bytes": usage.free - floor if floor else None,
            "floor_met": usage.free >= floor if floor else None,
        }

    # --- storage ---

    def _storage(self, channels, scans, videos, recognition) -> dict:
        by_channel = []
        totals = {"image": 0, "keyframe": 0, "timelapse": 0}
        total_files = {"image": 0, "keyframe": 0, "timelapse": 0}

        for channel in channels:
            image, keyframe = scans[channel]["image"], scans[channel]["keyframe"]
            channel_videos = videos[channel]

            by_cadence: dict[str, dict] = {}
            for video in channel_videos:
                bucket = by_cadence.setdefault(
                    video.cadence, {"files": 0, "bytes": 0, "newest": None, "oldest": None}
                )
                bucket["files"] += 1
                bucket["bytes"] += video.bytes
                if bucket["newest"] is None or video.starts > bucket["newest"]:
                    bucket["newest"] = video.starts
                if bucket["oldest"] is None or video.starts < bucket["oldest"]:
                    bucket["oldest"] = video.starts
            for bucket in by_cadence.values():
                bucket["newest"] = _iso(bucket["newest"])
                bucket["oldest"] = _iso(bucket["oldest"])

            video_bytes = sum(video.bytes for video in channel_videos)
            # The still is the inode's owner and the keyframe is the borrower, so
            # the discount goes on the keyframe side. Using `image.unique_bytes`
            # here would drop a promoted still out of the library total
            # altogether, which is the opposite of the double count.
            totals["image"] += image.bytes
            totals["keyframe"] += keyframe.unique_bytes
            totals["timelapse"] += video_bytes
            total_files["image"] += image.files
            total_files["keyframe"] += keyframe.files
            total_files["timelapse"] += len(channel_videos)

            by_channel.append({
                "channel": channel,
                "image": image.as_dict(),
                "keyframe": keyframe.as_dict(),
                "timelapse": {
                    "files": len(channel_videos),
                    "bytes": video_bytes,
                    "by_cadence": by_cadence,
                },
                "bytes": image.bytes + keyframe.unique_bytes + video_bytes,
            })

        crops = {"files": 0, "bytes": 0, "path": str(self.config.analysis_crop_root)}
        index = {"bytes": 0, "path": str(self.config.analysis_index_path), "exists": False}
        if recognition is not None:
            crop_files, crop_bytes = scan_tree(self.config.analysis_crop_root)
            crops.update(files=crop_files, bytes=crop_bytes)
            index.update(
                bytes=_path_group_bytes(self.config.analysis_index_path),
                exists=self.config.analysis_index_path.exists(),
            )

        return {
            "channels": by_channel,
            "by_track": {
                track: {"bytes": totals[track], "files": total_files[track]}
                for track in totals
            },
            "crops": crops,
            "index": index,
            "total_bytes": sum(totals.values()) + crops["bytes"] + index["bytes"],
        }

    # --- capture ---

    def _capture(self, now, channels, scans) -> dict:
        interval = self.config.capture_interval
        stale_after = max(interval * CAPTURE_STALE_INTERVALS, CAPTURE_STALE_FLOOR)

        rows = []
        for channel in channels:
            image, keyframe = scans[channel]["image"], scans[channel]["keyframe"]
            age = (now - image.newest).total_seconds() if image.newest else None

            expected = {
                label: span.total_seconds() / interval.total_seconds()
                for label, span in RECENT_WINDOWS.items()
            }
            # A channel that started capturing an hour ago cannot have a day's
            # worth of frames, so the yield is measured against the shorter of
            # the window and how long this channel has actually been running.
            if image.oldest is not None:
                running = (now - image.oldest).total_seconds()
                expected = {
                    label: max(1.0, min(count, running / interval.total_seconds()))
                    for label, count in expected.items()
                }

            yields = {
                label: round(min(image.recent_files.get(label, 0) / count, 9.99), 3)
                for label, count in expected.items()
            }
            mean_bytes = round(image.bytes / image.files) if image.files else None

            configured = channel in self.config.channels
            state = "idle"
            if not configured:
                state = "retired"
            elif image.newest is None:
                state = "silent"
            elif age is not None and age > stale_after.total_seconds():
                state = "stale"
            else:
                state = "live"

            rows.append({
                "channel": channel,
                "configured": configured,
                "state": state,
                "last_frame": _iso(image.newest),
                "last_frame_age_seconds": age,
                "first_frame": _iso(image.oldest),
                "frames": image.files,
                "frames_recent": dict(image.recent_files),
                "expected_recent": {label: round(count, 1) for label, count in expected.items()},
                "yield": yields,
                "mean_frame_bytes": mean_bytes,
                "bytes_per_day": (
                    round(mean_bytes * 86400 / interval.total_seconds()) if mean_bytes else None
                ),
                "keyframes": keyframe.files,
                "last_keyframe": _iso(keyframe.newest),
            })

        live = sum(1 for row in rows if row["state"] == "live")
        return {
            "interval_seconds": interval.total_seconds(),
            "stale_after_seconds": stale_after.total_seconds(),
            "resolution": (
                f"{self.config.capture_resolution.width}x{self.config.capture_resolution.height}"
            ),
            "live": live,
            "expected": len(self.config.channels),
            "channels": rows,
        }

    # --- renders ---

    def _renders(self, now, channels, scans, videos) -> dict:
        cadences = self.config.timelapse_cadences
        rows = []
        for channel in channels:
            stored = videos[channel]
            starts_by_cadence: dict[str, list[datetime]] = {}
            reach_by_cadence: dict[str, datetime] = {}
            for video in stored:
                starts_by_cadence.setdefault(video.cadence, []).append(video.starts)
                if video.finishes > reach_by_cadence.get(video.cadence, video.finishes - timedelta(seconds=1)):
                    reach_by_cadence[video.cadence] = video.finishes
            for starts in starts_by_cadence.values():
                starts.sort()

            cadence_rows = []
            for cadence in cadences:
                of_cadence = [video for video in stored if video.cadence == cadence.name]
                newest = max(of_cadence, key=lambda video: video.starts, default=None)
                # A period is only outstanding if there were ever frames to
                # render it from, so the search stops at the oldest frame on the
                # track this cadence reads.
                missing = self._missing_periods(
                    now, cadence, scans[channel][cadence.source].oldest,
                    starts_by_cadence, reach_by_cadence,
                )
                cadence_rows.append({
                    "cadence": cadence.name,
                    "source": cadence.source,
                    "files": len(of_cadence),
                    "bytes": sum(video.bytes for video in of_cadence),
                    "newest_period": _iso(newest.starts) if newest else None,
                    "newest_written_at": _iso(newest.written_at) if newest else None,
                    "newest_age_seconds": (
                        (now - newest.written_at).total_seconds() if newest else None
                    ),
                    "missing_periods": missing["count"],
                    "latest_period": _iso(missing["latest_period"]),
                    "latest_rendered": missing["latest_rendered"],
                    "fps": self.config.output_fps_for(cadence.name),
                    "retention_seconds": _seconds(self.config.retention_for(cadence.name)),
                })
            rows.append({"channel": channel, "cadences": cadence_rows})

        return {
            "cadences": [cadence.name for cadence in cadences],
            "channels": rows,
            "outstanding": sum(
                entry["missing_periods"] for row in rows for entry in row["cadences"]
            ),
        }

    def _missing_periods(
        self, now, cadence: Cadence, first_frame, starts_by_cadence, reach_by_cadence
    ) -> dict:
        """Closed periods with no stored video, counted from the videos on disk.

        Deliberately *not* the renderer's own queue. `pending_render_windows`
        needs every frame timestamp on the source track to apply its min-frames
        rule, which is the one scan this page is built to avoid. Counting the
        gaps in what was actually produced answers the same question -- is the
        renderer keeping up -- from data already in hand, and it stays honest
        about the difference: a period skipped for having too few frames is
        reported here as missing, because on disk it is.

        A video is matched to a period by its start *falling inside* the period
        rather than equalling it. Windows are clock-aligned now, but the library
        also holds videos written before they were -- an hourly starting at
        12:04:41 rather than 12:00:00 -- and on exact equality every one of those
        read as a missing hour. That was six cameras reported ~20 renders behind
        on a server that was not behind at all.
        """
        zone = self.config.render_timezone
        stored = starts_by_cadence.get(cadence.name, [])
        latest_period = cadence.floor(now.astimezone(zone))

        def is_rendered(period_start: datetime) -> bool:
            start = period_start.astimezone(timezone.utc)
            end = cadence.end_of(period_start).astimezone(timezone.utc)
            position = bisect_left(stored, start)
            return position < len(stored) and stored[position] < end

        if first_frame is None:
            # Nothing on the track this cadence reads, so no period it could have
            # rendered. A camera configured this morning is not a week behind on
            # weeklies, and a channel whose stills have all been pruned is not
            # behind on anything.
            return {"count": 0, "latest_period": None, "latest_rendered": None}

        if cadence.anchored:
            # An anchored render's start never moves, so it is judged on how far
            # it reaches rather than on where it begins: outstanding while
            # nothing stored already covers the period that just closed.
            reach = reach_by_cadence.get(cadence.name)
            rendered = reach is not None and reach >= latest_period.astimezone(timezone.utc)
            return {
                "count": 0 if rendered else 1,
                "latest_period": latest_period.astimezone(timezone.utc),
                "latest_rendered": rendered,
            }

        # Only closed periods can have been rendered; the one containing `now`
        # is still filling.
        period = cadence.previous_start(latest_period)
        horizon = self._render_horizon(now, cadence, first_frame)
        count = 0
        latest_rendered = None
        while period.astimezone(timezone.utc) >= horizon:
            present = is_rendered(period)
            if latest_rendered is None:
                latest_rendered = present
            if not present:
                count += 1
            period = cadence.previous_start(period)

        return {
            "count": count,
            "latest_period": cadence.previous_start(latest_period).astimezone(timezone.utc),
            "latest_rendered": latest_rendered,
        }

    def _render_horizon(self, now, cadence: Cadence, first_frame: datetime | None) -> datetime:
        """How far back a missing video is still worth counting.

        Bounded by whichever runs out first, exactly as the renderer bounds its
        own backfill: the frames this cadence reads, or the videos it keeps. A
        gap older than either is not a backlog, it is retention working -- and
        neither is a period from before the camera captured anything at all,
        which is why the oldest frame bounds it too.
        """
        bounds = [
            span
            for span in (
                self.config.retention_for_source(cadence.source),
                self.config.retention_for(cadence.name),
            )
            if span is not None
        ]
        horizon = now - (min(bounds) if bounds else cadence.window * 8)
        return max(horizon, first_frame) if first_frame is not None else horizon

    # --- retention ---

    def _retention(self, now, channels, scans, videos) -> dict:
        config = self.config

        def track_row(track: str, retention: timedelta | None) -> dict:
            oldest = min(
                (scans[channel][track].oldest for channel in channels
                 if scans[channel][track].oldest is not None),
                default=None,
            )
            age = (now - oldest).total_seconds() if oldest else None
            return {
                "track": track,
                "retention_seconds": _seconds(retention),
                "oldest": _iso(oldest),
                "oldest_age_seconds": age,
                # Above 1 means something older than retention is still on disk,
                # which is normal between hourly prunes and a problem if it
                # stays there.
                "used_fraction": (
                    round(age / retention.total_seconds(), 3)
                    if age is not None and retention else None
                ),
                "saturated": (
                    age >= retention.total_seconds() * 0.95
                    if age is not None and retention else None
                ),
            }

        cadence_rows = []
        for cadence in config.timelapse_cadences:
            of_cadence = [
                video for channel in channels for video in videos[channel]
                if video.cadence == cadence.name
            ]
            oldest = min((video.starts for video in of_cadence), default=None)
            retention = config.retention_for(cadence.name)
            cadence_rows.append({
                "cadence": cadence.name,
                "retention_seconds": _seconds(retention),
                "oldest": _iso(oldest),
                "oldest_age_seconds": (now - oldest).total_seconds() if oldest else None,
                "files": len(of_cadence),
            })

        return {
            "tracks": [
                track_row("image", config.image_retention),
                track_row("keyframe", config.keyframe_retention),
            ],
            "cadences": cadence_rows,
            "longest_image_cadence": (
                config.longest_image_cadence.name if config.longest_image_cadence else None
            ),
            "longest_image_cadence_seconds": config.longest_cadence_window.total_seconds(),
        }

    # --- growth ---

    def _growth(self, now, channels, scans, videos) -> dict:
        """What the library gained in the last day, and where that leads.

        Two numbers, because they answer different questions. The measured rate
        is what the last 24 hours actually wrote, and is what "days until the
        floor" is projected from. The steady state is where retention parks the
        library once the oldest frames start expiring at the rate new ones
        arrive, and is the number that says whether the disk is big enough at
        all -- a library still filling up has a measured rate that says nothing
        about where it stops.
        """
        config = self.config
        day = timedelta(days=1)

        measured = 0
        for channel in channels:
            measured += scans[channel]["image"].recent_bytes.get("day", 0)
            # Keyframes are hardlinks, so they add nothing until the still they
            # point at is pruned; counting them here would double the figure.
            measured += sum(
                video.bytes for video in videos[channel] if now - video.written_at <= day
            )

        # Steady state per channel: a full retention of stills, plus a full
        # keyframe retention at one frame a day, plus what the videos hold now.
        steady = 0
        for channel in channels:
            image = scans[channel]["image"]
            if not image.files:
                continue
            mean = image.bytes / image.files
            frames_per_day = 86400 / config.capture_interval.total_seconds()
            if config.image_retention is not None:
                steady += mean * frames_per_day * (config.image_retention / day)
            else:
                steady += image.bytes

            keyframe = scans[channel]["keyframe"]
            keyframe_mean = keyframe.bytes / keyframe.files if keyframe.files else mean
            if config.keyframe_retention is not None:
                steady += keyframe_mean * (config.keyframe_retention / day)
            else:
                # Kept forever, so the honest projection is a year of them --
                # ~500 MB for six channels, per the storage docs.
                steady += keyframe_mean * 365
            steady += sum(video.bytes for video in videos[channel])

        disk = self._disk()
        headroom = disk.get("headroom_bytes")
        # Judged on the stills alone. They are three orders of magnitude bigger
        # than everything else, so they decide when the library stops growing --
        # and a keyframe track kept forever never saturates, which would
        # otherwise make every configuration look like it was still filling.
        stills = next(
            row for row in self._retention(now, channels, scans, videos)["tracks"]
            if row["track"] == "image"
        )
        saturated = bool(stills["saturated"])

        days_left = None
        if headroom is not None and measured > 0 and not saturated:
            days_left = round(max(headroom, 0) / measured, 1)

        return {
            "measured_bytes_per_day": measured,
            "steady_state_bytes": round(steady),
            "saturated": saturated,
            "days_until_floor": days_left,
            "steady_state_fits": (
                (steady + config.minimum_free_bytes) <= disk["total_bytes"]
                if disk.get("available") else None
            ),
        }

    # --- analysis ---

    def _analysis(self, now, channels, scans, watermarks, recognition) -> dict:
        config = self.config
        if not config.analysis_enabled:
            return {"enabled": False, "reachable": False, "channels": []}
        if recognition is None:
            return {
                "enabled": True,
                "reachable": False,
                "index_path": str(config.analysis_index_path),
                "channels": [],
            }

        interval = config.capture_interval
        rows = []
        for channel in channels:
            image = scans[channel]["image"]
            watermark = watermarks.get(channel)
            analysed_through = (
                datetime.fromtimestamp(watermark, tz=timezone.utc) if watermark else None
            )
            # Against the newest frame on disk, not against the clock: analysis
            # can never be nearer than one capture interval to now, so measuring
            # from now would report a permanent lag that is not one.
            lag = (
                (image.newest - analysed_through).total_seconds()
                if analysed_through and image.newest else None
            )
            # The cutoff sits just past the watermark, so what it counted is
            # exactly what is left to do. With no watermark at all, everything is.
            backlog = image.recent_files["watermark"] if watermark else image.files

            rate = self.progress.rate(channel)
            eta = None
            if rate is not None and lag is not None and lag > interval.total_seconds():
                # The frontier moves at one second per second, so the backlog
                # only closes with whatever the analyzer does above that.
                gaining = rate - 1.0
                eta = lag / gaining if gaining > 0 else None

            state = "off"
            if channel not in config.channels:
                state = "retired"
            elif watermark is None:
                state = "unstarted"
            elif lag is None:
                state = "unknown"
            elif lag <= max(interval.total_seconds() * 3, 60):
                state = "current"
            elif rate is not None and rate < STOPPED_RATE:
                # Behind, and the watermark has not moved at all while this page
                # has been watching. That is a stopped analyzer, not a slow one,
                # and it wants a different sentence.
                state = "stopped"
            elif rate is not None and rate <= 1.0:
                state = "losing"
            else:
                state = "behind"

            rows.append({
                "channel": channel,
                "state": state,
                "analysed_through": _iso(analysed_through),
                "lag_seconds": lag,
                "backlog_frames": backlog,
                "rate": round(rate, 3) if rate is not None else None,
                "eta_seconds": round(eta) if eta is not None else None,
                "reads_plates": channel in config.analysis_plate_channels,
            })

        return {
            "enabled": True,
            "reachable": True,
            "index_path": str(config.analysis_index_path),
            "channels": rows,
            "counts": self._index_counts(recognition),
            "worst_lag_seconds": max(
                (row["lag_seconds"] for row in rows if row["lag_seconds"] is not None),
                default=None,
            ),
            "backlog_frames": sum(
                row["backlog_frames"] or 0 for row in rows
            ),
            "retention": {
                "detection_seconds": _seconds(config.analysis_detection_retention),
                "event_seconds": _seconds(config.analysis_event_retention),
            },
            "settings": {
                "score_threshold": config.analysis_score_threshold,
                "threads": config.analysis_threads,
                "batch_size": config.analysis_batch_size,
                "reid_enabled": config.analysis_reid_enabled,
                "reid_threshold": config.analysis_reid_threshold,
                "plate_channels": list(config.analysis_plate_channels),
                "plate_confidence": config.analysis_plate_confidence,
            },
        }

    # --- the archive ---

    def _archive(self, now, channels, recognition) -> dict:
        """The replica against the mirror: what is on disk, and what is not yet.

        Lag is the newest recording the mirror lists against the newest segment
        replicated -- large during the oldest-first backfill, minutes in steady
        state. Backlog compares counts, so it stays meaningful on a channel the
        backfill has not reached at all, where there is no lag to quote.
        """
        config = self.config
        if config.archive_root is None:
            return {"enabled": False, "channels": []}

        mirror: dict[str, dict] = {}
        if recognition is not None:
            try:
                mirror = recognition.segment_summary()
            except Exception:
                logger.exception("Could not read the footage mirror for the status page")

        rows = []
        total_files = total_bytes = 0
        for channel in channels:
            scan = scan_archive_channel(config.archive_root / channel)
            held = mirror.get(channel, {})
            newest_recorded = (
                datetime.fromtimestamp(held["newest"], tz=timezone.utc)
                if held.get("newest") else None
            )
            lag = (
                (newest_recorded - scan.newest_end).total_seconds()
                if newest_recorded and scan.newest_end else None
            )
            total_files += scan.files
            total_bytes += scan.bytes
            rows.append({
                "channel": channel,
                "files": scan.files,
                "bytes": scan.bytes,
                "oldest": _iso(scan.oldest_start),
                "newest": _iso(scan.newest_end),
                "recorded_segments": held.get("segments"),
                "recorded_bytes": held.get("bytes"),
                "lag_seconds": lag,
                "backlog_segments": (
                    max(held["segments"] - scan.files, 0) if held.get("segments") is not None else None
                ),
            })

        # The archive volume's own numbers: it is deliberately not the library's
        # filesystem, so the main disk tile says nothing about it.
        disk: dict = {"available": False}
        try:
            usage = shutil.disk_usage(config.archive_root)
            floor = config.archive_minimum_free_bytes
            disk = {
                "available": True,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "used_fraction": round(usage.used / usage.total, 4) if usage.total else None,
                "floor_met": usage.free >= floor if floor else None,
            }
        except OSError:
            pass

        return {
            "enabled": True,
            "root": str(config.archive_root),
            "retention_seconds": _seconds(config.archive_retention),
            "minimum_free_bytes": config.archive_minimum_free_bytes,
            "disk": disk,
            "channels": rows,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "worst_lag_seconds": max(
                (row["lag_seconds"] for row in rows if row["lag_seconds"] is not None),
                default=None,
            ),
            "backlog_segments": sum(
                row["backlog_segments"] or 0 for row in rows
            ),
        }

    @staticmethod
    def _index_counts(recognition) -> dict:
        try:
            return recognition.table_counts()
        except Exception:
            logger.exception("Could not count the analysis index for the status page")
            return {}

    # --- config and checks ---

    def _config_summary(self) -> dict:
        config = self.config
        return {
            "channels": list(config.channels),
            "capture_interval_seconds": config.capture_interval.total_seconds(),
            "resolution": f"{config.capture_resolution.width}x{config.capture_resolution.height}",
            "library_root": str(config.image_capture_library_root),
            "render_timezone": str(config.render_timezone),
            "video_duration_seconds": config.timelapse_video_duration.total_seconds(),
            "max_concurrent_renders": config.max_concurrent_renders,
            "deflicker_keyframe_renders": config.deflicker_keyframe_renders,
            "keyframe_at": config.keyframe_at.isoformat(),
            "keyframe_tolerance_seconds": config.keyframe_tolerance.total_seconds(),
            "web": f"{config.web_host}:{config.web_port}",
            "cadences": [
                {
                    "name": cadence.name,
                    "window_seconds": cadence.window.total_seconds(),
                    "source": cadence.source,
                    "anchored": cadence.anchored,
                    "fps": config.output_fps_for(cadence.name),
                    "min_frames": config.min_frames_for(cadence.name),
                    "retention_seconds": _seconds(config.retention_for(cadence.name)),
                }
                for cadence in config.timelapse_cadences
            ],
        }

    def _checks(self, report: dict) -> list[dict]:
        """Everything wrong, worst first, as one list the page can render flat.

        The configuration half is `validate_config`, unchanged -- the same
        warnings the daemon prints at startup, which are easy to miss in a
        journal and impossible to miss here.
        """
        checks: list[dict] = []

        def add(level: str, title: str, detail: str) -> None:
            checks.append({"level": level, "title": title, "detail": detail})

        disk = report["disk"]
        if disk.get("available"):
            if disk["floor_met"] is False:
                add("error", "Free space is below the floor",
                    f"{disk['free_bytes'] / 1e9:.1f} GB free against a "
                    f"{disk['minimum_free_bytes'] / 1e9:.1f} GB floor. The library is "
                    f"deleting past retention to stay writable.")
            elif disk["used_fraction"] and disk["used_fraction"] > 0.9:
                add("warn", "The disk is over 90% full",
                    f"{disk['free_bytes'] / 1e9:.1f} GB free of "
                    f"{disk['total_bytes'] / 1e9:.1f} GB.")

        growth = report["growth"]
        if growth["steady_state_fits"] is False:
            add("warn", "Retention does not fit the disk",
                f"At the current frame size this configuration settles at "
                f"{growth['steady_state_bytes'] / 1e9:.0f} GB, which does not leave the "
                f"{self.config.minimum_free_bytes / 1e9:.1f} GB floor free. Shorten "
                f"image_retention_days or lengthen the capture interval.")
        if growth["days_until_floor"] is not None and growth["days_until_floor"] < 3:
            add("warn", "Free space runs out within days",
                f"Writing {growth['measured_bytes_per_day'] / 1e9:.1f} GB a day leaves about "
                f"{growth['days_until_floor']} days before the floor.")

        for row in report["retention"]["tracks"]:
            # Pruning runs hourly, so being a little past retention is the normal
            # state between passes. Well past it means the prune is not running.
            if row["used_fraction"] is not None and row["used_fraction"] > 1.15:
                add("warn", f"{row['track']} files are outliving their retention",
                    f"The oldest is {_humanise(row['oldest_age_seconds'])} old against a "
                    f"{_humanise(row['retention_seconds'])} retention. Pruning runs hourly "
                    f"inside the capture daemon; check that it is running.")

        for row in report["capture"]["channels"]:
            if row["state"] == "stale":
                add("error", f"Camera {row['channel']} has stopped writing",
                    f"Last frame {_humanise(row['last_frame_age_seconds'])} ago, against a "
                    f"{report['capture']['interval_seconds']:.0f}s interval.")
            elif row["state"] == "silent" and row["configured"]:
                add("warn", f"Camera {row['channel']} has never written a frame",
                    "It is in the config but has no stills on disk.")
            elif row["state"] == "retired":
                add("info", f"Channel {row['channel']} is not in the config",
                    f"It still holds {row['frames']} stills. Nothing prunes a channel the "
                    f"daemon no longer captures.")
            elif row["yield"].get("hour", 1) < 0.75 and row["state"] == "live":
                add("warn", f"Camera {row['channel']} is dropping frames",
                    f"{row['frames_recent'].get('hour', 0)} frames in the last hour against "
                    f"{row['expected_recent'].get('hour', 0):.0f} expected.")

        for row in report["renders"]["channels"]:
            for entry in row["cadences"]:
                if entry["missing_periods"] >= 3:
                    add("warn", f"{entry['cadence']} renders are behind on camera {row['channel']}",
                        f"{entry['missing_periods']} closed periods within retention have no video.")

        analysis = report["analysis"]
        if analysis["enabled"] and not analysis["reachable"]:
            add("warn", "Recognition is enabled but its index is not readable",
                f"{analysis.get('index_path')} does not exist yet. The analyzer creates it on "
                f"its first run.")
        for row in analysis.get("channels", []):
            if row["state"] == "stopped":
                add("error", f"Analysis has stopped on camera {row['channel']}",
                    f"The watermark has not moved while this page has been open, and it "
                    f"is {_humanise(row['lag_seconds'])} behind. Check "
                    f"`systemctl status timelapsed-analyzer`.")
            elif row["state"] == "losing":
                add("error", f"Analysis is losing ground on camera {row['channel']}",
                    f"{_humanise(row['lag_seconds'])} behind and moving at "
                    f"{row['rate']:.2f}x real time, which is slower than the cameras write.")
            elif row["state"] == "behind" and (row["lag_seconds"] or 0) > 6 * 3600:
                add("warn", f"Analysis is well behind on camera {row['channel']}",
                    f"{_humanise(row['lag_seconds'])} behind"
                    + (f", catching up in about {_humanise(row['eta_seconds'])}."
                       if row["eta_seconds"] else "."))
            elif row["state"] == "unstarted" and row["channel"] in self.config.channels:
                add("info", f"Analysis has not started on camera {row['channel']}",
                    "No watermark has been written for it yet.")

        archive = report["archive"]
        if archive["enabled"]:
            archive_disk = archive["disk"]
            if archive_disk.get("available") and archive_disk.get("floor_met") is False:
                add("warn", "The archive volume is below its free-space floor",
                    f"{archive_disk['free_bytes'] / 1e9:.1f} GB free against a "
                    f"{archive['minimum_free_bytes'] / 1e9:.1f} GB floor. The archiver is "
                    f"dropping its oldest days to stay writable, which is retention by "
                    f"disk size doing its job.")
            if not archive["total_files"]:
                add("info", "The archive is empty",
                    "The archiver has not replicated anything yet. Its first pass pulls "
                    "the NVR's whole history, oldest first.")
            elif archive["worst_lag_seconds"] is not None and archive["worst_lag_seconds"] > 86400:
                add("warn", "The archive is behind the NVR",
                    f"The newest replicated segment trails the newest recording by "
                    f"{_humanise(archive['worst_lag_seconds'])}. Normal while the "
                    f"oldest-first backfill runs; if it persists, check "
                    f"`systemctl status timelapsed-archiver`.")

        services = report["services"] or {}
        if services.get("unavailable"):
            add("info", "systemd could not be asked about the services",
                f"{services['unavailable']} Everything else on this page is unaffected.")
        for unit, detail in services.items():
            if unit == "unavailable":
                continue
            if detail["active"] != "active":
                add("error", f"{unit} is {detail['active']}",
                    f"systemd reports sub-state {detail['sub']}, last result {detail['result']}.")
            elif detail["restarts"]:
                add("warn", f"{unit} has restarted {detail['restarts']} time(s)",
                    "Repeated restarts usually mean a crash loop; check the journal.")

        for warning in validate_config(self.config):
            add("warn", "Configuration", warning)

        order = {"error": 0, "warn": 1, "info": 2}
        checks.sort(key=lambda check: order.get(check["level"], 3))
        return checks


def _humanise(seconds: float | None) -> str:
    """A duration in the largest unit that keeps it readable."""
    if seconds is None:
        return "unknown"
    seconds = abs(float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def status_json(report: dict) -> bytes:
    return json.dumps(report, separators=(",", ":")).encode()
