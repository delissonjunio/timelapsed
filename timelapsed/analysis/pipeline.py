"""Frame in, events out.

The important idea here is the event. A detector run per frame produces one row
per box per frame, which at 10s capture means a car parked for eight hours is
~2,900 rows saying the same thing. An event is the contiguous sighting: it opens
when something appears, extends while it keeps appearing, and closes when it
stops. That is what the timeline draws, what a plate read votes inside, and what
an identity attaches to.

It also buys accuracy. Single-frame plate reads disagree at these plate sizes --
the same car read four different ways across four frames -- so the plate for an
event is the per-position majority across every frame of it, not any one read.

Plates go one step further and pool across events. A car sitting in a driveway
is one car whether or not the detector held on to it, and its plate stays in the
same few pixels the whole time; a plate that keeps reappearing in the same place
with nearly the same text is treated as that one car, and every read it ever
gave votes together. That is what turns four one-read sightings that disagree
into a single row with four reads behind it.
"""
import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image

from timelapsed.analysis.index import AnalysisIndex, from_epoch, to_epoch
from timelapsed.analysis.models import Detection

logger = logging.getLogger(__name__)

# Below this a body crop carries too little detail to re-identify. Measured: the
# usable crops on this footage are ~347px tall.
MIN_REID_HEIGHT = 150
# Plate text has to look like a real Brazilian plate. Both layouts are in use:
# the old ABC1234 and the Mercosul ABC1D23.
PLATE_LENGTH = 7

# A plate that keeps turning up in the same part of the frame is one car --
# parked, or waiting at a gate -- however many times the vehicle detector lost
# and refound it. Overlap is generous because a plate box is small and jitters
# by a few pixels between frames.
PLATE_TRACK_IOU = 0.3
# How long a plate stays poolable after its last read. Long, because a car sits
# in a driveway for hours and the text guard below is what keeps it honest.
PLATE_TRACK_GAP = timedelta(hours=6)
# Position alone would pool in whatever parks there next, so the texts have to
# nearly agree as well. Two reads of one plate differ in a character or two; two
# different plates differ in six or seven.
PLATE_TRACK_DRIFT = 2
# Distinct misreads of one plate are few. This caps what a long stay can grow
# to; it is a guard against unbounded rows, not a working limit.
PLATE_TALLY_LIMIT = 48


def looks_brazilian(text: str) -> bool:
    if len(text) != PLATE_LENGTH:
        return False
    letters, digits = str.isalpha, str.isdigit
    old = all(letters(c) for c in text[:3]) and all(digits(c) for c in text[3:])
    mercosul = (
        all(letters(c) for c in text[:3])
        and digits(text[3]) and letters(text[4]) and all(digits(c) for c in text[5:])
    )
    return old or mercosul


def tally_reads(
    reads: list[tuple[str, float]], existing: dict[str, list] | None = None
) -> dict[str, list]:
    """Full-length reads as {text: [count, confidence sum]}.

    A tally rather than the reads themselves because this is what gets stored
    and added to later: a car parked eight hours at a 5s interval gives ~5,800
    reads but only a handful of distinct strings among them.
    """
    tally = {text: [int(count), float(total)] for text, (count, total) in (existing or {}).items()}
    for text, confidence in reads:
        if len(text) != PLATE_LENGTH:
            continue
        entry = tally.setdefault(text, [0, 0.0])
        entry[0] += 1
        entry[1] += confidence
    if len(tally) > PLATE_TALLY_LIMIT:
        ranked = sorted(tally.items(), key=lambda item: item[1][0], reverse=True)
        tally = dict(ranked[:PLATE_TALLY_LIMIT])
    return tally


def vote_tally(tally: dict[str, list]) -> tuple[str, float, int, int] | None:
    """Reconcile a tally of reads into the most likely text.

    Per position rather than per string: at ~52px the model gets most characters
    right but rarely the same wrong one twice, so voting column by column
    recovers plates that no single frame read correctly.

    Returns the text, the mean confidence behind it, how many reads agreed with
    it exactly, and how many were pooled in total.
    """
    if not tally:
        return None

    voted = []
    for position in range(PLATE_LENGTH):
        weights: Counter[str] = Counter()
        for text, (_, total) in tally.items():
            weights[text[position]] += total
        voted.append(weights.most_common(1)[0][0])

    text = "".join(voted)
    reads = sum(count for count, _ in tally.values())
    confidence = sum(total for _, total in tally.values()) / reads
    agreed = tally.get(text, [0])[0]
    return text, float(confidence), max(agreed, 1), reads


