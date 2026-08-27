"""Pool plate rows an older build wrote one per sighting.

Plates are pooled as they are read now: a plate that keeps turning up in the
same part of the frame is one car, and every read it gives votes together. Rows
written before that landed have neither the pooled tally nor the plate box, so
nothing can be folded into them retroactively by position -- and an index from
that era holds runs of near-identical rows, the same car read once per sighting
and read differently each time.

This collapses those runs. It matches on what a legacy row does carry: the same
channel, close in time, and text that nearly agrees. Position is deliberately
not required. Legacy rows have no plate box, and the vehicle box behind them is
often pruned or genuinely different -- a car is in one place driving in and
another parked -- so demanding overlap would refuse most of the merges that are
actually wanted.

The looser match is why the window is minutes rather than the six hours live
pooling allows: fragments of one sighting are seconds apart, while two real
visits are usually not, and a tight window keeps the second case out.

    python -m timelapsed.analysis.backfill              # report, change nothing
    python -m timelapsed.analysis.backfill --apply
"""
import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from timelapsed.analysis.index import AnalysisIndex, from_epoch
from timelapsed.analysis.pipeline import (
    characters_apart,
    looks_brazilian,
    tally_reads,
    vote_tally,
)
from timelapsed.config import get_config

logger = logging.getLogger(__name__)

DEFAULT_GAP_MINUTES = 30


def _dominant(rows: list[dict]) -> str:
    """What a run of rows mostly says, weighted by the reads behind each."""
    tally: Counter[str] = Counter()
    for row in rows:
        tally[row["text"]] += max(row["votes"], 1)
    return tally.most_common(1)[0][0]


def _apart(first: list[dict], second: list[dict]) -> int:
    """Seconds between two runs, or 0 where they overlap in time."""
    gap = max(first[0]["captured_at"], second[0]["captured_at"]) - min(
        first[-1]["captured_at"], second[-1]["captured_at"]
    )
    return max(gap, 0)


def _group(rows: list[dict], gap_seconds: int, drift: int) -> list[list[dict]]:
    """Runs of rows that are one car, oldest first within each run.

    A row is compared against what its candidate group mostly says, not against
    whichever row joined it last. That distinction is the whole difference
    between one run and five: the last row is itself a misread half the time, so
    chaining off it lets a group wander two characters at a step, and one car
    read four hundred times comes out as several overlapping runs that each hold
    a slice of the same evening.

    Time is measured against the newest row in the group rather than the first,
    so a car that fragments for an hour chains through however many rows that
    took, while a genuine second visit an hour later still starts its own.
    """
    groups: list[list[dict]] = []
    for row in sorted(rows, key=lambda row: row["captured_at"]):
        best, closest = None, drift + 1
        for group in reversed(groups):  # newest first, so it wins a tie
            if row["captured_at"] - group[-1]["captured_at"] > gap_seconds:
                continue
            apart = characters_apart(_dominant(group), row["text"])
            if apart < closest:
                best, closest = group, apart

        if best is None:
            best = []
            groups.append(best)
        best.append(row)
    return groups


def _consolidate(groups: list[list[dict]], gap_seconds: int, drift: int) -> list[list[dict]]:
    """Fold together runs that turned out to be the same car.

    One pass leaves a car split whenever two runs of it are alive at once: a row
    reading exactly what the second run says is nearer to it than to the first,
    so both keep being fed and neither absorbs the other. They are not two cars.
    Nothing else was parked in that spot, they overlap in time, and their texts
    are a character apart.

    Merging repeats until nothing moves, so a chain of three folds into one.
    What bounds it is that both tests must pass on the runs as wholes -- they
    have to overlap or nearly so *and* mostly agree -- which two genuinely
    different plates never do.
    """
    groups = [list(group) for group in groups]
    merging = True
    while merging:
        merging = False
        for first in range(len(groups)):
            for second in range(first + 1, len(groups)):
                if _apart(groups[first], groups[second]) > gap_seconds:
                    continue
                if characters_apart(
                    _dominant(groups[first]), _dominant(groups[second])
                ) > drift:
                    continue
                groups[first] = sorted(
                    groups[first] + groups[second], key=lambda row: row["captured_at"]
                )
                del groups[second]
                merging = True
                break
            if merging:
                break
    return groups


