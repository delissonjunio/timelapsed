import configparser
import logging
from datetime import timedelta
from pathlib import Path

from timelapsed.schema import CADENCES, Cadence, Config, VideoResolution

CONFIG_PATHS = ("/etc/timelapsed.ini", "~/.timelapsed.ini", "./timelapsed.ini")

DEFAULT_CADENCES = "hourly,daily,weekly"
DEFAULT_IMAGE_RETENTION_DAYS = 8

logger = logging.getLogger(__name__)


def _optional_days(parser: configparser.ConfigParser, section: str, option: str, fallback: int) -> timedelta | None:
    """Read a retention setting in days. 0 (or negative) means keep forever."""
    days = parser.getint(section, option, fallback=fallback)
    return timedelta(days=days) if days > 0 else None


def _parse_cadences(raw: str) -> list[Cadence]:
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    unknown = [name for name in names if name not in CADENCES]
    if unknown:
        raise ValueError(
            f"Unknown timelapse cadence(s): {', '.join(unknown)}. "
            f"Valid values are: {', '.join(CADENCES)}"
        )
    if not names:
        raise ValueError("At least one timelapse cadence must be enabled")

    # Sorted shortest window first so logs and renders run in a predictable order.
    return sorted((CADENCES[name] for name in dict.fromkeys(names)), key=lambda c: c.window)


def validate_config(config: Config) -> list[str]:
    """Return human-readable warnings about settings that will not work as intended."""
    warnings = []

    longest_window = config.longest_cadence_window
    if config.image_retention is not None and config.image_retention <= longest_window:
        longest_cadence = max(config.timelapse_cadences, key=lambda c: c.window)
        warnings.append(
            f"image_retention_days ({config.image_retention.days}) is not greater than the "
            f"{longest_cadence.name} cadence window ({longest_window.days} days). Images will be "
            f"pruned before the {longest_cadence.name} render can use them. Set "
            f"image_retention_days to at least {longest_window.days + 1}."
        )

    frames_per_window = longest_window.total_seconds() / config.capture_interval.total_seconds()
    if frames_per_window < config.timelapse_min_frames:
        warnings.append(
            f"A capture interval of {config.capture_interval.total_seconds():.0f}s yields only "
            f"{frames_per_window:.0f} frames per {longest_window.days} days, below min_frames "
            f"({config.timelapse_min_frames}). Renders will be skipped."
        )

    return warnings


def get_config(config_paths: tuple[str, ...] = CONFIG_PATHS) -> Config:
    parser = configparser.ConfigParser()

    read_paths = [Path(path).expanduser() for path in config_paths]
    if not parser.read(read_paths):
        raise FileNotFoundError(
            "No timelapsed config found. Looked in: " + ", ".join(str(p) for p in read_paths)
        )

    return Config(
        nvr_url=parser["nvr"]["url"].rstrip("/"),
        nvr_username=parser["nvr"]["username"],
        nvr_password=parser["nvr"]["password"],
        channels=[channel.strip() for channel in parser["nvr"]["channels"].split(",") if channel.strip()],

        capture_interval=timedelta(seconds=parser.getint("capture", "interval_seconds")),
        capture_resolution=VideoResolution(
            width=parser.getint("capture", "resolution.width"),
            height=parser.getint("capture", "resolution.height"),
        ),

        timelapse_video_duration=timedelta(seconds=parser.getint("timelapse", "duration_seconds")),
        timelapse_output_fps=parser.getint("timelapse", "output_fps", fallback=30),
        timelapse_min_frames=parser.getint("timelapse", "min_frames", fallback=60),
        timelapse_cadences=_parse_cadences(parser.get("timelapse", "cadences", fallback=DEFAULT_CADENCES)),

        image_capture_library_root=Path(parser["image_capture_library"]["root"]).expanduser(),
        image_retention=_optional_days(
            parser, "image_capture_library", "image_retention_days", fallback=DEFAULT_IMAGE_RETENTION_DAYS
        ),
        timelapse_retention=_optional_days(
            parser, "image_capture_library", "timelapse_retention_days", fallback=0
        ),

        web_host=parser.get("web", "host", fallback="0.0.0.0"),
        web_port=parser.getint("web", "port", fallback=8080),

        logging_level=logging.getLevelName(parser.get("general", "logging_level", fallback="INFO").upper()),
    )
