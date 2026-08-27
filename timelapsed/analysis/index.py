"""The recognition index.

The rest of this project treats the filename as the index, which works because
every question it asks is "what is on disk between these two timestamps". This
subsystem asks questions a directory listing cannot answer -- "every time this
person appeared on this channel" -- so it keeps a real one.

SQLite, single file, stdlib. The analyzer is the only writer; the web viewer
opens it read-only. WAL is on so a read never blocks the writer, which is what
lets the viewer stay responsive while a backlog is being chewed through.
"""
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Bumped whenever SCHEMA changes shape. _migrate() reads PRAGMA user_version and
# applies everything newer, so an existing index is upgraded in place.
SCHEMA_VERSION = 1

SCHEMA = """
-- A contiguous sighting: one person or one vehicle, from when it appeared to
-- when it stopped appearing. This, not `detection`, is what the timeline draws
-- and what identity and plate reads hang off. A car parked for eight hours is
-- one row here and ~2,900 in `detection`, which is the whole reason it exists.
CREATE TABLE IF NOT EXISTS event (
    id           INTEGER PRIMARY KEY,
    channel      TEXT    NOT NULL,
    kind         TEXT    NOT NULL,          -- 'person' | 'vehicle'
    started_at   INTEGER NOT NULL,          -- epoch seconds, UTC, like everything on disk
    ended_at     INTEGER NOT NULL,
    frame_count  INTEGER NOT NULL DEFAULT 0,
    peak_score   REAL    NOT NULL DEFAULT 0,
    thumb_path   TEXT,                      -- representative crop, relative to the index root
    identity_id  INTEGER REFERENCES identity(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS event_channel_time ON event(channel, started_at, ended_at);
CREATE INDEX IF NOT EXISTS event_kind_time    ON event(kind, started_at);
CREATE INDEX IF NOT EXISTS event_identity     ON event(identity_id, started_at);

-- One row per box per frame. Cheap, numerous, and prunable: events and their
-- crops are the durable record, these are the working detail behind them.
CREATE TABLE IF NOT EXISTS detection (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    channel     TEXT    NOT NULL,
    captured_at INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    score       REAL    NOT NULL,
    x INTEGER NOT NULL, y INTEGER NOT NULL,
    w INTEGER NOT NULL, h INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS detection_event ON detection(event_id);
CREATE INDEX IF NOT EXISTS detection_time  ON detection(channel, captured_at);

-- A thing worth naming. `kind` is 'person' or 'vehicle'; `name` stays NULL
-- until somebody names it, which is the normal state for most rows.
CREATE TABLE IF NOT EXISTS identity (
    id             INTEGER PRIMARY KEY,
    kind           TEXT    NOT NULL,
    name           TEXT,
    created_at     INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL,
    sighting_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS identity_kind ON identity(kind, last_seen_at);

-- An appearance vector attached to an identity. `kind` distinguishes what
-- produced it: 'body' today, because faces on this hardware top out at 38px and
-- cannot be embedded reliably (see docs/Recognition-Feasibility.md). If a camera
-- is ever mounted at face height, 'face' rows join these without a migration.
CREATE TABLE IF NOT EXISTS signature (
    id          INTEGER PRIMARY KEY,
    identity_id INTEGER NOT NULL REFERENCES identity(id) ON DELETE CASCADE,
    event_id    INTEGER REFERENCES event(id) ON DELETE SET NULL,
    kind        TEXT    NOT NULL,           -- 'body' | 'face'
    vector      BLOB    NOT NULL,           -- float32, L2-normalised
    quality     REAL    NOT NULL DEFAULT 0, -- box height in px; bigger is better
    captured_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS signature_identity ON signature(identity_id);
CREATE INDEX IF NOT EXISTS signature_kind     ON signature(kind, captured_at);

-- One accepted plate read per event. Individual frames disagree at these plate
-- sizes, so this holds the voted result, not a raw read.
CREATE TABLE IF NOT EXISTS plate (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    channel     TEXT    NOT NULL,
    captured_at INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    confidence  REAL    NOT NULL,
    votes       INTEGER NOT NULL DEFAULT 1, -- frames that agreed on this text
    crop_path   TEXT
);
CREATE INDEX IF NOT EXISTS plate_text ON plate(text, captured_at);
CREATE INDEX IF NOT EXISTS plate_time ON plate(channel, captured_at);

-- How far each channel has been analysed. Restarting picks up here rather than
-- rescanning, and a channel that falls behind is visible at a glance.
CREATE TABLE IF NOT EXISTS watermark (
    channel         TEXT PRIMARY KEY,
    analysed_through INTEGER NOT NULL
);
"""


def to_epoch(moment: datetime) -> int:
    return int(moment.replace(tzinfo=moment.tzinfo or timezone.utc).timestamp())


