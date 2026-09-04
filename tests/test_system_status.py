"""The status report: the scan, the arithmetic on top of it, and the endpoints.

The report is built from a real library on a real filesystem, like the rest of
the suite. Frames here are a few bytes of filler rather than real JPEGs -- the
report never decodes one, it only counts and sizes them, so synthesising valid
images would buy nothing and would pull ffmpeg into tests that do not need it.
"""
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from timelapsed import system_status
from timelapsed.analysis.index import AnalysisIndex, to_epoch
from timelapsed.image_capture_library import ImageCaptureLibrary
from timelapsed.system_status import (
    AnalysisProgress,
    SystemStatusCollector,
    scan_frames,
    scan_timelapses,
)
from timelapsed.web import build_server

FRAME = b"x" * 4096


@pytest.fixture
def now() -> datetime:
    return datetime.now(tz=timezone.utc)


@pytest.fixture
def capture(config):
    """Writes stills into the configured library at chosen offsets from now."""
    library = ImageCaptureLibrary(config.image_capture_library_root)

    def _capture(channel: str, taken_at: list[datetime], payload: bytes = FRAME) -> None:
        for moment in taken_at:
            library.store_image(channel, "jpg", payload, moment)

    _capture.library = library  # pyright: ignore[reportFunctionMemberAccess]
    return _capture


def steady(now: datetime, count: int, every: timedelta, ending: timedelta = timedelta()) -> list[datetime]:
    """`count` instants `every` apart, the newest `ending` ago."""
    last = now - ending
    return [last - every * offset for offset in range(count)]


# --- the scan ---


def test_scan_counts_sizes_and_dates_a_frame_directory(tmp_path, capture, config, now):
    capture("1", steady(now, 5, timedelta(seconds=10)))

    scan = scan_frames(config.image_capture_library_root / "1" / "image")

    assert scan.files == 5
    assert scan.bytes == 5 * len(FRAME)
    assert scan.newest is not None and scan.oldest is not None
    assert scan.newest - scan.oldest == timedelta(seconds=40)


def test_scan_counts_frames_past_each_cutoff_it_is_given(capture, config, now):
    capture("1", steady(now, 6, timedelta(minutes=30)))  # newest now, oldest 2.5h ago

    scan = scan_frames(
        config.image_capture_library_root / "1" / "image",
        {"hour": now - timedelta(hours=1), "day": now - timedelta(days=1)},
    )

    assert scan.recent_files["hour"] == 3  # now, -30m, -60m
    assert scan.recent_files["day"] == 6
    assert scan.recent_bytes["hour"] == 3 * len(FRAME)


def test_scan_of_a_directory_that_does_not_exist_is_empty_rather_than_an_error(tmp_path):
    """A camera added to the config an hour ago has no directory yet."""
    scan = scan_frames(tmp_path / "nothing" / "here", {"hour": datetime.now(timezone.utc)})

    assert scan.files == 0 and scan.oldest is None
    assert scan.recent_files == {"hour": 0}


def test_scan_ignores_files_that_are_not_frames(capture, config, now):
    capture("1", steady(now, 2, timedelta(seconds=10)))
    (config.image_capture_library_root / "1" / "image" / "notes.txt").write_bytes(b"hello")
    (config.image_capture_library_root / "1" / "image" / ".partial.jpg").write_bytes(b"hello")

    assert scan_frames(config.image_capture_library_root / "1" / "image").files == 2


def test_scan_separates_out_bytes_shared_with_another_track(capture, config, now):
    """Keyframes are hardlinks, so their bytes are already counted as stills."""
    capture("1", [now])
    still = next((config.image_capture_library_root / "1" / "image").iterdir())
    capture.library.store_keyframe("1", still, now)

    keyframes = scan_frames(config.image_capture_library_root / "1" / "keyframe")

    assert keyframes.bytes == len(FRAME)
    assert keyframes.shared_bytes == len(FRAME)
    assert keyframes.unique_bytes == 0


def test_scan_reads_stored_timelapses_back_out(config, now):
    directory = config.image_capture_library_root / "1" / "timelapse"
    directory.mkdir(parents=True)
    (directory / "hourly_20250601_120000_UTC-20250601_130000_UTC.mp4").write_bytes(b"video")

    videos = scan_timelapses(directory)

    assert len(videos) == 1
    assert videos[0].cadence == "hourly"
    assert videos[0].bytes == 5


# --- storage ---


def test_storage_totals_do_not_count_a_hardlinked_keyframe_twice(capture, config, now):
    capture("1", steady(now, 4, timedelta(hours=1)))
    for still in (config.image_capture_library_root / "1" / "image").iterdir():
        capture.library.store_keyframe("1", still, now)
        break

    report = SystemStatusCollector(config).report()
    channel = next(row for row in report["storage"]["channels"] if row["channel"] == "1")

    assert channel["keyframe"]["files"] == 1
    assert channel["keyframe"]["unique_bytes"] == 0
    # Four stills and a keyframe pointing at one of them is four frames of disk.
    assert channel["bytes"] == 4 * len(FRAME)


