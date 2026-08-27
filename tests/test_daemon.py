"""Covers the capture worker and the render scheduler."""
import multiprocessing
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from timelapsed.image_capture_library import ImageCaptureLibrary
from timelapsed.schema import CADENCES
from timelapsed.timelapsed import RenderScheduler, capture_continuously, pending_render_windows
from tests.conftest import BASE_TIME, requires_ffmpeg


class FakeCaptureAgent:
    """Stands in for the NVR. Counts calls and can be told to fail."""

    def __init__(self, payload=b"jpeg", fail_times=0):
        self.payload = payload
        self.fail_times = fail_times
        self.calls = []

    def capture_image(self, channel_id, resolution=None):
        self.calls.append((channel_id, resolution))
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("NVR unreachable")
        return self.payload, "jpg"


def _sleep_briefly(_seconds):
    time.sleep(0.001)


@pytest.fixture
def fast_worker(monkeypatch):
    """Run capture_continuously for a bounded number of cycles with no real sleeping."""

    def _run(config, library, agent, cycles: int, start: datetime = BASE_TIME):
        clock = {"now": start}
        remaining = {"cycles": cycles}

        def fake_now(tz=None):
            return clock["now"]

        def advance(_seconds):
            remaining["cycles"] -= 1
            if remaining["cycles"] <= 0:
                raise KeyboardInterrupt
            clock["now"] += config.capture_interval

        monkeypatch.setattr("timelapsed.timelapsed.time.sleep", advance)
        monkeypatch.setattr("timelapsed.timelapsed.signal.signal", lambda *a, **k: None)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["now"]

        monkeypatch.setattr("timelapsed.timelapsed.datetime", FrozenDatetime)

        submitted = []
        rendered: dict[str, list[datetime]] = {}

        def record(self, cadence, windows):
            for start_time, end_time in windows:
                submitted.append((cadence.name, self.channel_id, start_time, end_time))
                rendered.setdefault(cadence.name, []).append(start_time)
            return bool(windows)

        monkeypatch.setattr(RenderScheduler, "submit", record)
        # A real render leaves a video behind, and that video is how the next
        # pass knows the window is done. Nothing renders here, so stand in for it
        # or every rollover would re-submit every window it has already seen.
        monkeypatch.setattr(
            ImageCaptureLibrary,
            "rendered_window_starts",
            lambda self, channel_id, cadence_name: sorted(rendered.get(cadence_name, [])),
        )
        monkeypatch.setattr(RenderScheduler, "shutdown", lambda self, timeout=30.0: None)

        try:
            capture_continuously("1", agent, library, config)
        except KeyboardInterrupt:
            pass
        return submitted

    return _run


# --- capture loop ----------------------------------------------------------

def test_worker_stores_one_image_per_cycle(config, library, fast_worker):
    agent = FakeCaptureAgent()

    fast_worker(config, library, agent, cycles=5)

    assert len(agent.calls) == 5
    stored = library.retrieve_images_within("1", BASE_TIME - timedelta(days=1), BASE_TIME + timedelta(days=1))
    assert len(stored) == 5


def test_worker_passes_the_configured_resolution(config, library, fast_worker):
    agent = FakeCaptureAgent()

    fast_worker(config, library, agent, cycles=2)

    assert all(resolution == config.capture_resolution for _, resolution in agent.calls)


def test_worker_survives_capture_failures(config, library, fast_worker):
    """A dead NVR must not kill the worker; it keeps trying."""
    agent = FakeCaptureAgent(fail_times=3)

    fast_worker(config, library, agent, cycles=6)

    assert len(agent.calls) == 6
    stored = library.retrieve_images_within("1", BASE_TIME - timedelta(days=1), BASE_TIME + timedelta(days=1))
    assert len(stored) == 3  # the three that succeeded


def test_worker_does_not_render_immediately_on_start(config, library, fast_worker):
    """Restarting must not re-render every cadence; each waits for a real rollover."""
    submitted = fast_worker(config, library, FakeCaptureAgent(), cycles=3)

    assert submitted == []


