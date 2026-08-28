import logging
from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from timelapsed.config import get_config, validate_config
from timelapsed.schema import CADENCES

FULL_CONFIG = """
[nvr]
url = http://192.168.1.10/
username = admin
password = s3cret
channels = 1, 2,3

[capture]
interval_seconds = 5
resolution.width = 1920
resolution.height = 1080

[timelapse]
duration_seconds = 60
output_fps = 24
min_frames = 48
cadences = weekly, hourly, daily

[image_capture_library]
root = {root}
image_retention_days = 14
timelapse_retention_days = 0

[general]
logging_level = debug
"""

MINIMAL_CONFIG = """
[nvr]
url = http://nvr.local
username = admin
password = pw
channels = 1

[capture]
interval_seconds = 10
resolution.width = 1280
resolution.height = 720

[timelapse]
duration_seconds = 30

[image_capture_library]
root = /var/lib/timelapsed
"""


def write_config(path: Path, contents: str) -> Path:
    path.write_text(contents)
    return path


def test_parses_every_setting(tmp_path: Path):
    path = write_config(tmp_path / "timelapsed.ini", FULL_CONFIG.format(root=tmp_path / "library"))

    config = get_config((str(path),))

    assert config.nvr_url == "http://192.168.1.10"  # trailing slash stripped
    assert config.nvr_username == "admin"
    assert config.nvr_password == "s3cret"
    assert config.channels == ["1", "2", "3"]  # whitespace stripped
    assert config.capture_interval == timedelta(seconds=5)
    assert (config.capture_resolution.width, config.capture_resolution.height) == (1920, 1080)
    assert config.timelapse_video_duration == timedelta(seconds=60)
    assert config.timelapse_output_fps == 24
    assert config.timelapse_min_frames == 48
    # sorted shortest window first regardless of the order written in the file
    assert [c.name for c in config.timelapse_cadences] == ["hourly", "daily", "weekly"]
    assert config.web_host == "0.0.0.0"
    assert config.web_port == 8080
    assert config.image_capture_library_root == tmp_path / "library"
    assert config.image_retention == timedelta(days=14)
    assert config.retention_for("weekly") is None  # 0 days means keep forever
    assert config.logging_level == logging.DEBUG


def test_optional_settings_fall_back_to_defaults(tmp_path: Path):
    path = write_config(tmp_path / "timelapsed.ini", MINIMAL_CONFIG)

    config = get_config((str(path),))

    assert config.timelapse_output_fps == 30
    assert config.timelapse_min_frames == 60
    assert config.image_retention == timedelta(days=8)  # one day clear of the weekly window
    assert [c.name for c in config.timelapse_cadences] == ["hourly", "daily", "weekly"]
    assert config.retention_for("weekly") is None
    assert config.logging_level == logging.INFO


def test_the_archive_is_off_until_a_root_is_configured(tmp_path: Path):
    path = write_config(tmp_path / "timelapsed.ini", MINIMAL_CONFIG)

    config = get_config((str(path),))

    assert config.archive_root is None
    assert config.archive_retention is None


def test_archive_settings_are_read(tmp_path: Path):
    path = write_config(
        tmp_path / "timelapsed.ini",
        MINIMAL_CONFIG + "\n[archive]\nroot = /srv/archive\nretention_days = 30\nmin_free_disk_gb = 10\n",
    )

    config = get_config((str(path),))

    assert config.archive_root == Path("/srv/archive")
    assert config.archive_retention == timedelta(days=30)
    assert config.archive_minimum_free_bytes == 10_000_000_000


def test_later_paths_override_earlier_ones(tmp_path: Path):
    system = write_config(tmp_path / "system.ini", MINIMAL_CONFIG)
    override = write_config(tmp_path / "override.ini", "[nvr]\nchannels = 7,8\n")

    config = get_config((str(system), str(override)))

    assert config.channels == ["7", "8"]
    assert config.nvr_url == "http://nvr.local"  # untouched keys survive