def test_storage_reports_videos_per_cadence(config, now):
    directory = config.image_capture_library_root / "1" / "timelapse"
    directory.mkdir(parents=True)
    (directory / "hourly_20250601_120000_UTC-20250601_130000_UTC.mp4").write_bytes(b"a" * 10)
    (directory / "daily_20250601_000000_UTC-20250602_000000_UTC.mp4").write_bytes(b"b" * 20)

    report = SystemStatusCollector(config).report()
    channel = next(row for row in report["storage"]["channels"] if row["channel"] == "1")

    assert channel["timelapse"]["by_cadence"]["hourly"]["bytes"] == 10
    assert channel["timelapse"]["by_cadence"]["daily"]["files"] == 1


# --- capture ---


def test_a_camera_writing_now_is_live_and_one_that_stopped_is_stale(capture, config, now):
    capture("1", steady(now, 20, config.capture_interval))
    capture("2", steady(now, 20, config.capture_interval, ending=timedelta(hours=3)))

    rows = {row["channel"]: row for row in SystemStatusCollector(config).report()["capture"]["channels"]}

    assert rows["1"]["state"] == "live"
    assert rows["2"]["state"] == "stale"
    assert rows["2"]["last_frame_age_seconds"] > 3 * 3600 - 60


def test_a_configured_camera_with_nothing_on_disk_is_silent(config):
    rows = {row["channel"]: row for row in SystemStatusCollector(config).report()["capture"]["channels"]}

    assert rows["1"]["state"] == "silent"
    assert rows["1"]["frames"] == 0


def test_a_channel_holding_frames_but_missing_from_the_config_is_retired(capture, config, now):
    """Nothing prunes a channel the daemon no longer captures, so it is called out."""
    capture("9", steady(now, 5, config.capture_interval))

    report = SystemStatusCollector(config).report()
    row = next(entry for entry in report["capture"]["channels"] if entry["channel"] == "9")

    assert row["configured"] is False and row["state"] == "retired"
    assert any(check["title"].endswith("is not in the config") for check in report["checks"])


def test_yield_is_measured_against_how_long_the_camera_has_actually_run(capture, config, now):
    """A camera five minutes old has not missed the other 23 hours of the day."""
    capture("1", steady(now, 60, config.capture_interval))  # five minutes at 5s

    row = next(
        entry for entry in SystemStatusCollector(config).report()["capture"]["channels"]
        if entry["channel"] == "1"
    )

    assert row["yield"]["day"] == pytest.approx(1.0, abs=0.05)
    assert row["expected_recent"]["day"] < 100


def test_a_camera_dropping_frames_is_reported_even_while_it_is_still_writing(capture, config, now):
    # A tenth of the frames the interval calls for, the newest of them just now.
    capture("1", steady(now, 72, config.capture_interval * 10))

    report = SystemStatusCollector(config).report()
    row = next(entry for entry in report["capture"]["channels"] if entry["channel"] == "1")

    assert row["state"] == "live"
    assert row["yield"]["hour"] < 0.2
    assert any("dropping frames" in check["title"] for check in report["checks"])


# --- analysis ---


@pytest.fixture
def index(config):
    with AnalysisIndex(config.analysis_index_path) as opened:
        yield opened


class Reader:
    """The slice of RecognitionReader the collector actually uses."""

    def __init__(self, index: AnalysisIndex):
        self.index = index

    def watermark_epochs(self) -> dict[str, int]:
        return self.index.watermarks()

    def table_counts(self) -> dict[str, int]:
        return self.index.table_counts()

    def segment_summary(self) -> dict[str, dict]:
        return self.index.segment_summary()


def test_analysis_lag_is_measured_against_the_newest_frame_not_the_clock(capture, config, index, now):
    capture("1", steady(now, 100, config.capture_interval))
    index.set_watermark("1", to_epoch(now - timedelta(minutes=2)))

    report = SystemStatusCollector(config).report(recognition=Reader(index))
    row = next(entry for entry in report["analysis"]["channels"] if entry["channel"] == "1")

    assert row["lag_seconds"] == pytest.approx(120, abs=10)
    # 100 frames at 5s covers eight minutes; the last two of them are unanalysed.
    assert row["backlog_frames"] == pytest.approx(24, abs=2)


