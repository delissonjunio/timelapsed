from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Callable, Literal

# Which track of frames a render reads. Stills are pruned in days; keyframes --
# one promoted still per local day -- are kept for years, which is the only way a
# monthly render is affordable. See `Cadence.source`.
SourceTrack = Literal["image", "keyframe"]


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
    # How far back the render reaches. For the calendar cadences this is
    # *nominal* -- the longest the period can be -- and is only ever read to order
    # the cadences and to bound retention. The arithmetic goes through
    # `previous_start` and `end_of`, which know that a month is 28 to 31 days.
    window: timedelta
    is_due: Callable[[datetime, datetime], bool]
    # Snaps a timestamp back to the start of the period containing it. Renders
    # are looked up by period, so a window has to have one canonical name no
    # matter what second of it the render actually fired on.
    floor: Callable[[datetime], datetime]

    # Which frames feed this render. Not configurable: an 8-day image retention
    # cannot physically feed a monthly video, and the 32 days of stills that
    # could would be ~380 GB for six channels on a 200 GB disk.
    source: SourceTrack = "image"

    # Calendar stepping. None means the period is exactly `window` long, which is
    # true of an hour, a day and a week, and false of a month.
    step_back: Callable[[datetime], datetime] | None = None
    step_forward: Callable[[datetime], datetime] | None = None

    # A cumulative render: the start is pinned to the oldest frame there is
    # rather than floored to a period, so there is only ever one candidate window
    # and whether it is outstanding is decided on the end. See
    # `pending_render_windows`.
    anchored: bool = False

    def previous_start(self, period_start: datetime) -> datetime:
        """Where the period before this one starts."""
        if self.step_back is not None:
            return self.step_back(period_start)
        return period_start - self.window

    def end_of(self, period_start: datetime) -> datetime:
        """Where the period starting here closes."""
        if self.step_forward is not None:
            return self.step_forward(period_start)
        return period_start + self.window


def _hour_rolled_over(now: datetime, last_run: datetime) -> bool:
    return (now.date(), now.hour) != (last_run.date(), last_run.hour)


def _day_rolled_over(now: datetime, last_run: datetime) -> bool:
    return now.date() != last_run.date()


def _week_rolled_over(now: datetime, last_run: datetime) -> bool:
    # isocalendar()[:2] is (ISO year, ISO week), so this rolls over on Monday
    # and stays correct across a year boundary.
    return now.isocalendar()[:2] != last_run.isocalendar()[:2]


def _month_rolled_over(now: datetime, last_run: datetime) -> bool:
    return (now.year, now.month) != (last_run.year, last_run.month)


def _floor_to_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _floor_to_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _floor_to_week(moment: datetime) -> datetime:
    # weekday() is 0 on Monday, which is where the ISO week starts and so where
    # _week_rolled_over fires.
    return _floor_to_day(moment) - timedelta(days=moment.weekday())


def _floor_to_month(moment: datetime) -> datetime:
    return _floor_to_day(moment).replace(day=1)


def _previous_month(period_start: datetime) -> datetime:
    """The first of the month before the one `period_start` opens.

    Takes a value already floored by `_floor_to_month`, so stepping back a single
    day always lands in the previous month whatever its length.
    """
    return (period_start - timedelta(days=1)).replace(day=1)


def _next_month(period_start: datetime) -> datetime:
    """The first of the month after the one `period_start` opens.

    28 is the only day every month has, so adding four days to it always lands in
    the next month and never skips one -- which `+ timedelta(days=31)` would do
    in February.
    """
    return (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)