def vote(reads: list[tuple[str, float]]) -> tuple[str, float, int] | None:
    """The vote for one sighting, ignoring anything seen before it."""
    result = vote_tally(tally_reads(reads))
    if result is None:
        return None
    text, confidence, agreed, _ = result
    return text, confidence, agreed


def nearly(first: str, second: str, drift: int = PLATE_TRACK_DRIFT) -> bool:
    """Two reads close enough to be the same plate rather than another car."""
    if len(first) != len(second):
        return False
    return sum(1 for a, b in zip(first, second) if a != b) <= drift


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    overlap = (right - left) * (bottom - top)
    return overlap / (aw * ah + bw * bh - overlap)


@dataclass
class OpenEvent:
    """An event still accepting frames."""
    event_id: int
    channel: str
    kind: str
    box: tuple[int, int, int, int]
    last_seen: int
    peak_score: float
    has_thumb: bool = False
    plate_reads: list[tuple[str, float]] = field(default_factory=list)
    best_plate_crop: tuple[float, np.ndarray] | None = None
    # Where the plate sat in the frame, not in the vehicle crop: this is what
    # says "the same car, still parked there" across separate events.
    best_plate_box: tuple[int, int, int, int] | None = None
    best_body: tuple[float, np.ndarray] | None = None


class EventTracker:
    """Associates per-frame detections with ongoing events, per channel.

    Association is IoU against the last known box. Objects move slowly relative
    to a 10s sampling interval -- a person crossing a gate is in roughly the same
    place two frames running -- so this is enough, and a motion model would be
    guessing at velocity from samples too sparse to support one.
    """

    def __init__(
        self,
        index: AnalysisIndex,
        store_crop: Callable[[np.ndarray, str, int, int], str | None] | None = None,
        assign_identity: Callable[[int, np.ndarray, float, int], None] | None = None,
        iou_threshold: float = 0.3,
        gap: timedelta = timedelta(seconds=60),
        plate_iou: float = PLATE_TRACK_IOU,
        plate_gap: timedelta = PLATE_TRACK_GAP,
    ):
        self.index = index
        # Injected rather than subclassed: the tracker needs to write a plate
        # crop when an event closes, but it has no business knowing where crops
        # live or how they are encoded.
        self.store_crop = store_crop or (lambda *_: None)
        # Identity is settled when the event closes, not while it runs: a person
        # walking towards the camera yields a better crop every frame, and
        # matching on each one would file the same visit under several
        # identities and count it several times over.
        self.assign_identity = assign_identity
        self.iou_threshold = iou_threshold
        self.gap_seconds = int(gap.total_seconds())
        self.plate_iou = plate_iou
        self.plate_gap_seconds = int(plate_gap.total_seconds())
        self.open_events: dict[str, list[OpenEvent]] = {}
        # The newest frame time seen on any channel. Staleness is measured
        # against this rather than the wall clock, so a backfill of last week's
        # frames does not look overdue and close every event on sight.
        self.frontier = 0

    def update(self, channel: str, at: int, detections: list[Detection]) -> list[tuple[Detection, OpenEvent]]:
        """Fold this frame's detections into events. Returns (detection, event) pairs."""
        self.open_events.setdefault(channel, [])
        self.frontier = max(self.frontier, at)
        self._close_stale(channel, at)
        # Fetched after the sweep, not before: _close_stale replaces the list,
        # so a reference taken earlier would be a stale alias and every frame
        # would open a fresh event instead of extending the running one.
        open_events = self.open_events[channel]

        paired, claimed = [], set()
        for detection in detections:
            best, best_iou = None, self.iou_threshold
            for candidate in open_events:
                if candidate.kind != detection.kind or id(candidate) in claimed:
                    continue
                overlap = iou(candidate.box, detection.box)
                if overlap >= best_iou:
                    best, best_iou = candidate, overlap

            if best is None:
                event_id = self.index.open_event(channel, detection.kind, at, detection.score)
                best = OpenEvent(
                    event_id=event_id, channel=channel, kind=detection.kind,
                    box=detection.box, last_seen=at, peak_score=detection.score,
                )
                open_events.append(best)
            else:
                self.index.extend_event(best.event_id, at, detection.score)
                best.box = detection.box
                best.last_seen = at
                best.peak_score = max(best.peak_score, detection.score)

            claimed.add(id(best))
            self.index.add_detection(
                best.event_id, channel, at, detection.kind, detection.score, detection.box
            )
            paired.append((detection, best))
        return paired

    def _close_stale(self, channel: str, now: int) -> None:
        open_events = self.open_events[channel]
        still_open = []
        for event in open_events:
            if now - event.last_seen > self.gap_seconds:
                self.finish(event)
            else:
                still_open.append(event)
        self.open_events[channel] = still_open

    def close_stale(self) -> None:
        """Close events nothing has extended lately, on every channel.

        Frames only reach `update` for the channel they came from, so a channel
        that goes quiet -- camera down, or simply nothing moving in front of it
        -- would hold its events, and their un-voted plate reads, open forever.
        This is the sweep the caller runs between passes; it is deliberately not
        `close_all`, because closing a running event and reopening it next pass
        files one car sitting in the driveway as a fresh sighting, and a fresh
        plate read, every few seconds.
        """
        for channel in list(self.open_events):
            self._close_stale(channel, self.frontier)

    def close_all(self) -> None:
        for channel in list(self.open_events):
            for event in self.open_events[channel]:
                self.finish(event)
            self.open_events[channel] = []

    def finish(self, event: OpenEvent) -> None:
        """Commit whatever an event accumulated: its identity and voted plate."""
        if event.best_body and self.assign_identity:
            quality, vector = event.best_body
            self.assign_identity(event.event_id, vector, quality, event.last_seen)

        if not event.plate_reads:
            return

        tally = tally_reads(event.plate_reads)
        result = vote_tally(tally)
        if result is None:
            logger.debug(
                "Event %s on channel %s: no full-length read among %d attempts",
                event.event_id, event.channel, len(event.plate_reads),
            )
            return

        track = self._matching_track(event, result[0])
        if track is None:
            self._open_plate(event, tally, result)
        else:
            self._extend_plate(event, tally, track)

    def _matching_track(self, event: OpenEvent, text: str) -> dict | None:
        """The plate already on record that this sighting is another look at.

        Same channel, recent, sitting in the same part of the frame, and reading
        nearly the same. Position is the strong signal -- a car does not move
        while it is parked -- and the text guard is what stops the next car to
        use that spot from being pooled into it.

        The most recent match wins: candidates come back newest first, and where
        two rows both overlap this box the fresher one is the live one.
        """
        if event.best_plate_box is None:
            return None
        since = event.last_seen - self.plate_gap_seconds
        for track in self.index.plate_tracks(event.channel, since):
            if iou(track["box"], event.best_plate_box) >= self.plate_iou and nearly(
                track["text"], text
            ):
                return track
        return None

    def _open_plate(self, event: OpenEvent, tally: dict, result: tuple) -> None:
        text, confidence, agreed, reads = result
        if not looks_brazilian(text):
            logger.debug("Discarded malformed plate %r on channel %s", text, event.channel)
            return
        self.index.add_plate(
            event.event_id, event.channel, event.last_seen, text, confidence, agreed,
            self._plate_crop(event), box=event.best_plate_box, reads=reads,
            tally=json.dumps(tally),
        )
        logger.info(
            "Plate %s on channel %s (%d reads, %d agreed, conf %.2f)",
            text, event.channel, reads, agreed, confidence,
        )

    def _extend_plate(self, event: OpenEvent, tally: dict, track: dict) -> None:
        """Fold this sighting into the plate already sitting in that spot."""
        stored = json.loads(track["tally"]) if track["tally"] else {}
        merged = tally_reads(event.plate_reads, existing=stored)
        text, confidence, agreed, reads = vote_tally(merged)
        if not looks_brazilian(text):
            # More evidence should never turn a good plate into a malformed one.
            # Keep what the row says and let the tally go on growing; the next
            # sighting may well settle it.
            text = track["text"]
            agreed = max(merged.get(text, [0])[0], 1)
        # A crop is written only when the row has none. The stored one came from
        # the most confident read of the first sighting, and churning it every
        # time the car is seen again would leave orphans for prune to sweep.
        crop_path = None if track["has_crop"] else self._plate_crop(event)
        self.index.extend_plate(
            track["id"], event.event_id, event.last_seen, text, confidence, agreed,
            reads, json.dumps(merged), event.best_plate_box, crop_path,
        )
        logger.info(
            "Plate %s on channel %s seen again in the same spot (%d reads pooled, "
            "%d agreed, conf %.2f)",
            text, event.channel, reads, agreed, confidence,
        )

    def _plate_crop(self, event: OpenEvent) -> str | None:
        if not event.best_plate_crop:
            return None
        return self.store_crop(
            event.best_plate_crop[1], "plate", event.event_id, event.last_seen
        )


