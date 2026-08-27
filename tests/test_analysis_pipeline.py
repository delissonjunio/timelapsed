from datetime import timedelta

import numpy as np
import pytest
from PIL import Image

from tests.conftest import BASE_TIME
from timelapsed.analysis.index import AnalysisIndex, to_epoch
from timelapsed.analysis.models import Detection
from timelapsed.analysis.pipeline import (
    EventTracker,
    FrameAnalyzer,
    iou,
    looks_brazilian,
    vote,
)

BASE = to_epoch(BASE_TIME)


@pytest.fixture
def index(tmp_path):
    with AnalysisIndex(tmp_path / "index.sqlite3") as opened:
        yield opened


@pytest.fixture
def tracker(index):
    return EventTracker(index, gap=timedelta(seconds=60))


class FakeDetector:
    """Returns canned boxes, so the suite needs no ONNX models on disk."""

    def __init__(self, script: list[list[Detection]]):
        self.script = script
        self.calls = 0

    def __call__(self, image, score_threshold):
        result = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return [d for d in result if d.score >= score_threshold]


def person(box, score=0.9):
    return Detection(kind="person", score=score, box=box)


def vehicle(box, score=0.9):
    return Detection(kind="vehicle", score=score, box=box)


# --- plate text validation ---


@pytest.mark.parametrize("text", ["ABC1234", "ABC1D23", "XYZ9A99"])
def test_both_brazilian_layouts_are_accepted(text):
    assert looks_brazilian(text)


@pytest.mark.parametrize("text", ["ABC123", "ABCD123", "1234567", "AB1C234", "", "ABC12345"])
def test_malformed_plates_are_rejected(text):
    assert not looks_brazilian(text)


# --- plate voting ---


def test_voting_recovers_a_plate_no_single_frame_read_correctly():
    """The real failure mode at ~52px: most characters right, rarely the same
    one wrong twice. Position-wise majority beats trusting any one read."""
    reads = [("TZT4E17", 0.8), ("TZF4L17", 0.8), ("TZF4E17", 0.8), ("TIF4E17", 0.8)]
    text, confidence, agreed = vote(reads)
    assert text == "TZF4E17"
    assert agreed == 1
    assert confidence == pytest.approx(0.8)


def test_voting_reports_how_many_frames_agreed_outright():
    reads = [("ABC1D23", 0.9), ("ABC1D23", 0.9), ("ABC1X23", 0.5)]
    text, _, agreed = vote(reads)
    assert text == "ABC1D23"
    assert agreed == 2


def test_voting_weights_by_confidence_not_just_count():
    """A confident read should outweigh two hesitant ones on the same slot."""
    reads = [("ABC1D23", 0.99), ("ABC1X23", 0.30), ("ABC1X23", 0.31)]
    text, _, _ = vote(reads)
    assert text == "ABC1D23"


def test_voting_ignores_reads_of_the_wrong_length():
    assert vote([("ABC12", 0.9), ("ABC1D23", 0.9)])[0] == "ABC1D23"
    assert vote([("ABC12", 0.9), ("TOOLONG12", 0.9)]) is None


def test_voting_on_nothing_returns_none():
    assert vote([]) is None


# --- geometry ---


def test_iou_is_zero_for_disjoint_boxes():
    assert iou((0, 0, 10, 10), (100, 100, 10, 10)) == 0.0


def test_iou_is_one_for_identical_boxes():
    assert iou((5, 5, 20, 20), (5, 5, 20, 20)) == pytest.approx(1.0)


# --- event association ---


def test_a_parked_car_collapses_into_one_event(tracker, index):
    """The whole reason events exist: eight hours of a parked car is one row
    here and thousands in `detection`."""
    box = (100, 100, 200, 150)
    for step in range(60):
        tracker.update("1", BASE + step * 10, [vehicle(box)])

    events = index.events(channel="1")
    assert len(events) == 1
    assert events[0].frame_count == 60
    assert events[0].ended_at == BASE + 590


def test_a_box_that_drifts_between_frames_stays_one_event(tracker, index):
    """Objects move between 10s samples; association is IoU, not equality."""
    for step in range(5):
        tracker.update("1", BASE + step * 10, [person((100 + step * 10, 100, 200, 150))])

    assert len(index.events(channel="1")) == 1


def test_a_box_that_jumps_across_the_frame_starts_a_new_event(tracker, index):
    tracker.update("1", BASE, [person((0, 0, 100, 100))])
    tracker.update("1", BASE + 10, [person((1500, 800, 100, 100))])

    assert len(index.events(channel="1")) == 2


def test_a_short_absence_is_tolerated_rather_than_splitting_the_event(tracker, index):
    """A detector miss on one frame should not chop a sighting in half."""
    box = (100, 100, 200, 150)
    tracker.update("1", BASE, [vehicle(box)])
    tracker.update("1", BASE + 30, [vehicle(box)])

    assert len(index.events(channel="1")) == 1