CADENCES: dict[str, Cadence] = {
    "hourly": Cadence("hourly", timedelta(hours=1), _hour_rolled_over, _floor_to_hour),
    "daily": Cadence("daily", timedelta(days=1), _day_rolled_over, _floor_to_day),
    "weekly": Cadence("weekly", timedelta(days=7), _week_rolled_over, _floor_to_week),
    # One frame per day across a calendar month: the construction-progress view.
    # Reads the keyframe track, because a month of stills does not fit on the disk.
    "monthly": Cadence(
        "monthly", timedelta(days=31), _month_rolled_over, _floor_to_month,
        source="keyframe", step_back=_previous_month, step_forward=_next_month,
    ),
    # Every keyframe ever captured, in one video, refreshed on the 1st. Its end is
    # floored to a day rather than a month so a project that started on the 10th
    # has something to watch that week instead of waiting for the rollover.
    "progress": Cadence(
        "progress", timedelta(days=31), _month_rolled_over, _floor_to_day,
        source="keyframe", anchored=True,
    ),
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
    # Per-cadence overrides of the two above, keyed by cadence name. A month holds
    # at most 31 frames, so the baselines -- tuned for a window holding thousands
    # -- would refuse to render one and would play it in a single second.
    timelapse_output_fps_by_cadence: dict[str, int]
    timelapse_min_frames_by_cadence: dict[str, int]
    timelapse_cadences: list[Cadence]
    # Wall clock the cadence rollovers are judged against. A render is named for
    # the period it covers, so this decides whether "daily" closes at midnight
    # UTC or at midnight where the cameras actually are.
    render_timezone: tzinfo
    # Even out the camera's auto-exposure between frames a day apart. Only ever
    # applied to keyframe-sourced renders: in an hourly, frames are seconds apart
    # and the changing light *is* the content.
    deflicker_keyframe_renders: bool
    # How many renders may run at once across every channel. ffmpeg is the
    # memory-hungry part of this daemon, so this is the guest's RAM budget
    # expressed as a process count.
    max_concurrent_renders: int

    image_capture_library_root: Path
    image_retention: timedelta | None
    # Local wall-clock time of the daily keyframe, and how far from it a stored
    # still may be and still count as that day's frame. A constant sun angle is
    # the whole point of a construction timelapse, so this is a time of day rather
    # than an interval.
    keyframe_at: time
    keyframe_tolerance: timedelta
    keyframe_retention: timedelta | None
    # Keyed by cadence name: hourly videos are numerous and disposable, weekly
    # ones are the archive, so they do not share a retention.
    timelapse_retention: dict[str, timedelta | None]
    # Hard floor on free space. Retention bounds age, not bytes, so this is what
    # actually keeps the daemon writing when the steady state moves. 0 disables.
    minimum_free_bytes: int

    # Full-segment replica of the NVR's own recordings, kept by the archiver
    # daemon. None disables it; on the deployed guest the root is its own
    # volume, so its retention and free-space floor are its own numbers too.
    archive_root: Path | None
    archive_retention: timedelta | None
    archive_minimum_free_bytes: int

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
    # Online matching fragments one person into many; this is the pass that
    # puts them back together. See IdentityMatcher.consolidate.
    analysis_reid_merge_threshold: float
    analysis_reid_window: timedelta
    # Plate reading only pays off where plates are big enough to read, which is
    # one channel here. Empty disables it entirely.
    analysis_plate_channels: list[str]
    analysis_plate_confidence: float

    logging_level: int

    @property
    def longest_image_cadence(self) -> Cadence | None:
        """The still-reading render that reaches furthest back, or None if there is none.

        Keyframe-sourced cadences are excluded deliberately. They do not read the
        still library at all, so they neither constrain `image_retention` nor earn
        any of its frames protection from the free-space reclaim.
        """
        image_cadences = [cadence for cadence in self.timelapse_cadences if cadence.source == "image"]
        if not image_cadences:
            return None
        return max(image_cadences, key=lambda cadence: cadence.window)

    @property
    def longest_cadence_window(self) -> timedelta:
        """The furthest back any still-reading render reaches, and so the minimum useful retention."""
        cadence = self.longest_image_cadence
        return cadence.window if cadence is not None else timedelta(0)

    def retention_for(self, cadence_name: str) -> timedelta | None:
        """How long to keep this cadence's videos. None means forever."""
        return self.timelapse_retention.get(cadence_name)

    def retention_for_source(self, source: SourceTrack) -> timedelta | None:
        """How long the track feeding a cadence keeps its frames. None means forever."""
        return self.image_retention if source == "image" else self.keyframe_retention

    def min_frames_for(self, cadence_name: str) -> int:
        """Fewest frames worth rendering for this cadence."""
        return self.timelapse_min_frames_by_cadence.get(cadence_name, self.timelapse_min_frames)

    def output_fps_for(self, cadence_name: str) -> int:
        """Playback frame rate for this cadence."""
        return self.timelapse_output_fps_by_cadence.get(cadence_name, self.timelapse_output_fps)
