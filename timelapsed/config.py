import configparser
import logging
import re
from datetime import datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timelapsed.schema import CADENCES, NVR_KINDS, Cadence, Config, NVRConfig, VideoResolution

CONFIG_PATHS = ("/etc/timelapsed.ini", "~/.timelapsed.ini", "./timelapsed.ini")

DEFAULT_CADENCES = "hourly,daily,weekly"
# UTC keeps rollovers deterministic and DST-free. Set a real zone when the
# videos are for people, who expect a "daily" to start at their own midnight.
DEFAULT_TIMEZONE = "UTC"
DEFAULT_IMAGE_RETENTION_DAYS = 8
# Timelapse footage compresses badly, so a full 60-second render is ~140 MB and
# daily is the expensive cadence. Bound hourly and daily, keep weekly forever.
# The keyframe-sourced cadences are a couple of MB each and are the archive.
# 0 means keep forever.
DEFAULT_TIMELAPSE_RETENTION_DAYS = {"hourly": 7, "daily": 90, "weekly": 0, "monthly": 0, "progress": 0}
DEFAULT_OUTPUT_FPS = 30
DEFAULT_MIN_FRAMES = 60
# One frame per day is a different kind of video from one frame every ten
# seconds, and the baselines above are wrong for it in both directions: at 30 fps
# a 31-frame month plays in one second, and a 60-frame minimum would refuse to
# render a month that can never hold more than 31.
DEFAULT_OUTPUT_FPS_BY_CADENCE = {"monthly": 6, "progress": 6}
DEFAULT_MIN_FRAMES_BY_CADENCE = {"monthly": 5, "progress": 10}
# Local noon: the highest sun, the shortest shadows, and the least drift across
# the seasons. A construction timelapse lives or dies on a constant sun angle.
DEFAULT_KEYFRAME_AT = "12:00"
DEFAULT_KEYFRAME_TOLERANCE_MINUTES = 30
DEFAULT_KEYFRAME_RETENTION_DAYS = 0
DEFAULT_DEFLICKER = True
# Enough headroom for render scratch space, the journal and an apt upgrade.
DEFAULT_MINIMUM_FREE_DISK_GB = 5.0
# The archive's own floor, on its own volume. Generous because the reclaim that
# enforces it runs between fetches, and a day of footage is ~40 GB: the floor
# has to absorb most of a day arriving before the next reclaim pass.
DEFAULT_ARCHIVE_MIN_FREE_DISK_GB = 50.0
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

# February, at one frame per day. A keyframe cadence cannot hold more than this
# in its shortest period, so a minimum above it never renders.
SHORTEST_MONTH_FRAMES = 28
# Above this, a month of daily frames goes past in about a second.
FLIPBOOK_FPS = 24

logger = logging.getLogger(__name__)

DEFAULT_NVR_KIND = "hikvision"
# A name becomes a directory-name prefix, a URL segment and a go2rtc stream
# name, so it is held to characters safe in all three.
NVR_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*$")


def _parse_nvrs(parser: configparser.ConfigParser) -> list[NVRConfig]:
    """Every `[nvr]` / `[nvr.<name>]` section, the unnamed one first.

    The unnamed section's channels keep their bare numbers as global ids, so a
    single-NVR config parses to exactly what it always meant; a named section's
    channels are namespaced `<name>-<number>`. Collisions between the resulting
    ids are refused here, once, rather than discovered as crossed wires in the
    library, the index and the archive.
    """
    sections = [
        section for section in parser.sections()
        if section == "nvr" or section.startswith("nvr.")
    ]
    # File order, except the unnamed section always leads: it is the default
    # NVR, and being first is what makes it so.
    sections.sort(key=lambda section: section != "nvr")
    if not sections:
        raise ValueError("No [nvr] section found. At least one NVR must be configured.")

    nvrs: list[NVRConfig] = []
    seen_ids: set[str] = set()
    for section in sections:
        name = None if section == "nvr" else section[len("nvr."):]
        if name is not None and not NVR_NAME.match(name):
            raise ValueError(
                f"Bad NVR name {name!r} in [{section}]: use lowercase letters, digits, "
                f"'-' and '_', starting with a letter or digit."
            )
        kind = parser.get(section, "type", fallback=DEFAULT_NVR_KIND).strip().lower()
        if kind not in NVR_KINDS:
            raise ValueError(
                f"Unknown NVR type {kind!r} in [{section}]. Valid values: {', '.join(NVR_KINDS)}"
            )
        nvr = NVRConfig(
            name=name,
            kind=kind,
            url=parser.get(section, "url").rstrip("/"),
            username=parser.get(section, "username"),
            password=parser.get(section, "password"),
            device_channels=tuple(
                channel.strip()
                for channel in parser.get(section, "channels").split(",")
                if channel.strip()
            ),
        )
        for channel_id in nvr.channel_ids:
            if channel_id in seen_ids:
                raise ValueError(
                    f"Channel id {channel_id!r} appears under more than one NVR section."
                )
            seen_ids.add(channel_id)
        nvrs.append(nvr)
    return nvrs


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