def from_epoch(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


@dataclass(frozen=True)
class Event:
    id: int
    channel: str
    kind: str
    started_at: int
    ended_at: int
    frame_count: int
    peak_score: float
    thumb_path: str | None
    identity_id: int | None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "channel": self.channel,
            "kind": self.kind,
            "starts": from_epoch(self.started_at).isoformat(),
            "finishes": from_epoch(self.ended_at).isoformat(),
            "frame_count": self.frame_count,
            "score": round(self.peak_score, 3),
            "identity_id": self.identity_id,
            "thumb": f"/crop/event/{self.id}.jpg" if self.thumb_path else None,
        }


class AnalysisIndex:
    """Owns the SQLite file. One writer (the analyzer), many readers (the web)."""

    def __init__(self, path: Path, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if read_only:
            # WAL readers still need to write the -shm sidecar, so the directory
            # has to be writable even though the database itself is not.
            self.connection = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, check_same_thread=False
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if not read_only:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = NORMAL")
            self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "AnalysisIndex":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} was written by a newer Timelapsed (schema {version}, "
                f"this build understands {SCHEMA_VERSION}). Refusing to downgrade it."
            )
        with self.connection:
            self.connection.executescript(SCHEMA)
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        logger.info("Analysis index at %s is at schema %s", self.path, SCHEMA_VERSION)

    # --- watermarks ---

    def watermark(self, channel: str) -> int | None:
        row = self.connection.execute(
            "SELECT analysed_through FROM watermark WHERE channel = ?", (channel,)
        ).fetchone()
        return row["analysed_through"] if row else None

    def set_watermark(self, channel: str, analysed_through: int) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO watermark (channel, analysed_through) VALUES (?, ?) "
                "ON CONFLICT(channel) DO UPDATE SET analysed_through = excluded.analysed_through",
                (channel, analysed_through),
            )

    def watermarks(self) -> dict[str, int]:
        return {
            row["channel"]: row["analysed_through"]
            for row in self.connection.execute("SELECT channel, analysed_through FROM watermark")
        }

    # --- events ---

    def open_event(self, channel: str, kind: str, at: int, score: float) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO event (channel, kind, started_at, ended_at, frame_count, peak_score) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (channel, kind, at, at, score),
            )
        return int(cursor.lastrowid)

    def extend_event(self, event_id: int, at: int, score: float) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE event SET ended_at = ?, frame_count = frame_count + 1, "
                "peak_score = MAX(peak_score, ?) WHERE id = ?",
                (at, score, event_id),
            )

    def set_event_thumb(self, event_id: int, thumb_path: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE event SET thumb_path = ? WHERE id = ?", (thumb_path, event_id)
            )

    def add_detection(
        self, event_id: int, channel: str, at: int, kind: str, score: float, box: tuple[int, int, int, int]
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO detection (event_id, channel, captured_at, kind, score, x, y, w, h) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, channel, at, kind, score, *box),
            )

    def events(
        self,
        channel: str | None = None,
        kind: str | None = None,
        start: int | None = None,
        end: int | None = None,
        identity_id: int | None = None,
        limit: int = 2000,
    ) -> list[Event]:
        clauses, parameters = [], []
        if channel:
            clauses.append("channel = ?")
            parameters.append(channel)
        if kind:
            clauses.append("kind = ?")
            parameters.append(kind)
        if identity_id is not None:
            clauses.append("identity_id = ?")
            parameters.append(identity_id)
        # Overlap, not containment: an event straddling the viewport edge still
        # has to be drawn or the timeline grows holes as you pan.
        if start is not None:
            clauses.append("ended_at >= ?")
            parameters.append(start)
        if end is not None:
            clauses.append("started_at <= ?")
            parameters.append(end)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM event {where} ORDER BY started_at DESC LIMIT ?",
            (*parameters, limit),
        )
        return [
            Event(
                id=row["id"], channel=row["channel"], kind=row["kind"],
                started_at=row["started_at"], ended_at=row["ended_at"],
                frame_count=row["frame_count"], peak_score=row["peak_score"],
                thumb_path=row["thumb_path"], identity_id=row["identity_id"],
            )
            for row in rows
        ]

    def event(self, event_id: int) -> Event | None:
        found = self.connection.execute(
            "SELECT * FROM event WHERE id = ?", (event_id,)
        ).fetchone()
        if not found:
            return None
        return Event(
            id=found["id"], channel=found["channel"], kind=found["kind"],
            started_at=found["started_at"], ended_at=found["ended_at"],
            frame_count=found["frame_count"], peak_score=found["peak_score"],
            thumb_path=found["thumb_path"], identity_id=found["identity_id"],
        )

    def activity(self, channel: str, start: int, end: int, buckets: int) -> dict[str, list[int]]:
        """Per-bucket counts for the timeline's density strips.

        Aggregated in SQL at query time rather than kept in a rollup table: the
        bucket size changes with every zoom level, so a materialised one would
        have to be rebuilt per view anyway.
        """
        buckets = max(1, min(buckets, 2000))
        width = max(1, (end - start) // buckets)
        counts = {kind: [0] * buckets for kind in ("person", "vehicle")}

        # An event spans buckets, so it is counted in each one it covers -- a car
        # present all afternoon should shade the whole afternoon, not one bar.
        for event in self.events(channel=channel, start=start, end=end, limit=20000):
            if event.kind not in counts:
                continue
            first = max(0, (event.started_at - start) // width)
            last = min(buckets - 1, (event.ended_at - start) // width)
            for bucket in range(int(first), int(last) + 1):
                counts[event.kind][bucket] += 1
        return counts

    # --- identities ---

    def create_identity(self, kind: str, at: int) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO identity (kind, created_at, last_seen_at, sighting_count) "
                "VALUES (?, ?, ?, 0)",
                (kind, at, at),
            )
        return int(cursor.lastrowid)

    def add_signature(
        self, identity_id: int, event_id: int | None, kind: str,
        vector: bytes, quality: float, at: int,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO signature (identity_id, event_id, kind, vector, quality, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (identity_id, event_id, kind, vector, quality, at),
            )
            self.connection.execute(
                "UPDATE identity SET last_seen_at = MAX(last_seen_at, ?), "
                "sighting_count = sighting_count + 1 WHERE id = ?",
                (at, identity_id),
            )

    def assign_identity(self, event_id: int, identity_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE event SET identity_id = ? WHERE id = ?", (identity_id, event_id)
            )

    def signatures(self, kind: str, since: int | None = None) -> list[tuple[int, bytes]]:
        """(identity_id, vector) pairs for matching. Bounded by `since` because
        body appearance is only comparable while the clothes are the same."""
        if since is None:
            rows = self.connection.execute(
                "SELECT identity_id, vector FROM signature WHERE kind = ?", (kind,)
            )
        else:
            rows = self.connection.execute(
                "SELECT identity_id, vector FROM signature WHERE kind = ? AND captured_at >= ?",
                (kind, since),
            )
        return [(row["identity_id"], row["vector"]) for row in rows]

    def identities(self, kind: str | None = None, limit: int = 500) -> list[dict]:
        where = "WHERE kind = ?" if kind else ""
        parameters = (kind, limit) if kind else (limit,)
        return [
            {
                "id": row["id"], "kind": row["kind"], "name": row["name"],
                "first_seen": from_epoch(row["created_at"]).isoformat(),
                "last_seen": from_epoch(row["last_seen_at"]).isoformat(),
                "sightings": row["sighting_count"],
            }
            for row in self.connection.execute(
                f"SELECT * FROM identity {where} ORDER BY last_seen_at DESC LIMIT ?", parameters
            )
        ]

    def rename_identity(self, identity_id: int, name: str | None) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE identity SET name = ? WHERE id = ?", (name, identity_id)
            )
        return cursor.rowcount > 0

    # --- plates ---

    def add_plate(
        self, event_id: int, channel: str, at: int, text: str,
        confidence: float, votes: int, crop_path: str | None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO plate (event_id, channel, captured_at, text, confidence, votes, crop_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, channel, at, text, confidence, votes, crop_path),
            )

    def plates(self, text: str | None = None, channel: str | None = None, limit: int = 500) -> list[dict]:
        clauses, parameters = [], []
        if text:
            clauses.append("text LIKE ?")
            parameters.append(f"%{text.upper()}%")
        if channel:
            clauses.append("channel = ?")
            parameters.append(channel)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return [
            {
                "id": row["id"], "event_id": row["event_id"], "channel": row["channel"],
                "seen_at": from_epoch(row["captured_at"]).isoformat(),
                "text": row["text"], "confidence": round(row["confidence"], 3),
                "votes": row["votes"],
                "crop": f"/crop/plate/{row['id']}.jpg" if row["crop_path"] else None,
            }
            for row in self.connection.execute(
                f"SELECT * FROM plate {where} ORDER BY captured_at DESC LIMIT ?",
                (*parameters, limit),
            )
        ]

    def crop_path(self, kind: str, row_id: int) -> str | None:
        table, column = {
            "event": ("event", "thumb_path"),
            "plate": ("plate", "crop_path"),
        }.get(kind, (None, None))
        if table is None:
            return None
        row = self.connection.execute(
            f"SELECT {column} AS path FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()
        return row["path"] if row else None

    # --- retention ---

    def prune(self, detections_before: int | None, events_before: int | None) -> tuple[int, int]:
        """Age out the index.

        Crops live under the index root and are NOT covered by the library's own
        reclaim, which measures free space across the whole filesystem: left
        unbounded they would push it under the floor and make it delete stills
        forever. So this owns its own retention.
        """
        removed_detections = removed_events = 0
        with self.connection:
            if detections_before is not None:
                cursor = self.connection.execute(
                    "DELETE FROM detection WHERE captured_at < ?", (detections_before,)
                )
                removed_detections = cursor.rowcount
            if events_before is not None:
                cursor = self.connection.execute(
                    "DELETE FROM event WHERE ended_at < ?", (events_before,)
                )
                removed_events = cursor.rowcount
        return removed_detections, removed_events

    def orphaned_crops(self) -> set[str]:
        """Crop paths no surviving row still points at."""
        referenced = set()
        for query in ("SELECT thumb_path AS p FROM event", "SELECT crop_path AS p FROM plate"):
            referenced.update(row["p"] for row in self.connection.execute(query) if row["p"])
        return referenced