@dataclass
class FrameResult:
    channel: str
    captured_at: int
    detections: int
    events_touched: int


class FrameAnalyzer:
    """Runs the models over frames and drives the tracker."""

    def __init__(
        self,
        index: AnalysisIndex,
        crops_root: Path,
        detector,
        score_threshold: float,
        body_embedder=None,
        plate_reader=None,
        identity_matcher=None,
        plate_channels: tuple[str, ...] = (),
        plate_confidence: float = 0.7,
        tracker: EventTracker | None = None,
    ):
        self.index = index
        self.crops_root = crops_root
        self.detector = detector
        self.score_threshold = score_threshold
        self.body_embedder = body_embedder
        self.plate_reader = plate_reader
        self.identity_matcher = identity_matcher
        self.plate_channels = plate_channels
        self.plate_confidence = plate_confidence
        self.tracker = tracker or EventTracker(
            index,
            store_crop=self._store_crop,
            assign_identity=(
                identity_matcher.assign if identity_matcher is not None else None
            ),
        )

    def analyse(self, channel: str, path: Path, captured_at: datetime) -> FrameResult:
        at = to_epoch(captured_at)
        image = np.asarray(Image.open(path).convert("RGB"))
        detections = self.detector(image, self.score_threshold)
        paired = self.tracker.update(channel, at, detections)

        for detection, event in paired:
            if not event.has_thumb:
                self._save_event_thumb(image, detection, event)
            if detection.kind == "person":
                self._maybe_embed(image, detection, event, at)
            elif detection.kind == "vehicle" and channel in self.plate_channels:
                self._maybe_read_plate(image, detection, event)

        return FrameResult(channel, at, len(detections), len({id(e) for _, e in paired}))

    def _save_event_thumb(self, image: np.ndarray, detection: Detection, event: OpenEvent) -> None:
        x, y, w, h = detection.box
        pad = int(0.15 * max(w, h))
        crop = image[
            max(y - pad, 0):y + h + pad,
            max(x - pad, 0):x + w + pad,
        ]
        if crop.size == 0:
            return
        path = self._store_crop(crop, "event", event.event_id, event.last_seen)
        if path:
            self.index.set_event_thumb(event.event_id, path)
            event.has_thumb = True

    def _maybe_embed(self, image: np.ndarray, detection: Detection, event: OpenEvent, at: int) -> None:
        if self.body_embedder is None or self.identity_matcher is None:
            return
        x, y, w, h = detection.box
        if h < MIN_REID_HEIGHT:
            return
        crop = image[y:y + h, x:x + w]
        if crop.size == 0:
            return
        # Re-embed only when the crop actually improves -- a bigger box carries
        # more detail. The match itself waits until the event closes, so a visit
        # is filed under one identity rather than one per frame.
        if event.best_body is None or h > event.best_body[0]:
            event.best_body = (float(h), self.body_embedder(crop))

    def _maybe_read_plate(self, image: np.ndarray, detection: Detection, event: OpenEvent) -> None:
        if self.plate_reader is None:
            return
        x, y, w, h = detection.box
        pad = int(0.08 * max(w, h))
        crop = image[max(y - pad, 0):y + h + pad, max(x - pad, 0):x + w + pad]
        if crop.size == 0:
            return
        for read in self.plate_reader(crop):
            if read.confidence < self.plate_confidence or not read.is_brazilian_region:
                continue
            event.plate_reads.append((read.text, read.confidence))
            px, py, pw, ph = read.box
            if event.best_plate_crop is None or read.confidence > event.best_plate_crop[0]:
                event.best_plate_crop = (read.confidence, crop[py:py + ph, px:px + pw].copy())
                # Back out of the crop into frame coordinates, which is the only
                # frame of reference two separate sightings share.
                origin_x, origin_y = max(x - pad, 0), max(y - pad, 0)
                event.best_plate_box = (origin_x + px, origin_y + py, pw, ph)

    def _store_crop(self, image: np.ndarray, kind: str, row_id: int, at: int) -> str | None:
        if image.size == 0:
            return None
        # Dated directories keep any one of them small, and make retention a
        # matter of deleting a day rather than walking a flat tree of thousands.
        day = from_epoch(at).strftime("%Y%m%d")
        relative = Path(kind) / day / f"{row_id}.jpg"
        target = self.crops_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        picture = Image.fromarray(image)
        picture.thumbnail((640, 640))
        picture.save(target, "JPEG", quality=82)
        return str(relative)