def test_a_long_absence_closes_the_event(tracker, index):
    box = (100, 100, 200, 150)
    tracker.update("1", BASE, [vehicle(box)])
    tracker.update("1", BASE + 600, [vehicle(box)])

    assert len(index.events(channel="1")) == 2


def test_two_objects_in_one_frame_get_one_event_each(tracker, index):
    tracker.update("1", BASE, [person((0, 0, 100, 200)), person((900, 0, 100, 200))])
    tracker.update("1", BASE + 10, [person((0, 0, 100, 200)), person((900, 0, 100, 200))])

    events = index.events(channel="1")
    assert len(events) == 2
    assert all(event.frame_count == 2 for event in events)


def test_one_detection_cannot_claim_two_events(tracker, index):
    """Overlapping boxes must not both absorb the same detection."""
    tracker.update("1", BASE, [person((0, 0, 100, 200)), person((10, 10, 100, 200))])
    events = index.events(channel="1")
    assert len(events) == 2


def test_a_person_and_a_vehicle_in_the_same_place_stay_separate(tracker, index):
    box = (100, 100, 200, 200)
    tracker.update("1", BASE, [person(box), vehicle(box)])

    kinds = sorted(event.kind for event in index.events(channel="1"))
    assert kinds == ["person", "vehicle"]


def test_channels_do_not_share_events(tracker, index):
    box = (100, 100, 200, 150)
    tracker.update("1", BASE, [vehicle(box)])
    tracker.update("5", BASE, [vehicle(box)])

    assert len(index.events(channel="1")) == 1
    assert len(index.events(channel="5")) == 1


def test_every_frame_still_records_its_own_detection_row(tracker, index):
    box = (100, 100, 200, 150)
    for step in range(5):
        tracker.update("1", BASE + step * 10, [vehicle(box)])

    count = index.connection.execute("SELECT COUNT(*) AS n FROM detection").fetchone()["n"]
    assert count == 5


# --- plate commit on close ---


def test_a_plate_is_committed_only_when_its_event_closes(tracker, index):
    tracker.update("1", BASE, [vehicle((100, 100, 200, 150))])
    tracker.open_events["1"][0].plate_reads = [("ABC1D23", 0.9), ("ABC1D23", 0.9)]

    assert index.plates() == []
    tracker.close_all()

    plates = index.plates()
    assert len(plates) == 1
    assert plates[0]["text"] == "ABC1D23"
    assert plates[0]["votes"] == 2


def test_a_malformed_voted_plate_is_discarded(tracker, index):
    tracker.update("1", BASE, [vehicle((100, 100, 200, 150))])
    tracker.open_events["1"][0].plate_reads = [("XXXXXXX", 0.9)]
    tracker.close_all()

    assert index.plates() == []


# --- FrameAnalyzer end to end, with fakes ---


@pytest.fixture
def frame(tmp_path, jpeg_bytes):
    path = tmp_path / "frame.jpg"
    Image.fromarray(np.full((480, 640, 3), 120, dtype=np.uint8)).save(path)
    return path


def test_analyzer_writes_events_detections_and_a_thumbnail(index, tmp_path, frame):
    analyzer = FrameAnalyzer(
        index=index,
        crops_root=tmp_path / "crops",
        detector=FakeDetector([[person((10, 10, 200, 300))]]),
        score_threshold=0.5,
    )
    analyzer.analyse("1", frame, BASE_TIME)

    events = index.events(channel="1")
    assert len(events) == 1
    assert events[0].thumb_path is not None
    assert (tmp_path / "crops" / events[0].thumb_path).exists()


def test_analyzer_honours_the_score_threshold(index, tmp_path, frame):
    analyzer = FrameAnalyzer(
        index=index,
        crops_root=tmp_path / "crops",
        detector=FakeDetector([[person((10, 10, 200, 300), score=0.4)]]),
        score_threshold=0.5,
    )
    analyzer.analyse("1", frame, BASE_TIME)
    assert index.events() == []


def test_analyzer_skips_plates_on_channels_not_configured_for_them(index, tmp_path, frame):
    class ExplodingPlateReader:
        def __call__(self, crop, detect_threshold=0.4):
            raise AssertionError("plate reader must not run on this channel")

    analyzer = FrameAnalyzer(
        index=index,
        crops_root=tmp_path / "crops",
        detector=FakeDetector([[vehicle((10, 10, 200, 150))]]),
        score_threshold=0.5,
        plate_reader=ExplodingPlateReader(),
        plate_channels=("5",),
    )
    analyzer.analyse("1", frame, BASE_TIME)  # channel 1 is not in plate_channels


def test_analyzer_skips_reid_on_bodies_too_small_to_identify(index, tmp_path, frame):
    class ExplodingEmbedder:
        def __call__(self, crop):
            raise AssertionError("re-ID must not run on a tiny crop")

    analyzer = FrameAnalyzer(
        index=index,
        crops_root=tmp_path / "crops",
        detector=FakeDetector([[person((10, 10, 40, 90))]]),
        score_threshold=0.5,
        body_embedder=ExplodingEmbedder(),
        identity_matcher=object(),
    )
    analyzer.analyse("1", frame, BASE_TIME)