def test_missing_config_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="No timelapsed config found"):
        get_config((str(tmp_path / "absent.ini"),))


def test_missing_required_section_raises(tmp_path: Path):
    path = write_config(tmp_path / "timelapsed.ini", "[general]\nlogging_level = INFO\n")

    with pytest.raises(KeyError):
        get_config((str(path),))


def test_rejects_unknown_cadence(tmp_path: Path):
    path = write_config(
        tmp_path / "timelapsed.ini", MINIMAL_CONFIG.replace("duration_seconds = 30", "duration_seconds = 30\ncadences = hourly, fortnightly")
    )

    with pytest.raises(ValueError, match="fortnightly"):
        get_config((str(path),))


def test_rejects_empty_cadence_list(tmp_path: Path):
    path = write_config(tmp_path / "timelapsed.ini", MINIMAL_CONFIG.replace("duration_seconds = 30", "duration_seconds = 30\ncadences ="))

    with pytest.raises(ValueError, match="At least one"):
        get_config((str(path),))


def test_validate_warns_when_retention_cannot_feed_weekly(config):
    config.image_retention = timedelta(days=3)

    warnings = validate_config(config)

    assert any("weekly" in warning and "image_retention_days" in warning for warning in warnings)


def test_validate_warns_when_capture_interval_is_too_slow(config):
    config.capture_interval = timedelta(hours=12)
    config.timelapse_min_frames = 500

    assert any("below min_frames" in warning for warning in validate_config(config))


def test_validate_warns_when_the_disk_floor_is_disabled(config):
    config.minimum_free_bytes = 0

    assert any("min_free_disk_gb" in warning for warning in validate_config(config))


def test_validate_is_quiet_for_a_sane_config(config):
    config.image_retention = timedelta(days=8)

    assert validate_config(config) == []


def test_per_cadence_timelapse_retention_overrides_the_baseline(tmp_path: Path):
    path = write_config(
        tmp_path / "timelapsed.ini",
        MINIMAL_CONFIG.replace(
            "root = /var/lib/timelapsed",
            "root = /var/lib/timelapsed\n"
            "timelapse_retention_days = 30\n"
            "timelapse_retention_days.hourly = 2\n"
            "timelapse_retention_days.weekly = 0",
        ),
    )

    config = get_config((str(path),))

    assert config.retention_for("hourly") == timedelta(days=2)
    assert config.retention_for("daily") == timedelta(days=30)  # inherits the baseline
    assert config.retention_for("weekly") is None  # 0 wins over the baseline


def test_timelapse_retention_defaults_are_per_cadence(tmp_path: Path):
    path = write_config(tmp_path / "timelapsed.ini", MINIMAL_CONFIG)

    config = get_config((str(path),))

    assert config.retention_for("hourly") == timedelta(days=7)
    assert config.retention_for("daily") == timedelta(days=90)
    assert config.retention_for("weekly") is None


def test_timezone_defaults_to_utc(tmp_path: Path):
    """Absent config, rollovers stay on UTC: deterministic, and free of DST."""
    path = write_config(tmp_path / "timelapsed.ini", MINIMAL_CONFIG)

    config = get_config((str(path),))

    assert config.render_timezone == ZoneInfo("UTC")


def test_timezone_is_read_as_an_iana_zone(tmp_path: Path):
    source = FULL_CONFIG.format(root=tmp_path / "library").replace(
        "cadences = weekly, hourly, daily",
        "cadences = weekly, hourly, daily\ntimezone = America/Sao_Paulo",
    )
    path = write_config(tmp_path / "timelapsed.ini", source)

    config = get_config((str(path),))

    assert config.render_timezone == ZoneInfo("America/Sao_Paulo")


