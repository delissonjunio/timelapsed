from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class VideoResolution:
    width: int
    height: int


@dataclass(frozen=True)
class Cadence:
    """A recurring timelapse: how far back it looks, and when it is due.

    `is_due` compares the current time against the last time this cadence ran and
    answers whether the clock has rolled over into a new period. It reads whatever
    wall clock the datetimes it is handed carry, so passing them in the configured
    `render_timezone` is what makes "daily" mean a local day rather than a UTC one.
    """

    name: str
    window: timedelta
    is_due: Callable[[datetime, datetime], bool]
    # Snaps a timestamp back to the start of the period containing it. Renders
    # are looked up by period, so a window has to have one canonical name no
    # matter what second of it the render actually fired on.
    floor: Callable[[datetime], datetime]


def _hour_rolled_over(now: datetime, last_run: datetime) -> bool:
    return (now.date(), now.hour) != (last_run.date(), last_run.hour)


def _day_rolled_over(now: datetime, last_run: datetime) -> bool:
    return now.date() != last_run.date()


def _week_rolled_over(now: datetime, last_run: datetime) -> bool:
    # isocalendar()[:2] is (ISO year, ISO week), so this rolls over on Monday
    # and stays correct across a year boundary.
    return now.isocalendar()[:2] != last_run.isocalendar()[:2]


def _floor_to_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _floor_to_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _floor_to_week(moment: datetime) -> datetime:
    # weekday() is 0 on Monday, which is where the ISO week starts and so where
    # _week_rolled_over fires.
    return _floor_to_day(moment) - timedelta(days=moment.weekday())


CADENCES: dict[str, Cadence] = {
    "hourly": Cadence("hourly", timedelta(hours=1), _hour_rolled_over, _floor_to_hour),
    "daily": Cadence("daily", timedelta(days=1), _day_rolled_over, _floor_to_day),
    "weekly": Cadence("weekly", timedelta(days=7), _week_rolled_over, _floor_to_week),
}


@dataclass
class Config:
    nvr_url: str
    nvr_username: str
    nvr_password: str

    channels: list[str]

    capture_interval: timedelta
    capture_resolution: VideoResolution

    timelapse_video_duration: timedelta
    timelapse_output_fps: int
    timelapse_min_frames: int
    timelapse_cadences: list[Cadence]
    # Wall clock the cadence rollovers are judged against. A render is named for
    # the period it covers, so this decides whether "daily" closes at midnight
    # UTC or at midnight where the cameras actually are.
    render_timezone: tzinfo
    # How many renders may run at once across every channel. ffmpeg is the
    # memory-hungry part of this daemon, so this is the guest's RAM budget
    # expressed as a process count.
    max_concurrent_renders: int

    image_capture_library_root: Path
    image_retention: timedelta | None
    # Keyed by cadence name: hourly videos are numerous and disposable, weekly
    # ones are the archive, so they do not share a retention.
    timelapse_retention: dict[str, timedelta | None]
    # Hard floor on free space. Retention bounds age, not bytes, so this is what
    # actually keeps the daemon writing when the steady state moves. 0 disables.
    minimum_free_bytes: int

    web_host: str
    web_port: int

    # Recognition. Runs in its own daemon over the stills capture already wrote,
    # so none of this affects the capture loop's timing.
    analysis_enabled: bool
    analysis_index_path: Path
    analysis_crop_root: Path
    analysis_model_root: Path
    # Below 0.5 the detector reports scenery: a neighbouring building read as a
    # vehicle on 70% of night frames, a pile of tools as a car indoors. Measured
    # in docs/Recognition-Feasibility.md; 0.5 removed every one of them.
    analysis_score_threshold: float
    analysis_threads: int
    analysis_batch_size: int
    analysis_detection_retention: timedelta | None
    analysis_event_retention: timedelta | None
    # Body-appearance matching, not face recognition -- faces are too small on
    # this footage to identify. 0.8 is chosen for precision: it misses most
    # repeat sightings rather than merging two different people.
    analysis_reid_enabled: bool
    analysis_reid_threshold: float
    analysis_reid_window: timedelta
    # Plate reading only pays off where plates are big enough to read, which is
    # one channel here. Empty disables it entirely.
    analysis_plate_channels: list[str]
    analysis_plate_confidence: float

    logging_level: int

    @property
    def longest_cadence_window(self) -> timedelta:
        """The furthest back any enabled render reaches, and so the minimum useful retention."""
        return max((cadence.window for cadence in self.timelapse_cadences), default=timedelta(0))

    def retention_for(self, cadence_name: str) -> timedelta | None:
        """How long to keep this cadence's videos. None means forever."""
        return self.timelapse_retention.get(cadence_name)