def test_worker_renders_hourly_on_the_hour(config, library, fast_worker):
    config.capture_interval = timedelta(minutes=20)
    config.timelapse_min_frames = 1
    start = datetime(2025, 6, 3, 12, 30, tzinfo=timezone.utc)

    submitted = fast_worker(config, library, FakeCaptureAgent(), cycles=6, start=start)

    hourly = [entry for entry in submitted if entry[0] == "hourly"]
    assert len(hourly) == 2  # 13:10 and 14:10 within six 20-minute cycles
    assert all(end - begin == timedelta(hours=1) for _, _, begin, end in hourly)
    # Windows are clock-aligned, not "one hour back from whenever this fired".
    assert [begin for _, _, begin, _ in hourly] == [
        datetime(2025, 6, 3, 12, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 13, tzinfo=timezone.utc),
    ]


def test_worker_renders_daily_and_weekly_with_the_right_windows(config, library, fast_worker):
    config.capture_interval = timedelta(hours=8)
    config.timelapse_min_frames = 1
    start = datetime(2025, 6, 7, 12, 0, tzinfo=timezone.utc)  # Saturday

    submitted = fast_worker(config, library, FakeCaptureAgent(), cycles=10, start=start)

    windows = {name: end - begin for name, _, begin, end in submitted}
    assert windows["daily"] == timedelta(days=1)
    assert windows["weekly"] == timedelta(days=7)
    assert any(name == "weekly" for name, *_ in submitted)


def test_worker_rolls_the_day_over_on_the_configured_timezone(config, library, fast_worker):
    """In Sao Paulo (UTC-3) the daily must close at 03:00 UTC, not at 00:00 UTC."""
    config.render_timezone = ZoneInfo("America/Sao_Paulo")
    config.timelapse_cadences = [CADENCES["daily"]]
    config.capture_interval = timedelta(hours=1)
    config.timelapse_min_frames = 1
    start = datetime(2025, 6, 1, 22, 0, tzinfo=timezone.utc)  # 19:00 locally

    # 22:00, 23:00, 00:00, 01:00, 02:00, 03:00, 04:00 UTC
    submitted = fast_worker(config, library, FakeCaptureAgent(), cycles=7, start=start)

    daily = [entry for entry in submitted if entry[0] == "daily"]
    assert len(daily) == 1
    # Fired at the local midnight, and the window handed to the render is UTC.
    assert daily[0][3] == datetime(2025, 6, 2, 3, 0, tzinfo=timezone.utc)
    assert daily[0][2] == datetime(2025, 6, 1, 3, 0, tzinfo=timezone.utc)


def test_only_enabled_cadences_are_rendered(config, library, fast_worker):
    config.timelapse_cadences = [CADENCES["weekly"]]
    config.capture_interval = timedelta(hours=12)
    config.timelapse_min_frames = 1
    start = datetime(2025, 6, 7, 12, 0, tzinfo=timezone.utc)

    submitted = fast_worker(config, library, FakeCaptureAgent(), cycles=8, start=start)

    assert {name for name, *_ in submitted} == {"weekly"}


def test_worker_prunes_expired_images(config, library, fast_worker):
    config.image_retention = timedelta(hours=2)
    config.capture_interval = timedelta(minutes=30)
    start = datetime(2025, 6, 3, 12, 0, tzinfo=timezone.utc)
    for age_hours in range(1, 12):
        library.store_image("1", "jpg", b"old", start - timedelta(hours=age_hours))

    fast_worker(config, library, FakeCaptureAgent(), cycles=8, start=start)

    survivors = library.retrieve_images_within("1", start - timedelta(days=1), start + timedelta(days=1))
    oldest = min(path.stem for path in survivors)
    assert "20250603_10" <= oldest  # nothing older than the retention window survived


# --- pending windows -------------------------------------------------------

def _store_frames(library, channel_id, start, end, spacing=timedelta(minutes=1)):
    """Fill [start, end) with frames, one every `spacing`."""
    taken_at = start
    while taken_at < end:
        library.store_image(channel_id, "jpg", b"frame", taken_at)
        taken_at += spacing


def _hourly_windows(library, config, now, **kwargs):
    return pending_render_windows(library, config, "1", CADENCES["hourly"], now, **kwargs)