def test_analysis_that_has_reached_the_newest_frame_is_current(capture, config, index, now):
    frames = steady(now, 20, config.capture_interval)
    capture("1", frames)
    index.set_watermark("1", to_epoch(max(frames)))

    row = next(
        entry for entry in
        SystemStatusCollector(config).report(recognition=Reader(index))["analysis"]["channels"]
        if entry["channel"] == "1"
    )

    assert row["state"] == "current" and row["lag_seconds"] == 0


def test_a_channel_with_frames_and_no_watermark_has_all_of_them_outstanding(capture, config, index, now):
    capture("1", steady(now, 12, config.capture_interval))

    row = next(
        entry for entry in
        SystemStatusCollector(config).report(recognition=Reader(index))["analysis"]["channels"]
        if entry["channel"] == "1"
    )

    assert row["state"] == "unstarted" and row["backlog_frames"] == 12


def test_analysis_counts_come_from_the_index(capture, config, index, now):
    event = index.open_event("1", "person", to_epoch(now - timedelta(minutes=5)), 0.9)
    index.add_detection(event, "1", to_epoch(now), "person", 0.9, (0, 0, 10, 10))
    identity = index.create_identity("person", to_epoch(now))
    index.rename_identity(identity, "Someone")

    counts = SystemStatusCollector(config).report(recognition=Reader(index))["analysis"]["counts"]

    assert counts["event"] == 1 and counts["detection"] == 1
    assert counts["identity"] == 1 and counts["named_identity"] == 1


def test_analysis_reports_itself_unreachable_when_the_index_is_not_there(config):
    analysis = SystemStatusCollector(config).report()["analysis"]

    assert analysis["enabled"] is True and analysis["reachable"] is False
    assert analysis["channels"] == []


def test_analysis_is_simply_off_when_recognition_is_disabled(config):
    disabled = SystemStatusCollector(config)
    disabled.config = config.__class__(**{**config.__dict__, "analysis_enabled": False})

    assert disabled.report()["analysis"] == {"enabled": False, "reachable": False, "channels": []}


# --- the rate sampler ---


def test_a_rate_needs_two_readings_far_enough_apart_to_mean_anything():
    progress = AnalysisProgress()
    progress.record(1000.0, {"1": 500})

    assert progress.rate("1") is None

    progress.record(1010.0, {"1": 520})
    assert progress.rate("1") is None, "ten seconds is measuring the commit interval"

    progress.record(1100.0, {"1": 700})
    assert progress.rate("1") == pytest.approx(2.0)  # 200 analysed seconds in 100


def test_old_readings_are_dropped_so_the_rate_describes_now():
    progress = AnalysisProgress(history_seconds=60)
    progress.record(1000.0, {"1": 0})
    progress.record(1200.0, {"1": 100})
    progress.record(1260.0, {"1": 160})

    # The 1000 sample is outside the hour... window, so the rate spans 1200-1260.
    assert progress.rate("1") == pytest.approx(1.0)


def test_a_channel_the_sampler_has_not_seen_has_no_rate():
    progress = AnalysisProgress()
    progress.record(1000.0, {"1": 0})
    progress.record(1100.0, {"1": 100})

    assert progress.rate("2") is None


class FixedRate:
    """A sampler stuck at one rate.

    Standing in for AnalysisProgress so the report's use of a rate can be tested
    without spending MIN_RATE_SPAN_SECONDS of real time producing one. The
    sampler's own arithmetic is covered by the tests above it.
    """

    def __init__(self, rate: float | None):
        self._rate = rate

    def record(self, *_) -> None:
        pass

    def rate(self, _channel: str) -> float | None:
        return self._rate


def test_analysis_that_is_not_gaining_on_the_cameras_is_reported_as_losing(capture, config, index, now):
    capture("1", steady(now, 200, config.capture_interval))
    index.set_watermark("1", to_epoch(now - timedelta(minutes=10)))

    collector = SystemStatusCollector(config)
    collector.progress = cast(Any, FixedRate(0.5))  # Half real time: the backlog only grows.

    row = next(
        entry for entry in collector.report(recognition=Reader(index))["analysis"]["channels"]
        if entry["channel"] == "1"
    )

    assert row["rate"] == pytest.approx(0.5)
    assert row["state"] == "losing" and row["eta_seconds"] is None


def test_a_watermark_that_never_moves_is_a_stopped_analyzer_not_a_slow_one(capture, config, index, now):
    capture("1", steady(now, 200, config.capture_interval))
    index.set_watermark("1", to_epoch(now - timedelta(minutes=30)))

    collector = SystemStatusCollector(config)
    collector.progress = cast(Any, FixedRate(0.0))

    report = collector.report(recognition=Reader(index))
    row = next(entry for entry in report["analysis"]["channels"] if entry["channel"] == "1")

    assert row["state"] == "stopped"
    assert any("Analysis has stopped" in check["title"] for check in report["checks"])


