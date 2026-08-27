"""The one-off that pools plate rows an older build wrote one per sighting."""
import json

import pytest

from tests.conftest import BASE_TIME
from timelapsed.analysis.backfill import apply, plan
from timelapsed.analysis.index import AnalysisIndex, from_epoch, to_epoch

BASE = to_epoch(BASE_TIME)
GAP = 30 * 60


@pytest.fixture
def index(tmp_path):
    with AnalysisIndex(tmp_path / "index.sqlite3") as opened:
        yield opened


def legacy(index, channel, at, text, votes=1, confidence=0.9, crop_path=None):
    """A plate row as the build before pooling wrote it: no box, no tally."""
    event_id = index.open_event(channel, "vehicle", at, 0.9)
    return index.add_plate(event_id, channel, at, text, confidence, votes, crop_path)


def test_a_run_of_near_identical_rows_becomes_one_voted_plate(index):
    """The reported case: one car, six rows, three different readings of it."""
    for step, text in enumerate(["JSY1H73", "JSY1H73", "JSY1H73", "JSY1H33", "JSY1H23", "JSY1H23"]):
        legacy(index, "5", BASE + step * 20, text)

    apply(index, plan(index, GAP, 2))

    plates = index.plates()
    assert len(plates) == 1
    assert plates[0]["text"] == "JSY1H73"  # what the most reads stood behind
    assert plates[0]["reads"] == 6
    assert plates[0]["votes"] == 3


def test_pooling_votes_per_position_rather_than_picking_a_winner(index):
    """No single row here is right; the majority of each column is."""
    for step, text in enumerate(["ABC1D23", "ABC1D28", "ABC1D93"]):
        legacy(index, "5", BASE + step * 20, text)

    apply(index, plan(index, GAP, 2))
    assert index.plates()[0]["text"] == "ABC1D23"


def test_the_pooled_row_spans_from_first_sighting_to_last(index):
    for step in range(4):
        legacy(index, "5", BASE + step * 60, "ABC1D23")

    apply(index, plan(index, GAP, 2))

    plate = index.plates()[0]
    assert plate["seen_at"] == from_epoch(BASE).isoformat()
    assert plate["last_seen_at"] == from_epoch(BASE + 180).isoformat()


def test_one_car_read_badly_all_evening_comes_out_as_one_row(index):
    """Matching against a group's dominant text rather than its last row. Real
    data from this deployment: one car parked in front of camera 5, read every
    ten seconds, hardly ever the same way twice. Chaining off the last row let
    each group wander two characters at a step and split the evening into
    several overlapping runs.
    """
    seen = ["JSY1M23", "JSY1M73", "JSY1M21", "JSY1M23", "JSY1M71", "JSY1M23",
            "JIY1M73", "JSY1M23", "JSY1M93", "JSY1M23", "JSV1M23", "JSY1M13"]
    for step, text in enumerate(seen * 3):
        legacy(index, "5", BASE + step * 10, text)

    apply(index, plan(index, GAP, 2))

    plates = index.plates()
    assert len(plates) == 1
    assert plates[0]["text"] == "JSY1M23"
    assert plates[0]["reads"] == len(seen) * 3


def test_two_different_plates_are_left_alone(index):
    legacy(index, "5", BASE, "ABC1D23")
    legacy(index, "5", BASE + 20, "XYZ9A99")

    assert plan(index, GAP, 2) == []
    assert len(index.plates()) == 2


def test_the_same_plate_on_another_camera_is_left_alone(index):
    """Two cameras see the plate in two places; pooling them would lose one."""
    legacy(index, "1", BASE, "ABC1D23")
    legacy(index, "5", BASE + 20, "ABC1D23")

    assert plan(index, GAP, 2) == []


def test_a_second_visit_outside_the_window_stays_a_second_visit(index):
    legacy(index, "5", BASE, "ABC1D23")
    legacy(index, "5", BASE + 4 * 3600, "ABC1D23")

    assert plan(index, GAP, 2) == []


def test_a_long_fragmented_stay_chains_through_the_window(index):
    """The window is measured to the newest row of a run, not the first, so a
    car that fragmented across two hours still pools into one row.
    """
    for step in range(20):
        legacy(index, "5", BASE + step * 360, "ABC1D23")  # every 6 minutes, for 2 hours

    apply(index, plan(index, GAP, 2))
    assert len(index.plates()) == 1


def test_the_clearest_crop_of_the_run_is_the_one_kept(index):
    legacy(index, "5", BASE, "ABC1D23", confidence=0.71, crop_path="plate/1/1.jpg")
    legacy(index, "5", BASE + 20, "ABC1D23", confidence=0.99, crop_path="plate/1/2.jpg")
    legacy(index, "5", BASE + 40, "ABC1D23", confidence=0.80, crop_path="plate/1/3.jpg")

    apply(index, plan(index, GAP, 2))

    kept = index.connection.execute("SELECT crop_path FROM plate").fetchone()
    assert kept["crop_path"] == "plate/1/2.jpg"


def test_rows_written_since_pooling_landed_are_not_touched(index):
    """They carry a plate box, and the live path already pools them by it."""
    event_id = index.open_event("5", "vehicle", BASE, 0.9)
    index.add_plate(event_id, "5", BASE, "ABC1D23", 0.9, 4, None, box=(300, 400, 60, 24))
    legacy(index, "5", BASE + 20, "ABC1D23")

    assert plan(index, GAP, 2) == []


def test_the_pooled_row_can_go_on_being_pooled_into(index):
    """What it writes has to be readable by the live path, or the next sighting
    would start over instead of adding to it.
    """
    for step in range(3):
        legacy(index, "5", BASE + step * 20, "ABC1D23")

    apply(index, plan(index, GAP, 2))

    stored = index.connection.execute("SELECT tally FROM plate").fetchone()["tally"]
    assert json.loads(stored) == {"ABC1D23": [3, pytest.approx(2.7)]}


def test_planning_alone_writes_nothing(index):
    for step in range(3):
        legacy(index, "5", BASE + step * 20, "ABC1D23")

    assert len(plan(index, GAP, 2)) == 1
    assert len(index.plates()) == 3