def test_pending_windows_offers_the_hour_that_just_closed(config, library):
    now = datetime(2025, 6, 3, 13, 0, 5, tzinfo=timezone.utc)
    _store_frames(library, "1", now - timedelta(hours=1), now)

    assert _hourly_windows(library, config, now) == [
        (datetime(2025, 6, 3, 12, tzinfo=timezone.utc), datetime(2025, 6, 3, 13, tzinfo=timezone.utc)),
    ]


def test_pending_windows_skips_hours_that_are_already_rendered(config, library, tmp_path):
    now = datetime(2025, 6, 3, 13, 0, 5, tzinfo=timezone.utc)
    _store_frames(library, "1", now - timedelta(hours=3), now)
    rendered = tmp_path / "rendered.mp4"
    rendered.write_bytes(b"mp4")
    # Stored a few seconds late, exactly as a real rollover render would be.
    library.store_timelapse(
        "1", rendered, "hourly",
        datetime(2025, 6, 3, 12, 0, 3, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 13, 0, 3, tzinfo=timezone.utc),
    )

    starts = [begin for begin, _ in _hourly_windows(library, config, now)]

    assert datetime(2025, 6, 3, 12, tzinfo=timezone.utc) not in starts
    assert starts == [
        datetime(2025, 6, 3, 11, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, tzinfo=timezone.utc),
    ]


def test_pending_windows_fills_a_gap_left_by_a_killed_render(config, library, tmp_path):
    """The hour a crash swallowed comes back on the next pass, newest first."""
    now = datetime(2025, 6, 3, 13, 0, 5, tzinfo=timezone.utc)
    _store_frames(library, "1", now - timedelta(hours=4), now)
    rendered = tmp_path / "rendered.mp4"
    rendered.write_bytes(b"mp4")
    for hour in (10, 11):  # 09:00 and 12:00 are the gaps
        library.store_timelapse(
            "1", rendered, "hourly",
            datetime(2025, 6, 3, hour, tzinfo=timezone.utc),
            datetime(2025, 6, 3, hour + 1, tzinfo=timezone.utc),
        )

    starts = [begin for begin, _ in _hourly_windows(library, config, now)]

    assert starts == [
        datetime(2025, 6, 3, 12, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 9, tzinfo=timezone.utc),
    ]


def test_pending_windows_ignores_hours_with_too_few_frames(config, library):
    config.timelapse_min_frames = 30
    now = datetime(2025, 6, 3, 13, 0, 5, tzinfo=timezone.utc)
    _store_frames(library, "1", now - timedelta(hours=1), now, spacing=timedelta(minutes=20))

    assert _hourly_windows(library, config, now) == []


def test_pending_windows_are_capped_and_bounded_by_retention(config, library):
    config.image_retention = timedelta(hours=6)
    now = datetime(2025, 6, 3, 13, 0, 5, tzinfo=timezone.utc)
    _store_frames(library, "1", now - timedelta(days=2), now, spacing=timedelta(minutes=5))

    assert len(_hourly_windows(library, config, now)) == 4  # MAX_WINDOWS_PER_RENDER
    # Retention is the real bound: nothing older than it can still be rendered.
    assert len(_hourly_windows(library, config, now, limit=100)) == 6


def test_pending_windows_never_offers_the_hour_still_in_progress(config, library):
    now = datetime(2025, 6, 3, 13, 30, tzinfo=timezone.utc)
    _store_frames(library, "1", datetime(2025, 6, 3, 13, tzinfo=timezone.utc), now)

    assert _hourly_windows(library, config, now) == []


# --- render scheduler ------------------------------------------------------

def _busy(seconds):
    time.sleep(seconds)


@requires_ffmpeg
def test_renders_are_serialised_by_the_shared_slot(config, library, populate_images):
    """One slot means one ffmpeg: the second channel waits rather than piling on."""
    populate_images(channel_id="1", count=40, end=BASE_TIME)
    populate_images(channel_id="2", count=40, end=BASE_TIME)
    slot = multiprocessing.BoundedSemaphore(1)
    window = [(BASE_TIME - timedelta(hours=1), BASE_TIME)]
    schedulers = [RenderScheduler(config, library, channel, slot) for channel in ("1", "2")]

    slot.acquire()  # Stand in for a render already in flight.
    try:
        for scheduler in schedulers:
            scheduler.submit(CADENCES["hourly"], window)
        time.sleep(2)
        assert library.rendered_window_starts("1", "hourly") == []
        assert library.rendered_window_starts("2", "hourly") == []
    finally:
        slot.release()

    for scheduler in schedulers:
        scheduler._processes["hourly"].join(timeout=60)
    assert len(library.rendered_window_starts("1", "hourly")) == 1
    assert len(library.rendered_window_starts("2", "hourly")) == 1