def test_a_channel_that_is_caught_up_is_not_called_stopped_for_standing_still(capture, config, index, now):
    """A current channel's watermark is idle because there is nothing to do."""
    frames = steady(now, 20, config.capture_interval)
    capture("1", frames)
    index.set_watermark("1", to_epoch(max(frames)))

    collector = SystemStatusCollector(config)
    collector.progress = cast(Any, FixedRate(0.0))

    report = collector.report(recognition=Reader(index))
    row = next(entry for entry in report["analysis"]["channels"] if entry["channel"] == "1")

    assert row["state"] == "current"
    assert not any("Analysis has stopped" in check["title"] for check in report["checks"])


def test_analysis_that_is_gaining_is_told_how_long_it_has_left(capture, config, index, now):
    capture("1", steady(now, 400, config.capture_interval))
    index.set_watermark("1", to_epoch(now - timedelta(minutes=20)))

    collector = SystemStatusCollector(config)
    # Three times real time, so it closes the gap at 2x per second of wall clock.
    collector.progress = cast(Any, FixedRate(3.0))

    row = next(
        entry for entry in collector.report(recognition=Reader(index))["analysis"]["channels"]
        if entry["channel"] == "1"
    )

    assert row["state"] == "behind"
    assert row["eta_seconds"] == pytest.approx(600, abs=30)  # 1200s of lag closing at 2x


def test_a_channel_that_is_current_is_never_given_an_eta(capture, config, index, now):
    frames = steady(now, 30, config.capture_interval)
    capture("1", frames)
    index.set_watermark("1", to_epoch(max(frames)))

    collector = SystemStatusCollector(config)
    collector.progress = cast(Any, FixedRate(4.0))

    row = next(
        entry for entry in collector.report(recognition=Reader(index))["analysis"]["channels"]
        if entry["channel"] == "1"
    )

    assert row["state"] == "current" and row["eta_seconds"] is None


# --- renders ---


def video(config, channel: str, name: str) -> None:
    directory = config.image_capture_library_root / channel / "timelapse"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.mp4").write_bytes(b"v" * 100)


def test_a_closed_period_with_no_video_is_reported_as_due(capture, config, now):
    # Two hours of stills and nothing rendered: the hour that just closed is due.
    capture("1", steady(now, 60, timedelta(minutes=2)))

    report = SystemStatusCollector(config).report()
    row = next(entry for entry in report["renders"]["channels"] if entry["channel"] == "1")
    hourly = next(entry for entry in row["cadences"] if entry["cadence"] == "hourly")

    assert hourly["missing_periods"] >= 1
    assert hourly["latest_rendered"] is False


def test_rendering_the_period_that_just_closed_clears_it(capture, config, now):
    capture("1", steady(now, 60, timedelta(minutes=2)))
    closed = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    video(config, "1", "hourly_" + closed.strftime("%Y%m%d_%H%M%S_%Z")
          + "-" + (closed + timedelta(hours=1)).strftime("%Y%m%d_%H%M%S_%Z"))

    row = next(
        entry for entry in SystemStatusCollector(config).report()["renders"]["channels"]
        if entry["channel"] == "1"
    )
    hourly = next(entry for entry in row["cadences"] if entry["cadence"] == "hourly")

    assert hourly["latest_rendered"] is True
    assert hourly["files"] == 1


def test_periods_from_before_the_camera_captured_anything_are_not_counted_as_due(capture, config, now):
    """Ten minutes of stills is not a week of missing weeklies."""
    capture("1", steady(now, 120, timedelta(seconds=5)))

    row = next(
        entry for entry in SystemStatusCollector(config).report()["renders"]["channels"]
        if entry["channel"] == "1"
    )

    assert {entry["cadence"]: entry["missing_periods"] for entry in row["cadences"]}["weekly"] == 0
    assert next(e for e in row["cadences"] if e["cadence"] == "daily")["missing_periods"] == 0


def test_a_channel_with_no_frames_at_all_has_nothing_outstanding(config):
    assert SystemStatusCollector(config).report()["renders"]["outstanding"] == 0


def test_a_video_covering_a_period_counts_even_if_it_does_not_start_on_the_hour(capture, config, now):
    """The library holds clips written before windows were clock-aligned.

    Matching a period on an exact start read every one of those as a missing
    hour: on the live server that was six cameras reported ~20 renders behind
    while every hour actually had a video.
    """
    capture("1", steady(now, 60, timedelta(minutes=2)))
    closed = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    ragged = closed + timedelta(minutes=4, seconds=41)
    video(config, "1", "hourly_" + ragged.strftime("%Y%m%d_%H%M%S_%Z")
          + "-" + (ragged + timedelta(hours=1)).strftime("%Y%m%d_%H%M%S_%Z"))

    row = next(
        entry for entry in SystemStatusCollector(config).report()["renders"]["channels"]
        if entry["channel"] == "1"
    )
    hourly = next(entry for entry in row["cadences"] if entry["cadence"] == "hourly")

    assert hourly["latest_rendered"] is True
    assert hourly["missing_periods"] == 0


