"""The footage lane with a replica behind it: every run says what became of it."""
import json
import threading
from datetime import timedelta

import pytest

from tests.conftest import BASE_TIME
from timelapsed.analysis.index import AnalysisIndex, to_epoch
from timelapsed.archiver import ABANDONED_FILENAME, STATUS_FILENAME, segment_filename, segment_key
from timelapsed.catalogue import ArchiveCatalogue, footage_status_runs
from timelapsed.web import build_server

BASE = to_epoch(BASE_TIME)


def row(name: str, start: int, end: int, size: int = 1000) -> dict:
    from timelapsed.analysis.index import from_epoch
    return {
        "starts": from_epoch(start).isoformat(), "finishes": from_epoch(end).isoformat(),
        "size_bytes": size, "playback_uri": f"rtsp://nvr/tracks/101?name={name}&size={size}",
    }


def key(name: str, start: int) -> str:
    from timelapsed.analysis.index import from_epoch
    return segment_key(name, from_epoch(start))


# --- the classifier ------------------------------------------------------------

def test_runs_split_where_the_replicas_answer_changes():
    rows = [row("a", BASE, BASE + 10), row("b", BASE + 12, BASE + 20), row("c", BASE + 22, BASE + 30),
            row("d", BASE + 32, BASE + 40), row("e", BASE + 42, BASE + 50)]

    runs = footage_status_runs(
        rows, max_gap=5,
        archived={key("a", BASE), key("b", BASE + 12)},
        abandoned={key("d", BASE + 32)},
        waiting={key("c", BASE + 22)},
        horizon=None,
    )

    # a and b merge (same status, 2 s apart); each change of status is a new run.
    assert [(run["status"], run["segments"]) for run in runs] == [
        ("archived", 2), ("failing", 1), ("abandoned", 1), ("pending", 1),
    ]
    assert runs[0]["size_bytes"] == 2000
    assert runs[0]["starts"] == "2025-06-01T12:00:00+00:00"
    assert runs[0]["finishes"] == "2025-06-01T12:00:20+00:00"


def test_a_gap_splits_a_run_even_within_one_status():
    rows = [row("a", BASE, BASE + 10), row("b", BASE + 100, BASE + 110)]

    runs = footage_status_runs(rows, max_gap=5, archived=set(), abandoned=set(), waiting=set(), horizon=None)

    assert [(run["status"], run["segments"]) for run in runs] == [("pending", 1), ("pending", 1)]


def test_recycled_footage_is_expired_whatever_else_was_recorded_about_it():
    from timelapsed.analysis.index import from_epoch
    old, recent = BASE - 10 * 86400, BASE
    rows = [row("old", old, old + 60), row("new", recent, recent + 60)]

    # The horizon sits well after the old segment: the device has recycled it,
    # so its write-off is moot -- expired wins over abandoned.
    runs = footage_status_runs(
        rows, max_gap=5, archived=set(), abandoned={key("old", old)}, waiting=set(),
        horizon=from_epoch(BASE - 86400),
    )

    assert [run["status"] for run in runs] == ["expired", "pending"]


def test_the_same_name_on_a_different_start_is_a_different_segment():
    rows = [row("reused", BASE - 200 * 86400, BASE - 200 * 86400 + 60), row("reused", BASE, BASE + 60)]

    runs = footage_status_runs(
        rows, max_gap=5, archived={key("reused", BASE - 200 * 86400)},
        abandoned=set(), waiting=set(), horizon=None,
    )

    assert [run["status"] for run in runs] == ["archived", "pending"]


# --- through the catalogue and the endpoint --------------------------------------

@pytest.fixture
def replica(config, tmp_path):
    """A mirror of five segments on channel 1 and a replica that holds one of
    them, gave up on another, is failing a third, and whose device has
    recycled a fourth."""
    config.archive_root = tmp_path / "archive"
    day = config.archive_root / "1" / BASE_TIME.strftime("%Y%m%d")
    day.mkdir(parents=True)
    with AnalysisIndex(config.analysis_index_path) as index:
        index.record_segments("1", [
            (BASE - 5 * 86400, BASE - 5 * 86400 + 60, 100, "rtsp://nvr/tracks/101?name=gone&size=100"),
            (BASE, BASE + 60, 100, "rtsp://nvr/tracks/101?name=held&size=100"),
            (BASE + 120, BASE + 180, 100, "rtsp://nvr/tracks/101?name=refused&size=100"),
            (BASE + 240, BASE + 300, 100, "rtsp://nvr/tracks/101?name=flaky&size=100"),
            (BASE + 360, BASE + 420, 100, "rtsp://nvr/tracks/101?name=queued&size=100"),
        ], swept_through=BASE + 1000)
    (day / segment_filename(BASE_TIME, BASE_TIME + timedelta(seconds=60), "held")).write_bytes(b"mp4")
    (config.archive_root / ABANDONED_FILENAME).write_text(json.dumps({
        key("refused", BASE + 120): {"channel": "1", "attempts": 5},
    }))
    from datetime import datetime, timezone
    (config.archive_root / STATUS_FILENAME).write_text(json.dumps({
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "channels": {"1": {
            "pending": 1, "waiting_retry": 1, "abandoned": 1, "expired": 1,
            "horizon": (BASE_TIME - timedelta(days=1)).isoformat(),
            "waiting": [key("flaky", BASE + 240)],
        }},
    }))
    return config


@pytest.fixture
def base_url(replica):
    server = build_server(replica)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_catalogue_reads_every_ledger_it_needs(replica):
    from timelapsed.analysis.index import from_epoch
    catalogue = ArchiveCatalogue(replica.archive_root)

    assert catalogue.keys("1", from_epoch(BASE - 3600), from_epoch(BASE + 3600)) == {key("held", BASE)}
    assert catalogue.keys("1", from_epoch(BASE + 3600), from_epoch(BASE + 7200)) == set()
    assert catalogue.abandoned_keys("1") == {key("refused", BASE + 120)}
    assert catalogue.abandoned_keys("2") == set()


def test_the_footage_lane_says_what_became_of_each_run(base_url):
    import urllib.request
    with urllib.request.urlopen(
        f"{base_url}/api/footage?channel=1&start={BASE - 6 * 86400}&end={BASE + 1000}"
    ) as response:
        runs = json.loads(response.read())

    assert [(run["status"], run["segments"]) for run in runs] == [
        ("expired", 1), ("archived", 1), ("abandoned", 1), ("failing", 1), ("pending", 1),
    ]
    assert "playback_uri" not in runs[0]


def test_without_a_replica_the_lane_is_the_plain_mirror(replica):
    """No [archive] root: runs merge on time alone and carry no status, exactly
    as before the replica existed."""
    import urllib.request
    replica.archive_root = None
    server = build_server(replica)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://{server.server_address[0]}:{server.server_address[1]}"
        with urllib.request.urlopen(f"{url}/api/footage?channel=1&start={BASE}&end={BASE + 1000}") as response:
            runs = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # A 1,000 s window merges at 1 s, so the four rows a minute apart stay
    # four runs -- plain ones, with no status to speak of.
    assert [run["segments"] for run in runs] == [1, 1, 1, 1]
    assert all("status" not in run for run in runs)
