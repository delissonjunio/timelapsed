"""Covers the capture worker and the render scheduler."""
import multiprocessing
import time
from datetime import datetime, timedelta, timezone

import pytest

from timelapsed.schema import CADENCES
from timelapsed.timelapsed import RenderScheduler, capture_continuously
from tests.conftest import BASE_TIME


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
        monkeypatch.setattr(
            RenderScheduler,
            "submit",
            lambda self, cadence, start_time, end_time: submitted.append(
                (cadence.name, self.channel_id, start_time, end_time)
            ),
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
    start = datetime(2025, 6, 3, 12, 30, tzinfo=timezone.utc)

    submitted = fast_worker(config, library, FakeCaptureAgent(), cycles=6, start=start)

    hourly = [entry for entry in submitted if entry[0] == "hourly"]
    assert len(hourly) == 2  # 13:10 and 14:10 within six 20-minute cycles
    assert all(end - begin == timedelta(hours=1) for _, _, begin, end in hourly)


def test_worker_renders_daily_and_weekly_with_the_right_windows(config, library, fast_worker):
    config.capture_interval = timedelta(hours=8)
    start = datetime(2025, 6, 7, 12, 0, tzinfo=timezone.utc)  # Saturday

    submitted = fast_worker(config, library, FakeCaptureAgent(), cycles=10, start=start)

    windows = {name: end - begin for name, _, begin, end in submitted}
    assert windows["daily"] == timedelta(days=1)
    assert windows["weekly"] == timedelta(days=7)
    assert any(name == "weekly" for name, *_ in submitted)


def test_only_enabled_cadences_are_rendered(config, library, fast_worker):
    config.timelapse_cadences = [CADENCES["weekly"]]
    config.capture_interval = timedelta(hours=12)
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


# --- render scheduler ------------------------------------------------------

def _busy(seconds):
    time.sleep(seconds)


def test_scheduler_starts_a_process_per_render(config, library):
    """Runs the real entrypoint; with an empty library it returns without rendering."""
    scheduler = RenderScheduler(config, library, "1")

    assert scheduler.submit(CADENCES["hourly"], BASE_TIME - timedelta(hours=1), BASE_TIME) is True

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
        assert scheduler.submit(CADENCES["hourly"], BASE_TIME - timedelta(hours=1), BASE_TIME) is False
        # A different cadence is unaffected by the busy one.
        assert scheduler.submit(CADENCES["daily"], BASE_TIME - timedelta(days=1), BASE_TIME) is True
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
