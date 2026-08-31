"""The segment archiver: what it fetches, where it files it, what it deletes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.conftest import BASE_TIME
from timelapsed.analysis.index import AnalysisIndex, to_epoch
from timelapsed.archiver import (
    HORIZON_PROBE_MARGIN,
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


def test_a_dahua_file_path_gets_a_stable_derived_name():
    """Dahua search results carry a raw on-disk path, not a URI with a name=.
    The derived name must be filesystem-safe and stable across sweeps."""
    path = "/mnt/dvr/2026-08-28/0/dav/00/0/1/98673/00.14.57-00.15.31[M][0@0][0].dav"

    name = uri_segment_name(path)

    assert name == uri_segment_name(path)  # stable
    assert name.startswith("dav-")
    assert "/" not in name and "[" not in name
    assert uri_segment_name("/mnt/dvr/whatever.idx") is None


# --- the archiver ---

class FakeClient:
    """Writes plausible MPEG-PS unless told to fail."""

    def __init__(self):
        self.calls = []
        self.failures = set()  # segment names that raise
        self.horizons = {}  # channel -> oldest-held start, or an Exception
        self.probes = []  # every (channel, start, end) the archiver asked

    def oldest_recording(self, channel, start, end):
        self.probes.append((channel, start, end))
        answer = self.horizons.get(channel)
        if isinstance(answer, Exception):
            raise answer
        return answer

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


def make_archiver(index, root, channels, retention=None, minimum_free_bytes=0):
    # One fake behind every channel, exposed as .client so the tests can script
    # it without caring that the archiver routes per channel now.
    client = FakeClient()
    built = SegmentArchiver(
        clients={channel: client for channel in channels},
        index=index,
        root=root,
        channels=channels,
        retention=retention,
        minimum_free_bytes=minimum_free_bytes,
    )
    built.client = client
    return built


@pytest.fixture
def archiver(index, tmp_path, fake_remux):
    return make_archiver(index, tmp_path / "archive", ["5", "6"])


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
    archiver = make_archiver(index, tmp_path / "archive", ["5"], retention=timedelta(days=7))
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


# --- the device's retention horizon ---

def test_segments_behind_the_device_horizon_are_skipped_not_failed(archiver, index):
    """The device recycles oldest footage first; a mirror row behind its
    horizon is unfetchable and skipping it is not a failure -- when the
    horizon later goes away, the segment is simply asked for again."""
    gone_start = NOW - timedelta(days=20)
    live_start = NOW - timedelta(hours=2)
    seed_segment(index, "5", "ch05_gone", gone_start, gone_start + timedelta(minutes=2))
    seed_segment(index, "5", "ch05_live", live_start, live_start + timedelta(minutes=2))
    archiver.client.horizons["5"] = NOW - timedelta(days=2)

    assert archiver.run_once(NOW) == 1
    assert archiver.client.calls == ["ch05_live"]

    # Not a tombstone: with the horizon gone the segment is fetched after all.
    archiver.client.horizons.clear()
    archiver.refresh_horizons()
    archiver.client.calls.clear()
    archiver.run_once(NOW)
    assert archiver.client.calls == ["ch05_gone"]


def test_the_horizon_keeps_slack_toward_fetching(archiver, index):
    """Wrongly fetching a doomed segment costs a fast failure; wrongly
    skipping a live one loses footage. Only clearly-behind is skipped."""
    horizon = NOW - timedelta(days=2)
    near_start = horizon - timedelta(minutes=30)  # behind, but inside the slack
    seed_segment(index, "5", "ch05_near", near_start, near_start + timedelta(minutes=2))
    archiver.client.horizons["5"] = horizon

    archiver.run_once(NOW)

    assert archiver.client.calls == ["ch05_near"]


def test_a_failed_probe_means_no_filtering(archiver, index):
    """No horizon is the safe state: the archiver fetches as it always has,
    and a doomed fetch fails fast on the magic check."""
    old_start = NOW - timedelta(days=20)
    seed_segment(index, "5", "ch05_old", old_start, old_start + timedelta(minutes=2))
    archiver.client.horizons["5"] = RuntimeError("device away")

    assert archiver.run_once(NOW) == 1
    assert archiver.client.calls == ["ch05_old"]


def test_the_probe_window_opens_before_the_mirrors_oldest_row(archiver, index):
    """The mirror never forgets, so nothing the device still holds can start
    before the mirror's oldest row; the margin absorbs clock translation."""
    old_start = NOW - timedelta(days=20)
    seed_segment(index, "5", "ch05_old", old_start, old_start + timedelta(minutes=2))

    archiver.refresh_horizons()

    (channel, start, end) = archiver.client.probes[0]
    assert channel == "5"
    assert start == old_start - HORIZON_PROBE_MARGIN
    assert end > start


def test_the_horizon_is_reprobed_mid_pass_and_newly_expired_dropped(archiver, index, monkeypatch):
    """A backfill pass runs for hours while the recycle frontier advances;
    a queued segment the device expired mid-pass is dropped, not fetched."""
    monkeypatch.setattr("timelapsed.archiver.HORIZON_REFRESH_SECONDS", -1)
    first = NOW - timedelta(days=10)
    second = NOW - timedelta(days=8)
    seed_segment(index, "5", "ch05_first", first, first + timedelta(minutes=2))
    seed_segment(index, "5", "ch05_second", second, second + timedelta(minutes=2))
    # Probed at the pass start, before the first fetch, before the second: the
    # frontier jumps past the second segment while the first is downloading.
    horizons = iter([
        first - timedelta(days=1),
        first - timedelta(days=1),
        second + timedelta(days=1),
    ])
    archiver.client.oldest_recording = lambda channel, start, end: next(horizons)

    assert archiver.run_once(NOW) == 1
    assert archiver.client.calls == ["ch05_first"]
    assert "ch05_second" not in archiver._failed


def test_reclaim_ages_out_files_and_their_empty_days(index, tmp_path, fake_remux):
    archiver = make_archiver(index, tmp_path / "archive", ["5"], retention=timedelta(days=7))
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
    archiver = make_archiver(index, tmp_path / "archive", ["5"], minimum_free_bytes=100)
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