def _pool(group: list[dict]) -> dict:
    """The one row a run collapses to.

    Each legacy row stands for the reads behind it: `votes` of them agreed on
    its text, which is all that survived of the vote it came from. That is
    enough to vote again across the whole run, per character position, which is
    the point -- three sightings reading `JSY1H73`, `JSY1H33` and `JSY1H23` are
    a plate no single one of them got right.
    """
    tally: dict[str, list] = {}
    for row in group:
        reads = max(row["votes"], 1)
        tally = tally_reads([(row["text"], row["confidence"])] * reads, existing=tally)

    text, confidence, agreed, reads = vote_tally(tally)
    if not looks_brazilian(text):
        # Pooling must not turn readable plates into a malformed one. Fall back
        # to whichever member text the most reads stood behind.
        text = max(tally.items(), key=lambda item: item[1][0])[0]
        agreed = max(tally[text][0], 1)

    keep, newest = group[0], group[-1]
    # The clearest crop of the run, so the row shows the best look at the plate
    # rather than whichever sighting happened to be first.
    with_crop = [row for row in group if row["crop_path"]]
    crop_path = (
        max(with_crop, key=lambda row: row["confidence"])["crop_path"] if with_crop else None
    )
    return {
        "id": keep["id"],
        "channel": keep["channel"],
        # Retention prunes by event, so hang the row off the newest one behind
        # it: it is the same car, and the older events go first.
        "event_id": newest["event_id"],
        "captured_at": keep["captured_at"],
        "last_seen_at": newest["captured_at"],
        "text": text,
        "confidence": confidence,
        "votes": agreed,
        "reads": reads,
        "tally": json.dumps(tally),
        "crop_path": crop_path,
        "drop": [row["id"] for row in group[1:]],
    }


def plan(index: AnalysisIndex, gap_seconds: int, drift: int) -> list[dict]:
    """What would be merged. Only rows with no plate box, which is what dates
    them: everything written since pooling landed records where it sat."""
    rows = [
        dict(row)
        for row in index.connection.execute(
            "SELECT id, event_id, channel, captured_at, text, confidence, votes, crop_path "
            "FROM plate WHERE x IS NULL ORDER BY channel, captured_at"
        )
    ]
    channels: dict[str, list[dict]] = {}
    for row in rows:
        channels.setdefault(row["channel"], []).append(row)

    pooled = []
    for channel in sorted(channels):
        grouped = _consolidate(_group(channels[channel], gap_seconds, drift), gap_seconds, drift)
        for group in grouped:
            if len(group) > 1:
                pooled.append(_pool(group))
    return pooled


def apply(index: AnalysisIndex, pooled: list[dict]) -> None:
    with index.connection:
        for merge in pooled:
            index.connection.execute(
                "UPDATE plate SET event_id = ?, last_seen_at = ?, text = ?, confidence = ?, "
                "votes = ?, reads = ?, tally = ?, crop_path = ? WHERE id = ?",
                (merge["event_id"], merge["last_seen_at"], merge["text"], merge["confidence"],
                 merge["votes"], merge["reads"], merge["tally"], merge["crop_path"],
                 merge["id"]),
            )
            index.connection.executemany(
                "DELETE FROM plate WHERE id = ?", [(row_id,) for row_id in merge["drop"]]
            )


def report(pooled: list[dict]) -> None:
    for merge in pooled:
        print(
            f"{from_epoch(merge['captured_at']):%Y-%m-%d %H:%M}"
            f"-{from_epoch(merge['last_seen_at']):%H:%M}  camera {merge['channel']}"
            f"  {merge['text']}"
            f"  {merge['votes']} of {merge['reads']} reads agreed"
            f"  (was {len(merge['drop']) + 1} rows)"
        )
    removed = sum(len(merge["drop"]) for merge in pooled)
    print(f"\n{len(pooled)} plates pooled, {removed} rows removed.")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", type=Path, help="Index to work on. Defaults to the config's.")
    parser.add_argument(
        "--gap-minutes", type=int, default=DEFAULT_GAP_MINUTES,
        help=f"How far apart two rows can be and still be one car (default {DEFAULT_GAP_MINUTES}).",
    )
    parser.add_argument(
        "--drift", type=int, default=2,
        help="Characters two reads may differ by and still be one plate (default 2).",
    )
    parser.add_argument("--apply", action="store_true", help="Write the merges. Off by default.")
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    path = arguments.index or get_config().analysis_index_path
    if not path.exists():
        print(f"No index at {path}", file=sys.stderr)
        return 1

    with AnalysisIndex(path) as index:
        pooled = plan(index, arguments.gap_minutes * 60, arguments.drift)
        report(pooled)
        if not pooled:
            return 0
        if not arguments.apply:
            print("\nNothing written. Pass --apply to keep this.")
            return 0
        apply(index, pooled)
        print("Written.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
