from datetime import timedelta

import numpy as np
import pytest
from PIL import Image

from tests.conftest import BASE_TIME
from timelapsed.analysis.index import AnalysisIndex, from_epoch, to_epoch
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


def must_vote(reads):
    result = vote(reads)
    assert result is not None
    return result


def test_voting_recovers_a_plate_no_single_frame_read_correctly():
    """The real failure mode at ~52px: most characters right, rarely the same
    one wrong twice. Position-wise majority beats trusting any one read."""
    reads = [("TZT4E17", 0.8), ("TZF4L17", 0.8), ("TZF4E17", 0.8), ("TIF4E17", 0.8)]
    text, confidence, agreed = must_vote(reads)
    assert text == "TZF4E17"
    assert agreed == 1
    assert confidence == pytest.approx(0.8)


def test_voting_reports_how_many_frames_agreed_outright():
    reads = [("ABC1D23", 0.9), ("ABC1D23", 0.9), ("ABC1X23", 0.5)]
    text, _, agreed = must_vote(reads)
    assert text == "ABC1D23"
    assert agreed == 2


def test_voting_weights_by_confidence_not_just_count():
    """A confident read should outweigh two hesitant ones on the same slot."""
    reads = [("ABC1D23", 0.99), ("ABC1X23", 0.30), ("ABC1X23", 0.31)]
    text, _, _ = must_vote(reads)
    assert text == "ABC1D23"


def test_voting_ignores_reads_of_the_wrong_length():
    assert must_vote([("ABC12", 0.9), ("ABC1D23", 0.9)])[0] == "ABC1D23"
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


def test_a_car_still_being_seen_keeps_one_event_across_passes(tracker, index):
    """The bug this guards: a sweep between passes used to close every open
    event, so one car standing in the driveway was filed as a fresh event -- and
    a fresh single-frame plate read -- every few seconds.
    """
    box = (100, 100, 200, 150)
    for step in range(4):
        tracker.update("1", BASE + step * 10, [vehicle(box)])
        tracker.open_events["1"][0].plate_reads.append(("ABC1D23", 0.9))
        tracker.close_stale()

    assert len(index.events(channel="1")) == 1
    assert index.plates() == []  # nothing committed while it is still there

    tracker.update("1", BASE + 300, [])
    tracker.close_stale()
    plates = index.plates()
    assert len(plates) == 1
    assert plates[0]["votes"] == 4


def test_a_quiet_channel_still_commits_its_plate(tracker, index):
    """Frames only reach `update` for their own channel, so an event on a camera
    that goes quiet has to be closed by the sweep or it never commits.
    """
    tracker.update("5", BASE, [vehicle((100, 100, 200, 150))])
    tracker.open_events["5"][0].plate_reads = [("ABC1D23", 0.9)]

    tracker.close_stale()
    assert index.plates() == []  # nothing newer has been seen anywhere yet

    tracker.update("1", BASE + 300, [])  # another channel moves the clock on
    tracker.close_stale()
    assert len(index.plates()) == 1


def test_a_backfill_does_not_close_events_against_the_wall_clock(tracker, index):
    """Frames arrive with their own capture times, which during a backfill are
    hours old. Staleness is measured against those, not against now.
    """
    old_time = BASE - 86400
    tracker.update("1", old_time, [vehicle((100, 100, 200, 150))])
    tracker.close_stale()

    assert len(tracker.open_events["1"]) == 1


def read_plate(tracker, channel, at, box, plate_box, reads):
    """One sighting: a vehicle seen once, with plate reads and where they sat."""
    tracker.update(channel, at, [vehicle(box)])
    event = tracker.open_events[channel][-1]
    event.plate_reads = list(reads)
    event.best_plate_box = plate_box
    return event


def test_a_plate_that_stays_put_pools_its_reads_into_one_row(tracker, index):
    """The point of pooling: a car parked in a driveway is one car, however many
    times the vehicle detector loses and refinds it. Four sightings that each
    read the plate once and disagree become one row that four reads voted on.
    """
    plate_box = (300, 400, 60, 24)
    for step, text in enumerate(["JSY1H73", "JSY1H33", "JSY1H23", "JSY1H23"]):
        read_plate(
            tracker, "5", BASE + step * 600, (100 + step, 100, 200, 150),
            plate_box, [(text, 0.9)],
        )
        tracker.close_all()

    plates = index.plates()
    assert len(plates) == 1
    assert plates[0]["text"] == "JSY1H23"  # the majority, per position
    assert plates[0]["reads"] == 4
    assert plates[0]["votes"] == 2


