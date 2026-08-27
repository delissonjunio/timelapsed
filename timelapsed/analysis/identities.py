"""Grouping sightings that look like the same person.

This is deliberately not called face recognition, because it is not. Faces on
this footage top out at 38px and carry no usable identity signal (measured; see
docs/Recognition-Feasibility.md). Bodies are ~347px tall, so what is on offer is
appearance matching: same person, same clothes, same day.

The threshold is chosen for precision over recall. Measured on 423 real body
crops from this deployment:

    t=0.7  ->  50% of same-person pairs matched,  14.6% wrongly matched
    t=0.8  ->  27% of same-person pairs matched,   1.2% wrongly matched

A group the user has named must stay trustworthy, so 0.8 is the default: it
misses most repeat sightings rather than merging two different people.
"""
import logging
from datetime import timedelta

import numpy as np

from timelapsed.analysis.index import AnalysisIndex

logger = logging.getLogger(__name__)

# Beyond this, body appearance is not comparable -- people change clothes.
# Matching across it produces confident nonsense, so the search window is capped
# rather than left to the threshold to sort out.
DEFAULT_APPEARANCE_WINDOW = timedelta(hours=12)


def to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class IdentityMatcher:
    """Online nearest-neighbour matching against previously seen signatures.

    Brute force on purpose. A day holds a few hundred signatures of 768 floats;
    the whole comparison is one matrix multiply in well under a millisecond, and
    an approximate index would be a dependency and a moving part for no gain.
    """

    def __init__(
        self,
        index: AnalysisIndex,
        kind: str = "body",
        threshold: float = 0.8,
        window: timedelta = DEFAULT_APPEARANCE_WINDOW,
    ):
        self.index = index
        self.kind = kind
        self.threshold = threshold
        self.window = window

    def match(self, vector: np.ndarray, at: int) -> tuple[int | None, float]:
        """Best existing identity for this vector, or (None, best_score)."""
        since = at - int(self.window.total_seconds())
        stored = self.index.signatures(self.kind, since=since)
        if not stored:
            return None, 0.0

        identity_ids = np.array([identity_id for identity_id, _ in stored])
        matrix = np.stack([from_blob(blob) for _, blob in stored])
        similarities = matrix @ np.asarray(vector, dtype=np.float32)

        # Score an identity by its best signature, not its mean: a person seen
        # from behind once and from the front once should still match on the
        # front one rather than being dragged under by the average.
        best = int(similarities.argmax())
        if similarities[best] < self.threshold:
            return None, float(similarities[best])
        return int(identity_ids[best]), float(similarities[best])

    def assign(self, event_id: int, vector: np.ndarray, quality: float, at: int) -> int:
        """Attach this sighting to an identity, creating one if nothing matches."""
        identity_id, score = self.match(vector, at)
        if identity_id is None:
            identity_id = self.index.create_identity(self.kind_to_identity_kind(), at)
            logger.debug(
                "New identity %s for event %s (best rival %.2f)", identity_id, event_id, score
            )
        else:
            logger.debug(
                "Event %s matched identity %s at %.2f", event_id, identity_id, score
            )

        self.index.add_signature(
            identity_id, event_id, self.kind, to_blob(vector), quality, at
        )
        self.index.assign_identity(event_id, identity_id)
        return identity_id

    def kind_to_identity_kind(self) -> str:
        return "person" if self.kind in ("body", "face") else "vehicle"

    def consolidate(self, merge_threshold: float | None = None) -> int:
        """Merge identities that turned out to be the same person.

        Online matching compares a new sighting against what exists *at that
        moment*, so it fragments badly: one person crossing a yard bends over,
        turns their back, is half-occluded by a post, and each of those fails to
        match the frontal view directly. A full day of one person can end up as
        a hundred separate identities, which is useless.

        This is the fix, and it works because fragments are not islands. A back
        view does not match a frontal view, but both match the three-quarter
        views in between -- so linking anything that matches and taking the
        transitive closure pulls the whole chain together.

        Single linkage does chain, and chaining is how two genuinely different
        people could merge. Two things bound it: the merge threshold is higher
        than nothing at all, and pairs are only considered when the identities
        overlap within the appearance window, so yesterday's red shirt cannot
        join today's.

        Returns the number of identities removed by merging.
        """
        threshold = self.threshold if merge_threshold is None else merge_threshold
        grouped = self.index.identity_signatures(self.kind)
        if len(grouped) < 2:
            return 0

        spans = self.index.identity_spans(self.kind)
        window = int(self.window.total_seconds())
        identity_ids = sorted(grouped)
        matrices = {
            identity_id: np.stack([from_blob(blob) for blob in blobs])
            for identity_id, blobs in grouped.items()
        }

        parent = {identity_id: identity_id for identity_id in identity_ids}

        def find(node: int) -> int:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                # Keep the lower id, so the oldest identity survives a merge and
                # any name attached to it stays put.
                parent[max(left_root, right_root)] = min(left_root, right_root)

        for index_a, first in enumerate(identity_ids):
            for second in identity_ids[index_a + 1:]:
                if find(first) == find(second):
                    continue
                first_span, second_span = spans.get(first), spans.get(second)
                if first_span and second_span:
                    gap = max(first_span[0], second_span[0]) - min(first_span[1], second_span[1])
                    if gap > window:
                        continue
                # Single linkage: the best-matching pair of crops decides.
                if float((matrices[first] @ matrices[second].T).max()) >= threshold:
                    union(first, second)

        merged = 0
        for identity_id in identity_ids:
            root = find(identity_id)
            if root != identity_id:
                self.index.merge_identities(root, identity_id)
                merged += 1

        if merged:
            logger.info(
                "Consolidated %d identities into %d at threshold %.2f",
                len(identity_ids), len(identity_ids) - merged, threshold,
            )
        return merged