def test_a_video_starting_in_the_next_period_does_not_cover_this_one(capture, config, now):
    """Containment, not proximity: the match is still bounded by the period."""
    capture("1", steady(now, 120, timedelta(minutes=2)))
    closed = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    earlier = closed - timedelta(hours=1)
    video(config, "1", "hourly_" + earlier.strftime("%Y%m%d_%H%M%S_%Z")
          + "-" + closed.strftime("%Y%m%d_%H%M%S_%Z"))

    row = next(
        entry for entry in SystemStatusCollector(config).report()["renders"]["channels"]
        if entry["channel"] == "1"
    )
    hourly = next(entry for entry in row["cadences"] if entry["cadence"] == "hourly")

    assert hourly["latest_rendered"] is False


# --- what counts as a channel ---


def test_the_analysis_root_inside_the_library_is_not_mistaken_for_a_camera(capture, config, now):
    """The default analysis_root is `{library}/index`, right beside the channels."""
    capture("1", steady(now, 5, config.capture_interval))
    (config.image_capture_library_root / "index" / "crops" / "event").mkdir(parents=True)
    (config.image_capture_library_root / "index" / "index.sqlite3").write_bytes(b"x")

    report = SystemStatusCollector(config).report()

    assert [row["channel"] for row in report["capture"]["channels"]] == ["1", "2"]
    assert not any(check["title"].startswith("Channel index") for check in report["checks"])


def test_a_directory_holding_a_real_track_is_still_a_channel(capture, config, now):
    capture("9", steady(now, 3, config.capture_interval))

    channels = [
        row["channel"] for row in SystemStatusCollector(config).report()["capture"]["channels"]
    ]

    assert channels == ["1", "2", "9"]


# --- systemd ---


def test_systemd_being_unreachable_is_reported_rather_than_looking_like_no_systemd(config, monkeypatch):
    """The viewer's own sandbox can block the bus, which is not the same thing."""
    monkeypatch.setattr(system_status.shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(
        system_status.subprocess, "run",
        lambda *_, **__: subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Failed to connect to bus: Address family not supported\n"
        ),
    )

    report = SystemStatusCollector(config).report()

    assert report["services"] == {"unavailable": "Failed to connect to bus: Address family not supported"}
    assert any("systemd could not be asked" in check["title"] for check in report["checks"])