def _parse_render_overrides(
    parser: configparser.ConfigParser,
    option: str,
    cadences: list[Cadence],
    baseline: int,
    per_cadence_defaults: dict[str, int],
) -> dict[str, int]:
    """Per-cadence overrides of a `[timelapse]` render setting, keyed by cadence name.

    `<option>.<cadence>` in the INI overrides one cadence. Absent that, a
    keyframe-sourced cadence falls back to its own built-in default rather than to
    the baseline. It holds one frame per day -- at most 31 in a month -- so a
    baseline tuned for a window holding thousands of stills is not a sensible
    thing for it to inherit, and the shipped `timelapsed.ini` writes that baseline
    out explicitly, so inheritance would silently break every monthly render.
    Still-sourced cadences do inherit the baseline.

    Only values that actually differ from the baseline are stored, so the common
    case leaves the dict empty and `Config.min_frames_for` answers from the
    baseline.
    """
    overrides: dict[str, int] = {}
    for cadence in cadences:
        default = baseline if cadence.source == "image" else per_cadence_defaults.get(cadence.name, baseline)
        value = parser.getint("timelapse", f"{option}.{cadence.name}", fallback=default)
        if value != baseline:
            overrides[cadence.name] = value
    return overrides


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


def _parse_time_of_day(raw: str) -> time:
    """The local wall-clock time the daily keyframe is taken at."""
    text = raw.strip()
    try:
        return datetime.strptime(text, "%H:%M").time()
    except ValueError as error:
        raise ValueError(
            f"Unknown keyframe time: {text!r}. Use a 24-hour HH:MM, such as '12:00'."
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
    # Monthly and progress share a nominal window, so the name breaks the tie and
    # the cheap render goes first.
    return sorted(
        (CADENCES[name] for name in dict.fromkeys(names)), key=lambda c: (c.window, c.name)
    )


def validate_config(config: Config) -> list[str]:
    """Return human-readable warnings about settings that will not work as intended."""
    warnings = []

    # Only still-sourced cadences have anything to say about the still library. A
    # keyframe-only configuration has no longest image cadence at all.
    longest_cadence = config.longest_image_cadence
    if longest_cadence is not None:
        longest_window = longest_cadence.window
        if config.image_retention is not None and config.image_retention <= longest_window:
            warnings.append(
                f"image_retention_days ({config.image_retention.days}) is not greater than the "
                f"{longest_cadence.name} cadence window ({longest_window.days} days). Images will be "
                f"pruned before the {longest_cadence.name} render can use them. Set "
                f"image_retention_days to at least {longest_window.days + 1}."
            )

        frames_per_window = longest_window.total_seconds() / config.capture_interval.total_seconds()
        if frames_per_window < config.min_frames_for(longest_cadence.name):
            warnings.append(
                f"A capture interval of {config.capture_interval.total_seconds():.0f}s yields only "
                f"{frames_per_window:.0f} frames per {longest_window.days} days, below min_frames "
                f"({config.min_frames_for(longest_cadence.name)}). Renders will be skipped."
            )

    if config.minimum_free_bytes <= 0:
        warnings.append(
            "min_free_disk_gb is 0, so nothing stops the library from filling the disk if "
            "retention turns out to be too generous for this channel count. Set it to a few GB."
        )

    keyframe_cadences = [cadence for cadence in config.timelapse_cadences if cadence.source == "keyframe"]
    if not keyframe_cadences:
        return warnings

    longest_keyframe_window = max(cadence.window for cadence in keyframe_cadences)
    if config.keyframe_retention is not None and config.keyframe_retention <= longest_keyframe_window:
        warnings.append(
            f"keyframe_retention_days ({config.keyframe_retention.days}) is not greater than the "
            f"{longest_keyframe_window.days}-day window the "
            f"{', '.join(c.name for c in keyframe_cadences)} render(s) read, so keyframes will be "
            f"pruned before they can be used. Keyframes are ~500 MB a year for six channels; set "
            f"keyframe_retention_days to 0 and keep them."
        )

    if config.keyframe_tolerance < config.capture_interval:
        warnings.append(
            f"keyframe tolerance_minutes is shorter than the capture interval "
            f"({config.capture_interval.total_seconds():.0f}s), so most days will hold no still "
            f"close enough to the keyframe time and the progress video will be mostly gaps."
        )

    for cadence in keyframe_cadences:
        output_fps = config.output_fps_for(cadence.name)
        if output_fps >= FLIPBOOK_FPS:
            warnings.append(
                f"output_fps for the {cadence.name} render is {output_fps}, so a 31-frame month "
                f"plays in {31 / output_fps:.1f}s. One frame per day wants something nearer "
                f"{DEFAULT_OUTPUT_FPS_BY_CADENCE.get(cadence.name, 6)}."
            )

        min_frames = config.min_frames_for(cadence.name)
        if min_frames > SHORTEST_MONTH_FRAMES:
            warnings.append(
                f"min_frames for the {cadence.name} render is {min_frames}, above the "
                f"{SHORTEST_MONTH_FRAMES} frames February can hold at one a day, so it would "
                f"never render."
            )

        if cadence.anchored and config.retention_for(cadence.name) is not None:
            warnings.append(
                f"timelapse_retention_days.{cadence.name} is set, but the {cadence.name} video's "
                f"start never moves, so age-based retention deletes the current one as soon as "
                f"that start ages out. Each render supersedes the last instead. Set it to 0."
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
    output_fps = parser.getint("timelapse", "output_fps", fallback=DEFAULT_OUTPUT_FPS)
    min_frames = parser.getint("timelapse", "min_frames", fallback=DEFAULT_MIN_FRAMES)

    library_root = Path(parser["image_capture_library"]["root"]).expanduser()
    analysis_root = Path(
        parser.get("analysis", "root", fallback=str(library_root / "index"))
    ).expanduser()

    nvrs = _parse_nvrs(parser)

    return Config(
        nvrs=nvrs,
        channels=[channel_id for nvr in nvrs for channel_id in nvr.channel_ids],

        capture_interval=timedelta(seconds=parser.getint("capture", "interval_seconds")),
        capture_resolution=VideoResolution(
            width=parser.getint("capture", "resolution.width"),
            height=parser.getint("capture", "resolution.height"),
        ),

        timelapse_video_duration=timedelta(seconds=parser.getint("timelapse", "duration_seconds")),
        timelapse_output_fps=output_fps,
        timelapse_min_frames=min_frames,
        timelapse_output_fps_by_cadence=_parse_render_overrides(
            parser, "output_fps", cadences, output_fps, DEFAULT_OUTPUT_FPS_BY_CADENCE
        ),
        timelapse_min_frames_by_cadence=_parse_render_overrides(
            parser, "min_frames", cadences, min_frames, DEFAULT_MIN_FRAMES_BY_CADENCE
        ),
        timelapse_cadences=cadences,
        render_timezone=_parse_timezone(parser.get("timelapse", "timezone", fallback=DEFAULT_TIMEZONE)),
        deflicker_keyframe_renders=parser.getboolean("timelapse", "deflicker", fallback=DEFAULT_DEFLICKER),
        max_concurrent_renders=max(
            1, parser.getint("timelapse", "max_concurrent_renders", fallback=DEFAULT_MAX_CONCURRENT_RENDERS)
        ),

        image_capture_library_root=library_root,
        image_retention=_optional_days(
            parser, "image_capture_library", "image_retention_days", fallback=DEFAULT_IMAGE_RETENTION_DAYS
        ),
        keyframe_at=_parse_time_of_day(parser.get("keyframe", "at", fallback=DEFAULT_KEYFRAME_AT)),
        keyframe_tolerance=timedelta(
            minutes=parser.getint("keyframe", "tolerance_minutes", fallback=DEFAULT_KEYFRAME_TOLERANCE_MINUTES)
        ),
        keyframe_retention=_optional_days(
            parser, "image_capture_library", "keyframe_retention_days",
            fallback=DEFAULT_KEYFRAME_RETENTION_DAYS,
        ),
        timelapse_retention=_parse_timelapse_retention(parser, cadences),
        minimum_free_bytes=int(
            max(0.0, parser.getfloat(
                "image_capture_library", "min_free_disk_gb", fallback=DEFAULT_MINIMUM_FREE_DISK_GB
            )) * 1_000_000_000
        ),

        # Empty (or absent) disables the archiver outright: a replica of the
        # NVR's recordings is only worth keeping on a volume sized for it.
        archive_root=(
            Path(raw_archive_root).expanduser()
            if (raw_archive_root := parser.get("archive", "root", fallback="").strip())
            else None
        ),
        archive_retention=_optional_days(parser, "archive", "retention_days", fallback=0),
        archive_minimum_free_bytes=int(
            max(0.0, parser.getfloat(
                "archive", "min_free_disk_gb", fallback=DEFAULT_ARCHIVE_MIN_FREE_DISK_GB
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
