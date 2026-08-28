"""The segment archiver: what it fetches, where it files it, what it deletes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.conftest import BASE_TIME
from timelapsed.analysis.index import AnalysisIndex, to_epoch
from timelapsed.archiver import (
    SETTLE,
    SegmentArchiver,
    parse_segment_filename,
    segment_filename,
    uri_segment_name,
)
from timelapsed.nvr_footage import MPEG_PS_MAGIC

NOW = BASE_TIME
BASE = to_epoch(BASE_TIME)


# --- naming ---

def test_segment_filenames_round_trip_through_the_device_name():
    """The device name is full of underscores; the split must survive it."""
    started = datetime(2026, 8, 28, 16, 3, 3, tzinfo=timezone.utc)
    ended = datetime(2026, 8, 28, 16, 7, 35, tzinfo=timezone.utc)

    filename = segment_filename(started, ended, "ch05_00000000033000801")

    assert filename == "20260828T160303Z_20260828T160735Z_ch05_00000000033000801.mp4"
    assert parse_segment_filename(Path(filename).stem) == (
        started, ended, "ch05_00000000033000801"
    )


def test_the_segment_name_comes_from_the_uri():
    uri = "rtsp://nvr/Streaming/tracks/501?starttime=x&name=ch05_007&size=99"
    assert uri_segment_name(uri) == "ch05_007"
    assert uri_segment_name("rtsp://nvr/Streaming/tracks/501") is None


# --- the archiver ---

class FakeClient:
    """Writes plausible MPEG-PS unless told to fail."""

    def __init__(self):
        self.calls = []
        self.failures = set()  # segment names that raise

    def download(self, playback_uri: str, destination: Path, deadline_seconds: float) -> int:
        name = uri_segment_name(playback_uri)
        self.calls.append(name)
        if name in self.failures:
            destination.write_bytes(b"partial")
            raise TimeoutError("scripted failure")
        body = MPEG_PS_MAGIC + name.encode()
        destination.write_bytes(body)
        return len(body)


@pytest.fixture
def fake_remux(monkeypatch):
    """Stands in for ffmpeg: 'remuxes' by copying, so tests need no real PS."""

    def run(command, check=True, capture_output=True, timeout=None):
        source = Path(command[command.index("-i") + 1])
        Path(command[-1]).write_bytes(b"mp4:" + source.read_bytes())

    monkeypatch.setattr("timelapsed.archiver.subprocess.run", run)


def seed_segment(index, channel, name, started_at, ended_at, size=1000):
    index.record_segments(
        channel,
        [(
            to_epoch(started_at), to_epoch(ended_at), size,
            f"rtsp://nvr/Streaming/tracks/{channel}01?starttime=x&endtime=y&name={name}&size={size}",
        )],
        swept_through=to_epoch(NOW),
    )


@pytest.fixture
def index(tmp_path):
    with AnalysisIndex(tmp_path / "index.sqlite3") as opened:
        yield opened


@pytest.fixture
def archiver(index, tmp_path, fake_remux):
    return SegmentArchiver(
        client=FakeClient(),
        index=index,
        root=tmp_path / "archive",
        channels=["5", "6"],
        retention=None,
        minimum_free_bytes=0,
    )


def test_settled_segments_are_fetched_into_the_day_layout(archiver, index):
    started = NOW - timedelta(hours=2)
    seed_segment(index, "5", "ch05_001", started, started + timedelta(minutes=2))

    assert archiver.run_once(NOW) == 1

    expected = (
        archiver.root / "5" / started.strftime("%Y%m%d")
        / segment_filename(started, started + timedelta(minutes=2), "ch05_001")
    )
    assert expected.read_bytes().startswith(b"mp4:")
    # Nothing staged left behind.
    assert list((archiver.root / ".scratch").glob("*")) == []


def test_fetches_run_oldest_first_across_channels(archiver, index):
    seed_segment(index, "5", "ch05_new", NOW - timedelta(hours=1), NOW - timedelta(minutes=58))
    seed_segment(index, "6", "ch06_old", NOW - timedelta(days=3), NOW - timedelta(days=3, minutes=-2))

    archiver.run_once(NOW)

    assert archiver.client.calls == ["ch06_old", "ch05_new"]


def test_a_segment_still_inside_the_settle_margin_waits(archiver, index):
    """Its end is still walking forward; fetching now would archive a
    truncation under a name the archiver then considers done."""
    seed_segment(index, "5", "ch05_open", NOW - SETTLE, NOW - timedelta(minutes=5))

    assert archiver.run_once(NOW) == 0
    assert archiver.client.calls == []


def test_what_is_already_on_disk_is_not_refetched(archiver, index):
    started = NOW - timedelta(hours=2)
    ended = started + timedelta(minutes=2)
    seed_segment(index, "5", "ch05_001", started, ended)
    day = archiver.root / "5" / started.strftime("%Y%m%d")
    day.mkdir(parents=True)
    (day / segment_filename(started, ended, "ch05_001")).write_bytes(b"already here")
    archiver.scan()

    assert archiver.run_once(NOW) == 0
    assert archiver.client.calls == []


def test_older_than_retention_is_never_fetched(index, tmp_path, fake_remux):
    """ch1 holds 205 days; a 30-day retention must not become a fetch/delete
    loop over the 175 days it would immediately throw away."""
    archiver = SegmentArchiver(
        client=FakeClient(), index=index, root=tmp_path / "archive",
        channels=["5"], retention=timedelta(days=7), minimum_free_bytes=0,
    )
    seed_segment(index, "5", "ch05_ancient", NOW - timedelta(days=30), NOW - timedelta(days=30, minutes=-2))

    assert archiver.run_once(NOW) == 0
    assert archiver.client.calls == []


def test_one_failure_neither_stops_the_pass_nor_is_retried(archiver, index):
    seed_segment(index, "5", "ch05_gone", NOW - timedelta(hours=3), NOW - timedelta(hours=3, minutes=-1))
    seed_segment(index, "5", "ch05_fine", NOW - timedelta(hours=2), NOW - timedelta(hours=2, minutes=-1))
    archiver.client.failures.add("ch05_gone")

    assert archiver.run_once(NOW) == 1
    # The failure left nothing behind, staged or final.
    assert not list(archiver.root.glob("**/*ch05_gone*"))
    assert list((archiver.root / ".scratch").glob("*")) == []

    # The next pass does not hammer the segment the device has expired.
    archiver.client.calls.clear()
    archiver.run_once(NOW)
    assert archiver.client.calls == []


def test_reclaim_ages_out_files_and_their_empty_days(index, tmp_path, fake_remux):
    archiver = SegmentArchiver(
        client=FakeClient(), index=index, root=tmp_path / "archive",
        channels=["5"], retention=timedelta(days=7), minimum_free_bytes=0,
    )
    old_start = NOW - timedelta(days=10)
    day = archiver.root / "5" / old_start.strftime("%Y%m%d")
    day.mkdir(parents=True)
    old_file = day / segment_filename(old_start, old_start + timedelta(minutes=1), "ch05_old")
    old_file.write_bytes(b"aged")
    archiver.scan()

    removed = archiver.reclaim(NOW)

    assert removed == 1
    assert not old_file.exists()
    assert not day.exists()


def test_the_free_space_floor_drops_the_oldest_whole_day(index, tmp_path, fake_remux, monkeypatch):
    archiver = SegmentArchiver(
        client=FakeClient(), index=index, root=tmp_path / "archive",
        channels=["5"], retention=None, minimum_free_bytes=100,
    )
    for label, days_ago in (("ch05_older", 5), ("ch05_newer", 1)):
        started = NOW - timedelta(days=days_ago)
        day = archiver.root / "5" / started.strftime("%Y%m%d")
        day.mkdir(parents=True)
        (day / segment_filename(started, started + timedelta(minutes=1), label)).write_bytes(b"x")
    archiver.scan()

    # Below the floor once, then healthy: exactly one day should go.
    readings = iter([50, 500])
    monkeypatch.setattr(archiver, "_free_bytes", lambda: next(readings))

    archiver.reclaim(NOW)

    surviving = [path.name for path in archiver.root.glob("*/*")]
    assert surviving == [(NOW - timedelta(days=1)).strftime("%Y%m%d")]