def test_units_that_are_simply_not_installed_are_left_out_without_complaint(config, monkeypatch):
    monkeypatch.setattr(system_status.shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(
        system_status.subprocess, "run",
        lambda *_, **__: subprocess.CompletedProcess(
            [], 0, stdout="LoadState=not-found\nActiveState=inactive\n", stderr=""
        ),
    )

    report = SystemStatusCollector(config).report()

    assert report["services"] is None
    assert not any("systemd" in check["title"] for check in report["checks"])


def test_a_machine_with_no_systemd_at_all_simply_has_no_services(config, monkeypatch):
    monkeypatch.setattr(system_status.shutil, "which", lambda _: None)

    assert SystemStatusCollector(config).report()["services"] is None


def test_the_viewer_unit_may_reach_the_bus_over_a_unix_socket():
    """`systemctl` talks to systemd over AF_UNIX; without it /status shows nothing.

    Checked here rather than noticed in production a second time: the unit file
    is not imported by anything, so nothing else would ever fail on it.
    """
    units = Path(__file__).resolve().parent.parent / "deploy"
    for name in ("timelapsed-web.service", "timelapsed.service", "timelapsed-analyzer.service"):
        families = [
            line for line in (units / name).read_text().splitlines()
            if line.startswith("RestrictAddressFamilies=")
        ]
        assert families, f"{name} has no RestrictAddressFamilies"
        assert "AF_UNIX" in families[0], f"{name} cannot reach the systemd bus"


# --- retention, growth and the disk ---


def test_retention_reports_the_oldest_frame_against_what_is_configured(capture, config, now):
    capture("1", [now - timedelta(days=3), now])

    tracks = {
        row["track"]: row for row in SystemStatusCollector(config).report()["retention"]["tracks"]
    }

    assert tracks["image"]["retention_seconds"] == 7 * 86400
    assert tracks["image"]["used_fraction"] == pytest.approx(3 / 7, abs=0.01)
    assert tracks["image"]["saturated"] is False


def test_a_track_kept_forever_has_no_fraction_to_report(capture, config, now):
    """keyframe_retention is None in the fixture, which means keep them."""
    capture("1", [now])
    still = next((config.image_capture_library_root / "1" / "image").iterdir())
    capture.library.store_keyframe("1", still, now)

    tracks = {
        row["track"]: row for row in SystemStatusCollector(config).report()["retention"]["tracks"]
    }

    assert tracks["keyframe"]["retention_seconds"] is None
    assert tracks["keyframe"]["used_fraction"] is None


def test_stills_left_well_past_their_retention_say_so(capture, config, now):
    """Pruning runs hourly; a fortnight of overshoot means it is not running."""
    capture("1", [now - timedelta(days=14), now])

    report = SystemStatusCollector(config).report()

    assert report["retention"]["tracks"][0]["used_fraction"] == pytest.approx(2.0, abs=0.01)
    assert any("outliving their retention" in check["title"] for check in report["checks"])


def test_being_a_little_past_retention_between_prunes_is_not_worth_reporting(capture, config, now):
    capture("1", [now - timedelta(days=7, hours=2), now])

    checks = SystemStatusCollector(config).report()["checks"]

    assert not any("outliving their retention" in check["title"] for check in checks)


def test_growth_is_judged_on_the_stills_not_on_a_keyframe_track_kept_forever(capture, config, now):
    """keyframe_retention is None here, and a track kept forever never settles."""
    capture("1", [now - timedelta(hours=6), now])

    growth = SystemStatusCollector(config).report()["growth"]

    assert growth["saturated"] is False
    assert growth["days_until_floor"] is not None


def test_growth_measures_the_last_day_and_projects_where_retention_parks_it(capture, config, now):
    capture("1", steady(now, 100, timedelta(minutes=10)))  # ~16 hours of frames

    growth = SystemStatusCollector(config).report()["growth"]

    assert growth["measured_bytes_per_day"] == 100 * len(FRAME)
    # Seven days of retention at one frame every five seconds is far more than
    # the sixteen hours actually on disk.
    assert growth["steady_state_bytes"] > growth["measured_bytes_per_day"]


def test_the_disk_report_carries_the_free_space_floor(config):
    disk = SystemStatusCollector(config).report()["disk"]

    assert disk["available"] is True
    assert disk["minimum_free_bytes"] == config.minimum_free_bytes
    assert disk["headroom_bytes"] == disk["free_bytes"] - config.minimum_free_bytes


def test_a_floor_bigger_than_the_disk_is_reported_as_an_error(config):
    collector = SystemStatusCollector(config)
    collector.config = config.__class__(
        **{**config.__dict__, "minimum_free_bytes": 10 ** 18}
    )

    report = collector.report()

    assert report["disk"]["floor_met"] is False
    assert report["checks"][0]["level"] == "error"
    assert "below the floor" in report["checks"][0]["title"]


# --- checks and caching ---


def test_configuration_warnings_reach_the_page(config):
    """The same warnings the daemon prints at startup, where they can be seen."""
    collector = SystemStatusCollector(config)
    collector.config = config.__class__(
        **{**config.__dict__, "image_retention": timedelta(days=2)}
    )

    checks = collector.report()["checks"]

    assert any(
        check["title"] == "Configuration" and "image_retention_days" in check["detail"]
        for check in checks
    )


def test_checks_are_ordered_worst_first(capture, config, now):
    capture("1", steady(now, 5, config.capture_interval, ending=timedelta(hours=6)))
    capture("9", steady(now, 5, config.capture_interval))

    levels = [check["level"] for check in SystemStatusCollector(config).report()["checks"]]

    assert levels == sorted(levels, key=["error", "warn", "info"].index)


def test_a_healthy_library_reports_no_checks_at_all(capture, config, now):
    capture("1", steady(now, 720, config.capture_interval))
    capture("2", steady(now, 720, config.capture_interval))
    for cadence in ("hourly", "daily", "weekly"):
        for channel in ("1", "2"):
            # Nothing is due when the frames only cover the current period.
            pass

    report = SystemStatusCollector(config).report()

    assert [check for check in report["checks"] if check["level"] == "error"] == []


def test_a_second_look_inside_the_window_is_served_from_the_cache(capture, config, now):
    capture("1", steady(now, 5, config.capture_interval))
    collector = SystemStatusCollector(config)

    first = collector.report()
    second = collector.report()

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["generated_at"] == first["generated_at"]


def test_forcing_a_refresh_rescans(capture, config, now):
    collector = SystemStatusCollector(config)
    collector.report()
    capture("1", steady(now, 5, config.capture_interval))

    assert collector.report()["storage"]["by_track"]["image"]["files"] == 0
    assert collector.report(force=True)["storage"]["by_track"]["image"]["files"] == 5


def test_a_collector_with_no_stale_window_rescans_on_every_look(capture, config, now):
    collector = SystemStatusCollector(config, ttl_seconds=0, max_age_seconds=0)
    collector.report()
    capture("1", steady(now, 3, config.capture_interval))

    assert collector.report()["storage"]["by_track"]["image"]["files"] == 3


def test_past_the_ttl_the_old_report_is_served_while_a_rescan_runs(capture, config, now):
    """A poll never waits on the scan: it gets what there is, and the next one gets the rest."""
    collector = SystemStatusCollector(config, ttl_seconds=0)
    collector.report()
    capture("1", steady(now, 3, config.capture_interval))

    stale = collector.report()
    collector.join_refresh(timeout=5)
    refreshed = collector.report()

    assert stale["cached"] is True
    assert stale["storage"]["by_track"]["image"]["files"] == 0
    assert refreshed["storage"]["by_track"]["image"]["files"] == 3


def test_only_one_background_rescan_runs_at_a_time(capture, config, monkeypatch):
    collector = SystemStatusCollector(config, ttl_seconds=0)
    collector.report()

    gate = threading.Event()
    scans: list[float] = []
    collect = collector._collect

    def slow_collect(recognition):
        scans.append(time.monotonic())
        gate.wait(timeout=5)
        return collect(recognition)

    monkeypatch.setattr(collector, "_collect", slow_collect)

    collector.report()
    collector.report()
    gate.set()
    collector.join_refresh(timeout=5)

    assert len(scans) == 1


# --- over HTTP ---


@pytest.fixture
def base_url(config, capture, now):
    capture("1", steady(now, 30, config.capture_interval))
    # /library needs the index to exist, and the nav links are checked below.
    AnalysisIndex(config.analysis_index_path).close()
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fetch(url: str, method: str = "GET"):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request) as response:
        return response, response.read()


