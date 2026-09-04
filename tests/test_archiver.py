"""The segment archiver: what it fetches, where it files it, what it deletes."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.conftest import BASE_TIME
from timelapsed.analysis.index import AnalysisIndex, to_epoch
from timelapsed.archiver import (
    ABANDON_AFTER_ATTEMPTS,
    ABANDONED_FILENAME,
    HORIZON_PROBE_MARGIN,
    RETRY_CEILING,
    RETRY_FIRST_DELAY,
    SETTLE,
    STATUS_FILENAME,
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

    assert name is not None
    assert name == uri_segment_name(path)  # stable
    assert name.startswith("dav-")
    assert "/" not in name and "[" not in name
    assert uri_segment_name("/mnt/dvr/whatever.idx") is None


# --- the archiver ---

class FakeClient:
    """Writes plausible MPEG-PS unless told to fail."""

    def search(self, channel, start, end):
        raise NotImplementedError("the archiver reads the mirror, never the device")

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
        assert name is not None
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
    built.client = client  # pyright: ignore[reportAttributeAccessIssue]
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
    assert archiver.client.calls == []  # pyright: ignore[reportAttributeAccessIssue]


def test_what_is_already_on_disk_is_not_refetched(archiver, index):
    started = NOW - timedelta(hours=2)
    ended = started + timedelta(minutes=2)
    seed_segment(index, "5", "ch05_001", started, ended)
    day = archiver.root / "5" / started.strftime("%Y%m%d")
    day.mkdir(parents=True)
    (day / segment_filename(started, ended, "ch05_001")).write_bytes(b"already here")
    archiver.scan()

    assert archiver.run_once(NOW) == 0
    assert archiver.client.calls == []  # pyright: ignore[reportAttributeAccessIssue]


def test_older_than_retention_is_never_fetched(index, tmp_path, fake_remux):
    """ch1 holds 205 days; a 30-day retention must not become a fetch/delete
    loop over the 175 days it would immediately throw away."""
    archiver = make_archiver(index, tmp_path / "archive", ["5"], retention=timedelta(days=7))
    seed_segment(index, "5", "ch05_ancient", NOW - timedelta(days=30), NOW - timedelta(days=30, minutes=-2))

    assert archiver.run_once(NOW) == 0
    assert archiver.client.calls == []  # pyright: ignore[reportAttributeAccessIssue]


def test_one_failure_neither_stops_the_pass_nor_hammers_the_next(archiver, index):
    seed_segment(index, "5", "ch05_gone", NOW - timedelta(hours=3), NOW - timedelta(hours=3, minutes=-1))
    seed_segment(index, "5", "ch05_fine", NOW - timedelta(hours=2), NOW - timedelta(hours=2, minutes=-1))
    archiver.client.failures.add("ch05_gone")

    assert archiver.run_once(NOW) == 1
    # The failure left nothing behind, staged or final.
    assert not list(archiver.root.glob("**/*ch05_gone*"))
    assert list((archiver.root / ".scratch").glob("*")) == []

    # An immediate next pass leaves the failure to its backoff.
    archiver.client.calls.clear()
    archiver.run_once(NOW)
    assert archiver.client.calls == []  # pyright: ignore[reportAttributeAccessIssue]


def test_a_failure_is_retried_once_its_backoff_elapses(archiver, index):
    started = NOW - timedelta(hours=3)
    seed_segment(index, "5", "ch05_flaky", started, started + timedelta(minutes=1))
    archiver.client.failures.add("ch05_flaky")
    archiver.run_once(NOW)
    archiver.client.calls.clear()

    # The device heals; the archiver comes back for the segment on its own.
    archiver.client.failures.clear()
    archiver.run_once(NOW + RETRY_FIRST_DELAY + timedelta(seconds=1))
    assert archiver.client.calls == ["ch05_flaky"]  # pyright: ignore[reportAttributeAccessIssue]
    assert len(list(archiver.root.glob("**/*ch05_flaky*"))) == 1


def test_repeated_failures_back_off_exponentially(archiver, index):
    started = NOW - timedelta(hours=3)
    seed_segment(index, "5", "ch05_stuck", started, started + timedelta(minutes=1))
    archiver.client.failures.add("ch05_stuck")

    archiver.run_once(NOW)  # first failure: retry after one delay
    second_attempt_at = NOW + RETRY_FIRST_DELAY + timedelta(seconds=1)
    archiver.run_once(second_attempt_at)  # second failure: the delay doubles
    archiver.client.calls.clear()

    archiver.run_once(second_attempt_at + RETRY_FIRST_DELAY + timedelta(seconds=1))
    assert archiver.client.calls == []  # pyright: ignore[reportAttributeAccessIssue]
    archiver.run_once(second_attempt_at + 2 * RETRY_FIRST_DELAY + timedelta(seconds=2))
    assert archiver.client.calls == ["ch05_stuck"]  # pyright: ignore[reportAttributeAccessIssue]


def test_an_expired_segment_sheds_its_failure_history(archiver, index):
    started = NOW - timedelta(days=20)
    seed_segment(index, "5", "ch05_doomed", started, started + timedelta(minutes=2))
    archiver.client.failures.add("ch05_doomed")

    archiver.run_once(NOW)
    assert "ch05_doomed" in archiver._failed
    # The device recycles it before any retry succeeds: the history goes too.
    archiver.client.horizons["5"] = NOW - timedelta(days=2)
    archiver.refresh_horizons()
    archiver.run_once(NOW + RETRY_FIRST_DELAY * 2)
    assert "ch05_doomed" not in archiver._failed


def test_the_backlog_gauges_count_segments_waiting_out_a_backoff(archiver, index, monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        "timelapsed.telemetry.record_metric",
        lambda name, value: recorded.__setitem__(name, value),
    )
    started = NOW - timedelta(days=2)
    seed_segment(index, "5", "ch05_refused", started, started + timedelta(minutes=1))
    archiver.client.failures.add("ch05_refused")
    archiver.run_once(NOW)

    archiver.run_once(NOW)  # deferred now, not queued -- but still owed
    assert recorded["Custom/archiver/backlog_segments"] == 1
    assert recorded["Custom/archiver/deferred_segments"] == 1
    age_days = (datetime.now(tz=timezone.utc) - started).total_seconds() / 86400
    assert recorded["Custom/archiver/backlog_oldest_days"] == pytest.approx(age_days, rel=0.01)


def test_the_status_file_tells_the_page_what_the_tree_cannot(archiver, index):
    gone_start = NOW - timedelta(days=20)
    seed_segment(index, "5", "ch05_gone", gone_start, gone_start + timedelta(minutes=2))
    seed_segment(index, "5", "ch05_refused", NOW - timedelta(hours=3), NOW - timedelta(hours=2))
    archiver.client.horizons["5"] = NOW - timedelta(days=2)
    archiver.client.failures.add("ch05_refused")

    archiver.run_once(NOW)
    payload = json.loads((archiver.root / STATUS_FILENAME).read_text())

    assert payload["channels"]["5"]["expired"] == 1
    assert payload["channels"]["5"]["waiting_retry"] == 1
    assert payload["channels"]["5"]["pending"] == 0
    assert payload["channels"]["5"]["horizon"] == (NOW - timedelta(days=2)).isoformat()
    assert payload["channels"]["6"] == {
        "pending": 0, "waiting_retry": 0, "abandoned": 0, "expired": 0, "horizon": None,
    }


def abandon(archiver, name: str, starting_at):
    """Fail `name` through every backoff until the archiver writes it off."""
    archiver.client.failures.add(name)
    at = starting_at
    for _ in range(ABANDON_AFTER_ATTEMPTS):
        archiver.run_once(at)
        at += RETRY_CEILING + timedelta(seconds=1)  # far past any backoff
    return at


def test_persistent_failures_are_written_off_after_the_limit(archiver, index):
    started = NOW - timedelta(hours=6)
    seed_segment(index, "5", "ch05_dead", started, started + timedelta(minutes=1))
    at = abandon(archiver, "ch05_dead", NOW)
    archiver.client.calls.clear()

    # Written off: never asked for again, and out of the failure ledger.
    archiver.run_once(at)
    assert archiver.client.calls == []  # pyright: ignore[reportAttributeAccessIssue]
    assert "ch05_dead" not in archiver._failed
    tombstones = json.loads((archiver.root / ABANDONED_FILENAME).read_text())
    assert tombstones["ch05_dead"]["channel"] == "5"
    assert tombstones["ch05_dead"]["attempts"] == ABANDON_AFTER_ATTEMPTS


def test_write_offs_survive_a_restart(index, tmp_path, fake_remux):
    first = make_archiver(index, tmp_path / "archive", ["5", "6"])
    started = NOW - timedelta(hours=6)
    seed_segment(index, "5", "ch05_dead", started, started + timedelta(minutes=1))
    at = abandon(first, "ch05_dead", NOW)

    reborn = make_archiver(index, tmp_path / "archive", ["5", "6"])
    reborn.scan()
    reborn.run_once(at)
    assert reborn.client.calls == []  # pyright: ignore[reportAttributeAccessIssue]


def test_a_written_off_segment_expiring_clears_its_tombstone(archiver, index):
    started = NOW - timedelta(days=20)
    seed_segment(index, "5", "ch05_dead", started, started + timedelta(minutes=2))
    at = abandon(archiver, "ch05_dead", NOW)

    # The device's own retention catches up with the write-off.
    archiver.client.horizons["5"] = NOW - timedelta(days=2)
    archiver.refresh_horizons()
    archiver.run_once(at)
    tombstones = json.loads((archiver.root / ABANDONED_FILENAME).read_text())
    assert "ch05_dead" not in tombstones


def test_written_off_segments_leave_the_backlog_gauges(archiver, index, monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        "timelapsed.telemetry.record_metric",
        lambda name, value: recorded.__setitem__(name, value),
    )
    started = NOW - timedelta(hours=6)
    seed_segment(index, "5", "ch05_dead", started, started + timedelta(minutes=1))
    at = abandon(archiver, "ch05_dead", NOW)

    archiver.run_once(at)
    assert recorded["Custom/archiver/backlog_segments"] == 0
    assert recorded["Custom/archiver/backlog_oldest_days"] == 0.0
    assert recorded["Custom/archiver/abandoned_segments"] == 1
    payload = json.loads((archiver.root / STATUS_FILENAME).read_text())
    assert payload["channels"]["5"]["abandoned"] == 1


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
