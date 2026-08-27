import configparser
import logging
from datetime import timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timelapsed.schema import CADENCES, Cadence, Config, VideoResolution

CONFIG_PATHS = ("/etc/timelapsed.ini", "~/.timelapsed.ini", "./timelapsed.ini")

DEFAULT_CADENCES = "hourly,daily,weekly"
# UTC keeps rollovers deterministic and DST-free. Set a real zone when the
# videos are for people, who expect a "daily" to start at their own midnight.
DEFAULT_TIMEZONE = "UTC"
DEFAULT_IMAGE_RETENTION_DAYS = 8
# Timelapse footage compresses badly, so a full 60-second render is ~140 MB and
# daily is the expensive cadence. Bound hourly and daily, keep weekly forever.
# 0 means keep forever.
DEFAULT_TIMELAPSE_RETENTION_DAYS = {"hourly": 7, "daily": 90, "weekly": 0}
# Enough headroom for render scratch space, the journal and an apt upgrade.
DEFAULT_MINIMUM_FREE_DISK_GB = 5.0
# One ffmpeg at a time. Each 1080p render peaks around 250 MB, so letting every
# channel render its rollover at once is how a small guest meets the OOM killer.
DEFAULT_MAX_CONCURRENT_RENDERS = 1

# Recognition defaults. The thresholds are measured, not guessed; the workings
# are in docs/Recognition-Feasibility.md.
DEFAULT_ANALYSIS_SCORE_THRESHOLD = 0.5
DEFAULT_ANALYSIS_THREADS = 2
# Frames per channel per pass. Bounded so one busy channel cannot starve the
# others during a backfill.
DEFAULT_ANALYSIS_BATCH_SIZE = 200
DEFAULT_ANALYSIS_DETECTION_RETENTION_DAYS = 30
DEFAULT_ANALYSIS_EVENT_RETENTION_DAYS = 365
DEFAULT_ANALYSIS_REID_THRESHOLD = 0.8
# Consolidation is what makes re-ID usable: matching alone fragmented one
# person across a day into 156 identities on real footage.
DEFAULT_ANALYSIS_REID_MERGE_THRESHOLD = 0.75
DEFAULT_ANALYSIS_REID_WINDOW_HOURS = 12
DEFAULT_ANALYSIS_PLATE_CONFIDENCE = 0.7

logger = logging.getLogger(__name__)


def _optional_days(parser: configparser.ConfigParser, section: str, option: str, fallback: int) -> timedelta | None:
    """Read a retention setting in days. 0 (or negative) means keep forever."""
    days = parser.getint(section, option, fallback=fallback)
    return timedelta(days=days) if days > 0 else None


def _parse_timelapse_retention(
    parser: configparser.ConfigParser, cadences: list[Cadence]
) -> dict[str, timedelta | None]:
    """Per-cadence video retention.

    `timelapse_retention_days` sets the baseline for every cadence;
    `timelapse_retention_days.<cadence>` overrides one of them. Absent both, each
    cadence falls back to its own built-in default.
    """
    section = "image_capture_library"
    baseline = parser.getint(section, "timelapse_retention_days", fallback=None)

    retention: dict[str, timedelta | None] = {}
    for cadence in cadences:
        fallback = baseline if baseline is not None else DEFAULT_TIMELAPSE_RETENTION_DAYS.get(cadence.name, 0)
        days = parser.getint(section, f"timelapse_retention_days.{cadence.name}", fallback=fallback)
        retention[cadence.name] = timedelta(days=days) if days > 0 else None
    return retention


