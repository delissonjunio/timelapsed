from datetime import timedelta

import numpy as np
import pytest

from tests.conftest import BASE_TIME
from timelapsed.analysis.identities import IdentityMatcher, from_blob, to_blob
from timelapsed.analysis.index import AnalysisIndex, to_epoch

BASE = to_epoch(BASE_TIME)


@pytest.fixture
def index(tmp_path):
    with AnalysisIndex(tmp_path / "index.sqlite3") as opened:
        yield opened


@pytest.fixture
def matcher(index):
    return IdentityMatcher(index, threshold=0.8, window=timedelta(hours=12))


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def test_vectors_survive_the_blob_round_trip():
    vector = unit(0.1, 0.5, -0.3, 0.8)
    assert np.allclose(from_blob(to_blob(vector)), vector, atol=1e-6)


def test_the_first_sighting_creates_an_identity(matcher, index):
    identity_id = matcher.assign(
        index.open_event("1", "person", BASE, 0.9), unit(1, 0, 0), 300.0, BASE
    )
    assert identity_id is not None
    assert index.identities()[0]["sightings"] == 1


def test_a_similar_sighting_joins_the_existing_identity(matcher, index):
    first = matcher.assign(
        index.open_event("1", "person", BASE, 0.9), unit(1, 0, 0.02), 300.0, BASE
    )
    second = matcher.assign(
        index.open_event("1", "person", BASE + 60, 0.9), unit(1, 0, 0.03), 300.0, BASE + 60
    )
    assert first == second
    assert len(index.identities()) == 1


def test_a_dissimilar_sighting_starts_its_own_identity(matcher, index):
    first = matcher.assign(
        index.open_event("1", "person", BASE, 0.9), unit(1, 0, 0), 300.0, BASE
    )
    second = matcher.assign(
        index.open_event("1", "person", BASE + 60, 0.9), unit(0, 1, 0), 300.0, BASE + 60
    )
    assert first != second
    assert len(index.identities()) == 2


def test_matching_stops_at_the_appearance_window(index):
    """Body appearance is only comparable while the clothes are. Beyond the
    window an identical vector must not match, or a name leaks across days."""
    matcher = IdentityMatcher(index, threshold=0.8, window=timedelta(hours=12))
    vector = unit(1, 0, 0)
    first = matcher.assign(index.open_event("1", "person", BASE, 0.9), vector, 300.0, BASE)

    later = BASE + int(timedelta(days=2).total_seconds())
    second = matcher.assign(index.open_event("1", "person", later, 0.9), vector, 300.0, later)

    assert first != second


def test_an_identity_is_scored_by_its_best_signature_not_its_average(matcher, index):
    """Someone seen once from behind and once from the front should still match
    on the front one, rather than being dragged under by the mean."""
    identity_id = index.create_identity("person", BASE)
    index.add_signature(identity_id, None, "body", to_blob(unit(1, 0, 0)), 300.0, BASE)
    index.add_signature(identity_id, None, "body", to_blob(unit(0, 1, 0)), 300.0, BASE)

    matched, score = matcher.match(unit(1, 0, 0.01), BASE + 10)
    assert matched == identity_id
    assert score > 0.99


def test_matching_against_an_empty_index_reports_no_candidate(matcher):
    assert matcher.match(unit(1, 0, 0), BASE) == (None, 0.0)


def test_the_assigned_event_carries_the_identity(matcher, index):
    event_id = index.open_event("1", "person", BASE, 0.9)
    identity_id = matcher.assign(event_id, unit(1, 0, 0), 300.0, BASE)

    assert index.event(event_id).identity_id == identity_id
    assert len(index.events(identity_id=identity_id)) == 1


def test_a_higher_threshold_splits_what_a_lower_one_merges(index):
    """The threshold is the precision/recall dial the docs describe; this pins
    that it actually behaves like one.

    Uses match() rather than assign() so neither probe writes a signature the
    other would then find.
    """
    identity_id = index.create_identity("person", BASE)
    index.add_signature(identity_id, None, "body", to_blob(unit(1, 0, 0)), 300.0, BASE)

    probe = unit(1, 0, 0.75)  # cosine ~0.8 against the stored signature
    assert IdentityMatcher(index, threshold=0.5).match(probe, BASE)[0] == identity_id
    assert IdentityMatcher(index, threshold=0.95).match(probe, BASE)[0] is None


# --- consolidation ---


def build_identity(index, matcher, vectors, at=BASE):
    """Force each vector into its own identity, the way online matching would
    when consecutive poses do not resemble each other."""
    ids = []
    for offset, vector in enumerate(vectors):
        identity_id = index.create_identity("person", at + offset)
        event_id = index.open_event("1", "person", at + offset, 0.9)
        index.add_signature(identity_id, event_id, "body", to_blob(vector), 300.0, at + offset)
        index.assign_identity(event_id, identity_id)
        ids.append(identity_id)
    return ids


def test_consolidation_chains_fragments_through_the_poses_between_them(index, matcher):
    """The real failure: a back view never matches a frontal view directly, but
    both match the three-quarter views in between. Single linkage is what pulls
    the whole chain into one person."""
    chain = [unit(1, 0, 0), unit(1, 0.5, 0), unit(0.5, 1, 0), unit(0, 1, 0)]
    build_identity(index, matcher, chain)
    assert len(index.identities()) == 4
    # The ends are far apart on their own.
    assert float(chain[0] @ chain[-1]) < 0.75

    matcher.consolidate(0.75)

    identities = index.identities()
    assert len(identities) == 1
    assert identities[0]["sightings"] == 4


def test_consolidation_leaves_genuinely_different_people_apart(index, matcher):
    build_identity(index, matcher, [unit(1, 0, 0), unit(0, 1, 0), unit(0, 0, 1)])

    matcher.consolidate(0.9)

    assert len(index.identities()) == 3


def test_consolidation_moves_every_sighting_onto_the_survivor(index, matcher):
    ids = build_identity(index, matcher, [unit(1, 0, 0), unit(1, 0.02, 0)])
    matcher.consolidate(0.75)

    survivor = index.identities()[0]
    assert survivor["id"] == min(ids)
    assert len(index.events(identity_id=survivor["id"])) == 2
    assert survivor["sightings"] == 2


def test_consolidation_keeps_a_name_somebody_typed(index, matcher):
    """Merging must never silently discard a name."""
    ids = build_identity(index, matcher, [unit(1, 0, 0), unit(1, 0.02, 0)])
    index.rename_identity(max(ids), "Delisson")

    matcher.consolidate(0.75)

    identities = index.identities()
    assert len(identities) == 1
    assert identities[0]["name"] == "Delisson"


def test_consolidation_will_not_merge_across_the_appearance_window(index, matcher):
    """Identical vectors a week apart are the same shirt, not the same visit."""
    vector = unit(1, 0, 0)
    build_identity(index, matcher, [vector], at=BASE)
    build_identity(index, matcher, [vector], at=BASE + 7 * 86400)

    matcher.consolidate(0.75)

    assert len(index.identities()) == 2


def test_consolidation_is_idempotent(index, matcher):
    build_identity(index, matcher, [unit(1, 0, 0), unit(1, 0.02, 0), unit(1, 0.04, 0)])

    assert matcher.consolidate(0.75) > 0
    assert matcher.consolidate(0.75) == 0
    assert len(index.identities()) == 1


def test_consolidation_on_an_empty_index_does_nothing(index, matcher):
    assert matcher.consolidate(0.75) == 0
