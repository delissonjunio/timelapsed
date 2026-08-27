from datetime import timedelta

import pytest

from tests.conftest import BASE_TIME
from timelapsed.analysis.index import AnalysisIndex, to_epoch

BASE = to_epoch(BASE_TIME)


@pytest.fixture
def index(tmp_path):
    with AnalysisIndex(tmp_path / "index.sqlite3") as opened:
        yield opened


def test_schema_is_created_on_first_open(index):
    tables = {
        row["name"]
        for row in index.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"event", "detection", "identity", "signature", "plate", "watermark"} <= tables


def test_reopening_an_index_keeps_its_rows(tmp_path):
    path = tmp_path / "index.sqlite3"
    with AnalysisIndex(path) as first:
        first.open_event("1", "person", BASE, 0.9)
    with AnalysisIndex(path) as second:
        assert len(second.events()) == 1


def test_a_newer_schema_is_refused_rather_than_downgraded(tmp_path):
    """A rollback must not quietly rewrite an index a later build wrote."""
    path = tmp_path / "index.sqlite3"
    with AnalysisIndex(path) as opened:
        opened.connection.execute("PRAGMA user_version = 99")
        opened.connection.commit()

    with pytest.raises(RuntimeError, match="newer Timelapsed"):
        AnalysisIndex(path)


# --- watermarks ---


def test_watermark_round_trips_and_overwrites(index):
    assert index.watermark("1") is None
    index.set_watermark("1", BASE)
    assert index.watermark("1") == BASE
    index.set_watermark("1", BASE + 60)
    assert index.watermark("1") == BASE + 60
    assert index.watermarks() == {"1": BASE + 60}


# --- events ---


def test_extending_an_event_moves_its_end_and_keeps_the_peak_score(index):
    event_id = index.open_event("1", "person", BASE, 0.7)
    index.extend_event(event_id, BASE + 10, 0.9)
    index.extend_event(event_id, BASE + 20, 0.6)

    event = index.event(event_id)
    assert event.started_at == BASE
    assert event.ended_at == BASE + 20
    assert event.frame_count == 3
    assert event.peak_score == pytest.approx(0.9)


def test_events_are_filtered_by_channel_kind_and_identity(index):
    index.open_event("1", "person", BASE, 0.9)
    index.open_event("1", "vehicle", BASE, 0.9)
    index.open_event("2", "person", BASE, 0.9)

    assert len(index.events(channel="1")) == 2
    assert len(index.events(kind="person")) == 2
    assert len(index.events(channel="1", kind="vehicle")) == 1


def test_events_overlapping_the_window_edge_are_returned(index):
    """An event straddling the viewport edge still has to be drawn, or the
    timeline grows holes as you pan across it."""
    straddling = index.open_event("1", "vehicle", BASE - 3600, 0.9)
    index.extend_event(straddling, BASE + 3600, 0.9)

    found = index.events(channel="1", start=BASE - 60, end=BASE + 60)
    assert [event.id for event in found] == [straddling]


def test_events_entirely_outside_the_window_are_excluded(index):
    index.open_event("1", "person", BASE - 86400, 0.9)
    assert index.events(channel="1", start=BASE - 60, end=BASE + 60) == []


# --- activity ---


def test_activity_shades_every_bucket_an_event_spans(index):
    """A car present all afternoon should shade the afternoon, not one bar."""
    event_id = index.open_event("1", "vehicle", BASE, 0.9)
    index.extend_event(event_id, BASE + 3600, 0.9)

    counts = index.activity("1", BASE, BASE + 3600, buckets=4)
    assert counts["vehicle"] == [1, 1, 1, 1]
    assert counts["person"] == [0, 0, 0, 0]


def test_activity_separates_kinds_and_counts_overlaps(index):
    index.open_event("1", "person", BASE, 0.9)
    index.open_event("1", "person", BASE, 0.8)
    index.open_event("1", "vehicle", BASE, 0.9)

    counts = index.activity("1", BASE, BASE + 60, buckets=2)
    assert counts["person"][0] == 2
    assert counts["vehicle"][0] == 1


def test_activity_survives_a_zero_width_window(index):
    """The viewer can ask for a degenerate range while a drag is in flight."""
    index.open_event("1", "person", BASE, 0.9)
    counts = index.activity("1", BASE, BASE, buckets=10)
    assert len(counts["person"]) == 10


# --- identities ---


def test_adding_a_signature_bumps_the_sighting_count_and_last_seen(index):
    identity_id = index.create_identity("person", BASE)
    index.add_signature(identity_id, None, "body", b"\x00" * 16, 200.0, BASE + 500)

    identity = index.identities()[0]
    assert identity["id"] == identity_id
    assert identity["sightings"] == 1
    assert identity["last_seen"].startswith("2025-06-01T12:08")


def test_signatures_are_bounded_by_the_appearance_window(index):
    identity_id = index.create_identity("person", BASE)
    index.add_signature(identity_id, None, "body", b"\x01" * 16, 200.0, BASE)
    index.add_signature(identity_id, None, "body", b"\x02" * 16, 200.0, BASE + 86400)

    assert len(index.signatures("body")) == 2
    assert len(index.signatures("body", since=BASE + 3600)) == 1


def test_renaming_an_identity_reports_whether_it_existed(index):
    identity_id = index.create_identity("person", BASE)
    assert index.rename_identity(identity_id, "Someone") is True
    assert index.identities()[0]["name"] == "Someone"
    assert index.rename_identity(9999, "Nobody") is False


# --- plates ---


def test_plates_are_searchable_by_partial_text(index):
    event_id = index.open_event("1", "vehicle", BASE, 0.9)
    index.add_plate(event_id, "1", BASE, "ABC1D23", 0.95, 4, None)

    assert len(index.plates(text="ABC")) == 1
    assert len(index.plates(text="abc1d23")) == 1
    assert index.plates(text="ZZZ") == []


# --- retention ---


def test_pruning_events_cascades_to_their_detections(index):
    old = index.open_event("1", "person", BASE - 86400, 0.9)
    index.add_detection(old, "1", BASE - 86400, "person", 0.9, (0, 0, 10, 10))
    recent = index.open_event("1", "person", BASE, 0.9)
    index.add_detection(recent, "1", BASE, "person", 0.9, (0, 0, 10, 10))

    index.prune(detections_before=None, events_before=BASE - 3600)

    assert [event.id for event in index.events()] == [recent]
    remaining = index.connection.execute("SELECT COUNT(*) AS n FROM detection").fetchone()["n"]
    assert remaining == 1


def test_detections_can_be_pruned_while_their_events_survive(index):
    """Events are the durable record; the per-frame rows behind them are not."""
    event_id = index.open_event("1", "person", BASE - 86400, 0.9)
    index.add_detection(event_id, "1", BASE - 86400, "person", 0.9, (0, 0, 10, 10))

    removed, _ = index.prune(detections_before=BASE - 3600, events_before=None)

    assert removed == 1
    assert len(index.events()) == 1


def test_orphaned_crops_lists_only_referenced_paths(index):
    event_id = index.open_event("1", "person", BASE, 0.9)
    index.set_event_thumb(event_id, "event/20250601/1.jpg")
    assert index.orphaned_crops() == {"event/20250601/1.jpg"}