def _parse_timezone(raw: str) -> tzinfo:
    """The wall clock cadence rollovers are judged against, as an IANA zone name."""
    name = raw.strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(
            f"Unknown timelapse timezone: {name!r}. Use an IANA zone name such as "
            f"'UTC' or 'America/Sao_Paulo'."
        ) from error


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

    if config.minimum_free_bytes <= 0:
        warnings.append(
            "min_free_disk_gb is 0, so nothing stops the library from filling the disk if "
            "retention turns out to be too generous for this channel count. Set it to a few GB."
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

    cadences = _parse_cadences(parser.get("timelapse", "cadences", fallback=DEFAULT_CADENCES))

    library_root = Path(parser["image_capture_library"]["root"]).expanduser()
    analysis_root = Path(
        parser.get("analysis", "root", fallback=str(library_root / "index"))
    ).expanduser()

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
        timelapse_cadences=cadences,
        render_timezone=_parse_timezone(parser.get("timelapse", "timezone", fallback=DEFAULT_TIMEZONE)),
        max_concurrent_renders=max(
            1, parser.getint("timelapse", "max_concurrent_renders", fallback=DEFAULT_MAX_CONCURRENT_RENDERS)
        ),

        image_capture_library_root=library_root,
        image_retention=_optional_days(
            parser, "image_capture_library", "image_retention_days", fallback=DEFAULT_IMAGE_RETENTION_DAYS
        ),
        timelapse_retention=_parse_timelapse_retention(parser, cadences),
        minimum_free_bytes=int(
            max(0.0, parser.getfloat(
                "image_capture_library", "min_free_disk_gb", fallback=DEFAULT_MINIMUM_FREE_DISK_GB
            )) * 1_000_000_000
        ),

        web_host=parser.get("web", "host", fallback="0.0.0.0"),
        web_port=parser.getint("web", "port", fallback=8080),

        analysis_enabled=parser.getboolean("analysis", "enabled", fallback=False),
        # Everything recognition writes lives under one directory, deliberately
        # outside the per-channel image and timelapse trees so the library's own
        # pruning and reclaim never walk it.
        analysis_index_path=analysis_root / "index.sqlite3",
        analysis_crop_root=analysis_root / "crops",
        analysis_model_root=Path(
            parser.get("analysis", "model_root", fallback=str(analysis_root / "models"))
        ).expanduser(),
        analysis_score_threshold=parser.getfloat(
            "analysis", "score_threshold", fallback=DEFAULT_ANALYSIS_SCORE_THRESHOLD
        ),
        analysis_threads=max(1, parser.getint(
            "analysis", "threads", fallback=DEFAULT_ANALYSIS_THREADS
        )),
        analysis_batch_size=max(1, parser.getint(
            "analysis", "batch_size", fallback=DEFAULT_ANALYSIS_BATCH_SIZE
        )),
        analysis_detection_retention=_optional_days(
            parser, "analysis", "detection_retention_days",
            fallback=DEFAULT_ANALYSIS_DETECTION_RETENTION_DAYS,
        ),
        analysis_event_retention=_optional_days(
            parser, "analysis", "event_retention_days",
            fallback=DEFAULT_ANALYSIS_EVENT_RETENTION_DAYS,
        ),
        analysis_reid_enabled=parser.getboolean("analysis", "reid_enabled", fallback=True),
        analysis_reid_threshold=parser.getfloat(
            "analysis", "reid_threshold", fallback=DEFAULT_ANALYSIS_REID_THRESHOLD
        ),
        analysis_reid_merge_threshold=parser.getfloat(
            "analysis", "reid_merge_threshold", fallback=DEFAULT_ANALYSIS_REID_MERGE_THRESHOLD
        ),
        analysis_reid_window=timedelta(hours=parser.getint(
            "analysis", "reid_window_hours", fallback=DEFAULT_ANALYSIS_REID_WINDOW_HOURS
        )),
        analysis_plate_channels=[
            channel.strip()
            for channel in parser.get("analysis", "plate_channels", fallback="").split(",")
            if channel.strip()
        ],
        analysis_plate_confidence=parser.getfloat(
            "analysis", "plate_confidence", fallback=DEFAULT_ANALYSIS_PLATE_CONFIDENCE
        ),

        logging_level=logging.getLevelName(parser.get("general", "logging_level", fallback="INFO").upper()),
    )
