from dataclasses import dataclass
from datetime import datetime, timedelta
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
    answers whether the clock has rolled over into a new period.
    """

    name: str
    window: timedelta
    is_due: Callable[[datetime, datetime], bool]


def _hour_rolled_over(now: datetime, last_run: datetime) -> bool:
    return (now.date(), now.hour) != (last_run.date(), last_run.hour)


def _day_rolled_over(now: datetime, last_run: datetime) -> bool:
    return now.date() != last_run.date()


def _week_rolled_over(now: datetime, last_run: datetime) -> bool:
    # isocalendar()[:2] is (ISO year, ISO week), so this rolls over on Monday
    # and stays correct across a year boundary.
    return now.isocalendar()[:2] != last_run.isocalendar()[:2]


CADENCES: dict[str, Cadence] = {
    "hourly": Cadence("hourly", timedelta(hours=1), _hour_rolled_over),
    "daily": Cadence("daily", timedelta(days=1), _day_rolled_over),
    "weekly": Cadence("weekly", timedelta(days=7), _week_rolled_over),
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

    image_capture_library_root: Path
    image_retention: timedelta | None
    # Keyed by cadence name: hourly videos are numerous and disposable, weekly
    # ones are the archive, so they do not share a retention.
    timelapse_retention: dict[str, timedelta | None]

    web_host: str
    web_port: int

    logging_level: int

    @property
    def longest_cadence_window(self) -> timedelta:
        """The furthest back any enabled render reaches, and so the minimum useful retention."""
        return max((cadence.window for cadence in self.timelapse_cadences), default=timedelta(0))

    def retention_for(self, cadence_name: str) -> timedelta | None:
        """How long to keep this cadence's videos. None means forever."""
        return self.timelapse_retention.get(cadence_name)
