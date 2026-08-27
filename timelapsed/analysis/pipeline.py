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
"""
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


def vote(reads: list[tuple[str, float]]) -> tuple[str, float, int] | None:
    """Reconcile several reads of one plate into the most likely text.

    Per position rather than per string: at ~52px the model gets most characters
    right but rarely the same wrong one twice, so voting column by column
    recovers plates that no single frame read correctly.
    """
    candidates = [(text, confidence) for text, confidence in reads if len(text) == PLATE_LENGTH]
    if not candidates:
        return None

    voted = []
    for position in range(PLATE_LENGTH):
        tally: Counter[str] = Counter()
        for text, confidence in candidates:
            tally[text[position]] += confidence
        voted.append(tally.most_common(1)[0][0])

    text = "".join(voted)
    agreed = sum(1 for candidate, _ in candidates if candidate == text)
    confidence = float(np.mean([c for _, c in candidates]))
    return text, confidence, max(agreed, 1)


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
        self.open_events: dict[str, list[OpenEvent]] = {}

    def update(self, channel: str, at: int, detections: list[Detection]) -> list[tuple[Detection, OpenEvent]]:
        """Fold this frame's detections into events. Returns (detection, event) pairs."""
        self.open_events.setdefault(channel, [])
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

        result = vote(event.plate_reads)
        if result is None:
            logger.debug(
                "Event %s on channel %s: no full-length read among %d attempts",
                event.event_id, event.channel, len(event.plate_reads),
            )
            return

        text, confidence, votes = result
        if not looks_brazilian(text):
            logger.debug("Discarded malformed plate %r on channel %s", text, event.channel)
            return

        crop_path = None
        if event.best_plate_crop:
            crop_path = self.store_crop(
                event.best_plate_crop[1], "plate", event.event_id, event.last_seen
            )
        self.index.add_plate(
            event.event_id, event.channel, event.last_seen, text, confidence, votes, crop_path
        )
        logger.info(
            "Plate %s on channel %s (%d reads, %d agreed, conf %.2f)",
            text, event.channel, len(event.plate_reads), votes, confidence,
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