def test_the_status_page_is_served_as_html(base_url):
    response, body = fetch(f"{base_url}/status")

    assert response.status == 200
    assert response.headers["Content-Type"].startswith("text/html")
    assert b"System status" in body
    assert b"/api/system" in body


def test_the_status_page_answers_head_with_the_length_it_would_send(base_url):
    head, body = fetch(f"{base_url}/status", method="HEAD")
    _, full = fetch(f"{base_url}/status")

    assert body == b""
    assert int(head.headers["Content-Length"]) == len(full)


def test_the_report_endpoint_returns_the_whole_report(base_url):
    _, body = fetch(f"{base_url}/api/system")
    report = json.loads(body)

    assert set(report) >= {
        "disk", "storage", "capture", "renders", "analysis", "retention",
        "growth", "host", "config", "checks",
    }
    assert report["capture"]["channels"][0]["frames"] == 30


def test_the_report_endpoint_is_not_cached_by_the_browser(base_url):
    response, _ = fetch(f"{base_url}/api/system")

    assert response.headers["Cache-Control"] == "no-store"


def test_asking_for_a_refresh_rebuilds_the_report(base_url):
    first = json.loads(fetch(f"{base_url}/api/system")[1])
    cached = json.loads(fetch(f"{base_url}/api/system")[1])
    forced = json.loads(fetch(f"{base_url}/api/system?refresh=1")[1])

    assert cached["cached"] is True
    assert forced["cached"] is False
    assert forced["generated_at"] >= first["generated_at"]


def test_the_status_page_works_with_recognition_switched_off(config, capture, now):
    """Storage and capture are the viewer's problems either way."""
    capture("1", steady(now, 4, config.capture_interval))
    without = config.__class__(**{**config.__dict__, "analysis_enabled": False})
    server = build_server(without)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://{server.server_address[0]}:{server.server_address[1]}"
        assert fetch(f"{url}/status")[0].status == 200
        report = json.loads(fetch(f"{url}/api/system")[1])
        assert report["analysis"]["enabled"] is False
        assert report["storage"]["by_track"]["image"]["files"] == 4

        # /library, which does need it, still refuses.
        with pytest.raises(urllib.error.HTTPError) as error:
            fetch(f"{url}/library")
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_viewer_and_the_library_both_link_to_the_status_page(base_url):
    assert b'href="/status"' in fetch(f"{base_url}/")[1]
    assert b'href="/status"' in fetch(f"{base_url}/library")[1]


# --- the archive ---


def archive_file(config, channel: str, started: datetime, ended: datetime, name: str, size: int = 512):
    from timelapsed.archiver import segment_filename

    day = config.archive_root / channel / started.astimezone(timezone.utc).strftime("%Y%m%d")
    day.mkdir(parents=True, exist_ok=True)
    (day / segment_filename(started, ended, name)).write_bytes(b"v" * size)


def test_the_archive_section_is_off_until_a_root_is_configured(config):
    report = SystemStatusCollector(config).report()

    assert report["archive"] == {"enabled": False, "channels": []}