def test_scheduler_starts_a_process_per_render(config, library):
    """Runs the real entrypoint; with an empty library it returns without rendering."""
    scheduler = RenderScheduler(config, library, "1")

    assert scheduler.submit(CADENCES["hourly"], [(BASE_TIME - timedelta(hours=1), BASE_TIME)]) is True

    process = scheduler._processes["hourly"]
    process.join(timeout=30)
    assert process.exitcode == 0


def test_config_and_library_survive_pickling(config, library):
    """Workers are handed these across a process boundary, so they must pickle."""
    import pickle

    restored_config = pickle.loads(pickle.dumps(config))
    restored_library = pickle.loads(pickle.dumps(library))

    assert restored_config == config
    assert restored_library.root_path == library.root_path
    assert [c.name for c in restored_config.timelapse_cadences] == [
        c.name for c in config.timelapse_cadences
    ]
    # The rollover callables must survive too, not just their names.
    assert restored_config.timelapse_cadences[0].is_due(
        datetime(2025, 6, 1, 13, tzinfo=timezone.utc),
        datetime(2025, 6, 1, 12, tzinfo=timezone.utc),
    )


def test_scheduler_skips_a_cadence_that_is_still_rendering(config, library):
    scheduler = RenderScheduler(config, library, "1")
    scheduler._processes["hourly"] = multiprocessing.Process(target=_busy, args=(5,))
    scheduler._processes["hourly"].start()

    try:
        assert scheduler.submit(CADENCES["hourly"], [(BASE_TIME - timedelta(hours=1), BASE_TIME)]) is False
        # A different cadence is unaffected by the busy one.
        assert scheduler.submit(CADENCES["daily"], [(BASE_TIME - timedelta(days=1), BASE_TIME)]) is True
    finally:
        for process in scheduler._processes.values():
            process.terminate()
            process.join(timeout=5)


def test_scheduler_shutdown_terminates_stragglers(config, library):
    scheduler = RenderScheduler(config, library, "1")
    scheduler._processes["daily"] = multiprocessing.Process(target=_busy, args=(30,))
    scheduler._processes["daily"].start()

    scheduler.shutdown(timeout=0.2)

    scheduler._processes["daily"].join(timeout=5)
    assert not scheduler._processes["daily"].is_alive()


def test_pending_windows_floors_on_the_configured_timezone_but_returns_utc(config, library):
    """The period is the local day; the bounds handed back are UTC, because that
    is what the stored filenames are stamped with."""
    config.render_timezone = ZoneInfo("America/Sao_Paulo")
    config.timelapse_min_frames = 1
    now = datetime(2025, 6, 2, 3, 0, 5, tzinfo=timezone.utc)  # 00:00 locally
    _store_frames(library, "1", now - timedelta(days=1), now, spacing=timedelta(hours=1))

    windows = pending_render_windows(library, config, "1", CADENCES["daily"], now)

    # 03:00 UTC to 03:00 UTC is midnight to midnight in Sao Paulo.
    assert windows[0] == (
        datetime(2025, 6, 1, 3, tzinfo=timezone.utc),
        datetime(2025, 6, 2, 3, tzinfo=timezone.utc),
    )
    assert all(begin.tzinfo is timezone.utc and end.tzinfo is timezone.utc for begin, end in windows)


def test_pending_windows_on_utc_are_unchanged_by_the_default(config, library):
    """The default zone must leave the UTC-aligned behaviour exactly as it was."""
    config.timelapse_min_frames = 1
    now = datetime(2025, 6, 2, 0, 0, 5, tzinfo=timezone.utc)
    _store_frames(library, "1", now - timedelta(days=1), now, spacing=timedelta(hours=1))

    windows = pending_render_windows(library, config, "1", CADENCES["daily"], now)

    assert windows[0] == (
        datetime(2025, 6, 1, tzinfo=timezone.utc),
        datetime(2025, 6, 2, tzinfo=timezone.utc),
    )
