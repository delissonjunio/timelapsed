import logging
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from timelapsed.config import get_config, validate_config

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