def test_pooling_keeps_the_moment_the_car_first_turned_up(tracker, index):
    plate_box = (300, 400, 60, 24)
    for step in range(3):
        read_plate(tracker, "5", BASE + step * 600, (100, 100, 200, 150),
                   plate_box, [("ABC1D23", 0.9)])
        tracker.close_all()

    plate = index.plates()[0]
    assert plate["seen_at"].startswith(from_epoch(BASE).isoformat()[:16])
    assert plate["last_seen_at"].startswith(from_epoch(BASE + 1200).isoformat()[:16])


def test_a_different_car_in_the_same_spot_is_not_pooled_in(tracker, index):
    """Position alone would merge whatever parks there next. The reads have to
    nearly agree too, and two different plates do not.
    """
    plate_box = (300, 400, 60, 24)
    read_plate(tracker, "5", BASE, (100, 100, 200, 150), plate_box, [("ABC1D23", 0.9)])
    tracker.close_all()
    read_plate(tracker, "5", BASE + 600, (100, 100, 200, 150), plate_box, [("XYZ9A99", 0.9)])
    tracker.close_all()

    assert sorted(plate["text"] for plate in index.plates()) == ["ABC1D23", "XYZ9A99"]


def test_a_plate_somewhere_else_in_frame_is_not_pooled_in(tracker, index):
    read_plate(tracker, "5", BASE, (100, 100, 200, 150), (300, 400, 60, 24),
               [("ABC1D23", 0.9)])
    tracker.close_all()
    read_plate(tracker, "5", BASE + 600, (900, 100, 200, 150), (1200, 200, 60, 24),
               [("ABC1D23", 0.9)])
    tracker.close_all()

    assert len(index.plates()) == 2


def test_a_plate_seen_again_days_later_starts_a_new_row(tracker, index):
    plate_box = (300, 400, 60, 24)
    read_plate(tracker, "5", BASE, (100, 100, 200, 150), plate_box, [("ABC1D23", 0.9)])
    tracker.close_all()
    read_plate(tracker, "5", BASE + 3 * 86400, (100, 100, 200, 150), plate_box,
               [("ABC1D23", 0.9)])
    tracker.close_all()

    assert len(index.plates()) == 2


def test_pooling_survives_a_restart(tracker, index):
    """The tally lives in the row, not in the tracker, so a restart mid-stay
    goes on pooling into the same plate instead of opening a second one.
    """
    plate_box = (300, 400, 60, 24)
    read_plate(tracker, "5", BASE, (100, 100, 200, 150), plate_box, [("ABC1D23", 0.9)])
    tracker.close_all()

    fresh = EventTracker(index, gap=timedelta(seconds=60))
    read_plate(fresh, "5", BASE + 600, (100, 100, 200, 150), plate_box, [("ABC1D23", 0.9)])
    fresh.close_all()

    plates = index.plates()
    assert len(plates) == 1
    assert plates[0]["reads"] == 2


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


def test_a_visit_is_filed_under_one_identity_however_many_frames_it_spans(index, tmp_path, frame):
    """A person walking towards the camera gives a better crop every frame.
    Matching on each one would file one visit under several identities and
    count it several times over, so the match waits for the event to close.
    """
    assigned = []

    class RecordingMatcher:
        def assign(self, event_id, vector, quality, at):
            assigned.append((event_id, vector, quality, at))

    class GrowingDetector:
        """The same person, closer each frame."""
        def __init__(self):
            self.step = 0

        def __call__(self, image, score_threshold):
            self.step += 1
            height = 150 + self.step * 40
            return [person((10, 10, 200, height))]

    analyzer = FrameAnalyzer(
        index=index,
        crops_root=tmp_path / "crops",
        detector=GrowingDetector(),
        score_threshold=0.5,
        body_embedder=lambda crop: np.ones(8, dtype=np.float32) / np.sqrt(8),
        identity_matcher=RecordingMatcher(),
    )

    for step in range(4):
        analyzer.analyse("1", frame, BASE_TIME + timedelta(seconds=10 * step))
    assert assigned == []  # still open, nothing settled yet

    analyzer.tracker.close_all()
    assert len(assigned) == 1
    # And it used the best crop, which is the last and largest.
    assert assigned[0][2] == pytest.approx(150 + 4 * 40)


def test_analyzer_skips_reid_on_bodies_too_small_to_identify(index, tmp_path, frame):
    class ExplodingEmbedder:
        def __call__(self, crop):
            raise AssertionError("re-ID must not run on a tiny crop")

    class UnusedMatcher:
        def assign(self, *args):
            raise AssertionError("nothing should be assigned")

    analyzer = FrameAnalyzer(
        index=index,
        crops_root=tmp_path / "crops",
        detector=FakeDetector([[person((10, 10, 40, 90))]]),
        score_threshold=0.5,
        body_embedder=ExplodingEmbedder(),
        identity_matcher=UnusedMatcher(),
    )
    analyzer.analyse("1", frame, BASE_TIME)