def test_an_unknown_timezone_is_rejected(tmp_path: Path):
    source = FULL_CONFIG.format(root=tmp_path / "library").replace(
        "cadences = weekly, hourly, daily",
        "cadences = weekly, hourly, daily\ntimezone = Mars/Olympus_Mons",
    )
    path = write_config(tmp_path / "timelapsed.ini", source)

    with pytest.raises(ValueError, match="Unknown timelapse timezone"):
        get_config((str(path),))


# --- the keyframe track and the calendar cadences ---------------------------

KEYFRAME_CONFIG = MINIMAL_CONFIG.replace(
    "duration_seconds = 30",
    "duration_seconds = 30\n"
    "cadences = hourly, daily, weekly, monthly, progress\n"
    "timezone = America/Sao_Paulo\n"
    "output_fps = 30\n"
    "min_frames = 60\n",
).replace(
    "root = /var/lib/timelapsed",
    "root = /var/lib/timelapsed\n\n[keyframe]\nat = 09:30\ntolerance_minutes = 45\n",
)


def test_keyframe_settings_have_working_defaults(tmp_path: Path):
    path = write_config(tmp_path / "timelapsed.ini", MINIMAL_CONFIG)

    config = get_config((str(path),))

    assert config.keyframe_at == time(12, 0)  # local noon
    assert config.keyframe_tolerance == timedelta(minutes=30)
    assert config.keyframe_retention is None  # 0 days means keep forever
    assert config.deflicker_keyframe_renders is True


def test_keyframe_settings_are_read(tmp_path: Path):
    path = write_config(tmp_path / "timelapsed.ini", KEYFRAME_CONFIG)

    config = get_config((str(path),))

    assert config.keyframe_at == time(9, 30)
    assert config.keyframe_tolerance == timedelta(minutes=45)


@pytest.mark.parametrize("raw", ["25:00", "noon", "12", "12:00:00"])
def test_an_unparseable_keyframe_time_is_a_startup_error(tmp_path: Path, raw):
    path = write_config(
        tmp_path / "timelapsed.ini",
        MINIMAL_CONFIG.replace(
            "root = /var/lib/timelapsed",
            "root = /var/lib/timelapsed\n\n[keyframe]\nat = " + raw + "\n",
        ),
    )

    with pytest.raises(ValueError, match="Unknown keyframe time"):
        get_config((str(path),))


def test_the_calendar_cadences_do_not_inherit_the_render_baselines(tmp_path: Path):
    """The shipped ini writes `output_fps = 30` and `min_frames = 60` explicitly.

    Inheriting those would play a 31-frame month in one second and then refuse to
    render it at all, so a keyframe cadence falls back to its own defaults.
    """
    path = write_config(tmp_path / "timelapsed.ini", KEYFRAME_CONFIG)

    config = get_config((str(path),))

    assert config.output_fps_for("weekly") == 30
    assert config.min_frames_for("weekly") == 60
    assert config.output_fps_for("monthly") == 6
    assert config.min_frames_for("monthly") == 5


def test_an_explicit_per_cadence_render_override_still_wins(tmp_path: Path):
    path = write_config(
        tmp_path / "timelapsed.ini",
        KEYFRAME_CONFIG.replace(
            "min_frames = 60\n",
            "min_frames = 60\nmin_frames.monthly = 20\noutput_fps.monthly = 12\n",
        ),
    )

    config = get_config((str(path),))

    assert config.min_frames_for("monthly") == 20
    assert config.output_fps_for("monthly") == 12


def test_a_keyframe_cadence_does_not_stretch_the_protected_window(tmp_path: Path):
    """The regression guard for the two things `longest_cadence_window` feeds.

    A 31-day monthly window would make `validate_config` demand 32 days of stills
    -- ~380 GB for six channels -- and would hand `reclaim` a protected window so
    long that its first and cheapest tier, stills no render will ever read again,
    is permanently empty.
    """
    path = write_config(tmp_path / "timelapsed.ini", KEYFRAME_CONFIG)

    config = get_config((str(path),))

    assert [c.name for c in config.timelapse_cadences] == [
        "hourly", "daily", "weekly", "monthly", "progress"
    ]
    assert config.longest_cadence_window == timedelta(days=7)
    assert config.longest_image_cadence.name == "weekly"
    assert not any("monthly" in warning for warning in validate_config(config))