def test_the_archive_is_measured_against_the_mirror(config, index, tmp_path, now):
    config.archive_root = tmp_path / "archive"
    old = now - timedelta(days=2)
    archive_file(config, "1", old, old + timedelta(minutes=1), "ch01_001")
    # The mirror lists that segment plus a newer one not yet replicated.
    index.record_segments("1", [
        (to_epoch(old), to_epoch(old + timedelta(minutes=1)), 512, "rtsp://nvr/x?name=ch01_001&size=512"),
        (to_epoch(now - timedelta(hours=1)), to_epoch(now - timedelta(minutes=58)), 900,
         "rtsp://nvr/x?name=ch01_002&size=900"),
    ], swept_through=to_epoch(now))

    report = SystemStatusCollector(config).report(recognition=Reader(index))
    archive = report["archive"]
    row = next(entry for entry in archive["channels"] if entry["channel"] == "1")

    assert archive["enabled"] and archive["total_files"] == 1
    assert row["files"] == 1 and row["recorded_segments"] == 2
    assert row["backlog_segments"] == 1
    # Behind by the distance from the replicated segment's end to the newest
    # recording the mirror lists: two days less the hour.
    assert row["lag_seconds"] == pytest.approx((timedelta(days=2) - timedelta(minutes=59)).total_seconds(), abs=5)
    assert any("behind the NVR" in check["title"] for check in report["checks"])


def test_an_empty_archive_reports_itself_rather_than_failing(config, tmp_path):
    config.archive_root = tmp_path / "archive"

    report = SystemStatusCollector(config).report()
    archive = report["archive"]

    assert archive["enabled"] and archive["total_files"] == 0
    assert all(row["files"] == 0 for row in archive["channels"])
    assert any(check["title"] == "The archive is empty" for check in report["checks"])


def test_the_archiver_status_file_splits_expired_from_the_backlog(config, index, tmp_path, now):
    from timelapsed.archiver import STATUS_FILENAME

    config.archive_root = tmp_path / "archive"
    config.archive_root.mkdir(parents=True)
    old = now - timedelta(days=10)
    # The mirror lists three segments, none replicated: two the device has
    # already recycled, one failing its fetches.
    index.record_segments("1", [
        (to_epoch(old), to_epoch(old + timedelta(minutes=1)), 512, "rtsp://nvr/x?name=ch01_001&size=512"),
        (to_epoch(old + timedelta(hours=1)), to_epoch(old + timedelta(hours=1, minutes=1)), 512,
         "rtsp://nvr/x?name=ch01_002&size=512"),
        (to_epoch(now - timedelta(hours=2)), to_epoch(now - timedelta(hours=1)), 900,
         "rtsp://nvr/x?name=ch01_003&size=900"),
    ], swept_through=to_epoch(now))
    (config.archive_root / STATUS_FILENAME).write_text(json.dumps({
        "generated_at": now.isoformat(),
        "channels": {"1": {
            "pending": 0, "waiting_retry": 1, "abandoned": 1, "expired": 1, "horizon": None,
        }},
    }))

    report = SystemStatusCollector(config).report(recognition=Reader(index))
    archive = report["archive"]
    row = next(entry for entry in archive["channels"] if entry["channel"] == "1")

    assert row["expired_segments"] == 1 and row["failing_segments"] == 1
    assert row["abandoned_segments"] == 1
    # Three listed: one recycled forever, one written off, one truly owed.
    assert row["backlog_segments"] == 1
    assert archive["expired_segments"] == 1 and archive["failing_segments"] == 1
    assert archive["abandoned_segments"] == 1
    assert any(check["title"] == "Segments are failing to archive" for check in report["checks"])


def test_a_stale_archiver_status_file_is_ignored(config, index, tmp_path, now):
    from timelapsed.archiver import STATUS_FILENAME

    config.archive_root = tmp_path / "archive"
    config.archive_root.mkdir(parents=True)
    index.record_segments("1", [
        (to_epoch(now - timedelta(hours=2)), to_epoch(now - timedelta(hours=1)), 900,
         "rtsp://nvr/x?name=ch01_003&size=900"),
    ], swept_through=to_epoch(now))
    (config.archive_root / STATUS_FILENAME).write_text(json.dumps({
        "generated_at": (now - timedelta(hours=2)).isoformat(),
        "channels": {"1": {"pending": 0, "waiting_retry": 5, "expired": 1, "horizon": None}},
    }))

    report = SystemStatusCollector(config).report(recognition=Reader(index))
    row = next(entry for entry in report["archive"]["channels"] if entry["channel"] == "1")

    # A dead daemon's last words must not keep dressing up the numbers.
    assert row["expired_segments"] is None and row["failing_segments"] is None
    assert row["backlog_segments"] == 1
    assert not any(check["title"] == "Segments are failing to archive" for check in report["checks"])