def _with_calendar_cadences(config):
    config.image_retention = timedelta(days=8)
    names = ["hourly", "daily", "weekly", "monthly", "progress"]
    config.timelapse_cadences = [CADENCES[name] for name in names]
    config.timelapse_retention = {name: None for name in names}
    config.timelapse_output_fps_by_cadence = {"monthly": 6, "progress": 6}
    config.timelapse_min_frames_by_cadence = {"monthly": 5, "progress": 10}
    return config


def test_validate_is_quiet_with_the_calendar_cadences_enabled(config):
    assert validate_config(_with_calendar_cadences(config)) == []


def test_validate_warns_when_keyframes_expire_before_a_month_is_up(config):
    _with_calendar_cadences(config).keyframe_retention = timedelta(days=10)

    assert any("keyframe_retention_days" in warning for warning in validate_config(config))


def test_validate_warns_when_the_keyframe_tolerance_is_below_the_capture_interval(config):
    config = _with_calendar_cadences(config)
    config.capture_interval = timedelta(minutes=10)
    config.keyframe_tolerance = timedelta(minutes=1)

    assert any("tolerance_minutes" in warning for warning in validate_config(config))


def test_validate_warns_when_a_month_would_flash_past(config):
    config = _with_calendar_cadences(config)
    config.timelapse_output_fps_by_cadence = {"monthly": 30, "progress": 6}

    assert any("output_fps for the monthly" in warning for warning in validate_config(config))


def test_validate_warns_when_a_month_could_never_reach_min_frames(config):
    config = _with_calendar_cadences(config)
    config.timelapse_min_frames_by_cadence = {"monthly": 31, "progress": 10}

    assert any("February" in warning for warning in validate_config(config))


def test_validate_warns_when_the_progress_video_is_given_an_age_based_retention(config):
    """Its start is day one of the project, so pruning on age deletes the current one."""
    config = _with_calendar_cadences(config)
    config.timelapse_retention["progress"] = timedelta(days=90)

    assert any("supersedes" in warning for warning in validate_config(config))


# --- the shipped template ---------------------------------------------------

EXAMPLE_INI = Path(__file__).resolve().parent.parent / "timelapsed.ini.example"


def test_the_shipped_template_loads_and_validates_clean(tmp_path: Path):
    path = write_config(tmp_path / "timelapsed.ini", EXAMPLE_INI.read_text())

    config = get_config((str(path),))

    assert [c.name for c in config.timelapse_cadences] == ["hourly", "daily", "weekly"]
    assert config.keyframe_at == time(12, 0)
    assert config.keyframe_retention is None
    assert validate_config(config) == []


def test_the_shipped_template_validates_clean_with_the_calendar_cadences_on(tmp_path: Path):
    """`install.sh` copies this file verbatim, so it writes `output_fps = 30` and
    `min_frames = 60` into /etc. If the calendar cadences inherited those, turning
    them on would play a month in one second and then refuse to render it."""
    path = write_config(
        tmp_path / "timelapsed.ini",
        EXAMPLE_INI.read_text().replace(
            "cadences = hourly,daily,weekly", "cadences = hourly,daily,weekly,monthly,progress"
        ),
    )

    config = get_config((str(path),))

    assert config.output_fps_for("monthly") == 6
    assert config.output_fps_for("weekly") == 30
    assert config.min_frames_for("monthly") == 5
    assert config.min_frames_for("weekly") == 60
    assert config.longest_cadence_window == timedelta(days=7)
    assert validate_config(config) == []
