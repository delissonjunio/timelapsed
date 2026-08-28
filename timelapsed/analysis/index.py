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
SCHEMA_VERSION = 3

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

-- One row per car, not per sighting. Individual frames disagree at these plate
-- sizes, so this holds the voted result rather than a raw read -- and the vote
-- pools every read of a plate that kept turning up in the same part of the
-- frame, which is what a parked or waiting car looks like. `captured_at` is
-- when it first appeared there, `last_seen_at` the most recent read.
--
-- `tally` is what makes pooling survive a restart: the full-length reads seen
-- so far as {text: [count, confidence sum]}. Adding a sighting is adding to it
-- and voting again, and it stays small where a list of raw reads would not --
-- a car parked eight hours at a 5s interval is ~5,800 reads but only a handful
-- of distinct misreads.
CREATE TABLE IF NOT EXISTS plate (
    id           INTEGER PRIMARY KEY,
    event_id     INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    channel      TEXT    NOT NULL,
    captured_at  INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL DEFAULT 0,
    text         TEXT    NOT NULL,
    confidence   REAL    NOT NULL,
    votes        INTEGER NOT NULL DEFAULT 1, -- reads that agreed on this text
    reads        INTEGER NOT NULL DEFAULT 1, -- full-length reads pooled in total
    tally        TEXT,                       -- JSON {text: [count, confidence sum]}
    x INTEGER, y INTEGER, w INTEGER, h INTEGER, -- where the plate sat in frame
    crop_path    TEXT
);
CREATE INDEX IF NOT EXISTS plate_text ON plate(text, captured_at);
CREATE INDEX IF NOT EXISTS plate_time ON plate(channel, captured_at);
CREATE INDEX IF NOT EXISTS plate_track ON plate(channel, last_seen_at);

-- How far each channel has been analysed. Restarting picks up here rather than
-- rescanning, and a channel that falls behind is visible at a glance.
CREATE TABLE IF NOT EXISTS watermark (
    channel         TEXT PRIMARY KEY,
    analysed_through INTEGER NOT NULL
);

-- One row per segment the NVR itself has recorded, mirrored from
-- ContentMgmt/search. A rebuildable cache: the device is authoritative for
-- what it holds, so none of this needs backing up -- dropping the table and
-- re-sweeping recreates it. Keyed on (channel, started_at) because a segment
-- still being written keeps the start it opened with while its end walks
-- forward; re-sweeping the recent past updates the row in place.
CREATE TABLE IF NOT EXISTS nvr_segment (
    id           INTEGER PRIMARY KEY,
    channel      TEXT    NOT NULL,
    started_at   INTEGER NOT NULL,          -- epoch seconds, UTC
    ended_at     INTEGER NOT NULL,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    -- The exact URI the device returned. ContentMgmt/download rejects anything
    -- without the name= and size= a search result carries, so this is stored
    -- verbatim rather than reconstructed from the times.
    playback_uri TEXT    NOT NULL,
    UNIQUE (channel, started_at)
);
CREATE INDEX IF NOT EXISTS nvr_segment_time ON nvr_segment(channel, started_at, ended_at);

-- How far each channel's segment sweep has reached, so a poll asks the device
-- about the recent past rather than its whole history every time.
CREATE TABLE IF NOT EXISTS nvr_sweep (
    channel       TEXT PRIMARY KEY,
    swept_through INTEGER NOT NULL
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
            # Before SCHEMA, not after: an index it declares may name a column
            # an older table has not got yet, and creating it would fail.
            self._add_missing_columns()
            self.connection.executescript(SCHEMA)
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        logger.info("Analysis index at %s is at schema %s", self.path, SCHEMA_VERSION)

    def _add_missing_columns(self) -> None:
        """CREATE TABLE IF NOT EXISTS leaves an existing table alone, so columns
        added to SCHEMA after an index was first written have to be asked for.

        Every one of them is nullable or defaulted, which is the constraint that
        keeps this a one-liner per column instead of a table rebuild. A table
        that is not there yet is skipped: SCHEMA is about to create it in full.
        """
        wanted = {
            "plate": {
                "last_seen_at": "INTEGER NOT NULL DEFAULT 0",
                "reads": "INTEGER NOT NULL DEFAULT 1",
                "tally": "TEXT",
                "x": "INTEGER", "y": "INTEGER", "w": "INTEGER", "h": "INTEGER",
            },
        }
        for table, columns in wanted.items():
            present = {
                row["name"]
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            if not present:
                continue
            for column, definition in columns.items():
                if column not in present:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
            if table == "plate":
                # Rows written before the span existed cover a single moment.
                self.connection.execute(
                    "UPDATE plate SET last_seen_at = captured_at WHERE last_seen_at = 0"
                )
                # ...and the reads behind them are at least the ones that agreed.
                # The column's default of 1 would otherwise claim a row that 40
                # frames voted on rests on one read.
                self.connection.execute(
                    "UPDATE plate SET reads = votes WHERE votes > reads"
                )

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

    # --- NVR footage ---

    def segment_sweep(self, channel: str) -> int | None:
        row = self.connection.execute(
            "SELECT swept_through FROM nvr_sweep WHERE channel = ?", (channel,)
        ).fetchone()
        return row["swept_through"] if row else None

    def record_segments(
        self, channel: str, segments: list[tuple[int, int, int, str]], swept_through: int
    ) -> None:
        """Store one sweep's worth of (started_at, ended_at, size_bytes, playback_uri).

        One transaction for the batch and the watermark together: a sweep that
        dies mid-write leaves the watermark where it was, so the next poll asks
        the device again rather than trusting half an answer.
        """
        with self.connection:
            self.connection.executemany(
                "INSERT INTO nvr_segment (channel, started_at, ended_at, size_bytes, playback_uri) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(channel, started_at) DO UPDATE SET "
                "ended_at = excluded.ended_at, size_bytes = excluded.size_bytes, "
                "playback_uri = excluded.playback_uri",
                [(channel, *segment) for segment in segments],
            )
            self.connection.execute(
                "INSERT INTO nvr_sweep (channel, swept_through) VALUES (?, ?) "
                "ON CONFLICT(channel) DO UPDATE SET swept_through = excluded.swept_through",
                (channel, swept_through),
            )

    def clear_segments(self, channel: str) -> None:
        """Forget a channel's segments and its sweep watermark, for a rebuild."""
        with self.connection:
            self.connection.execute("DELETE FROM nvr_segment WHERE channel = ?", (channel,))
            self.connection.execute("DELETE FROM nvr_sweep WHERE channel = ?", (channel,))

    def segments(
        self,
        channel: str | None = None,
        start: int | None = None,
        end: int | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        clauses, parameters = [], []
        if channel:
            clauses.append("channel = ?")
            parameters.append(channel)
        # Overlap, not containment, as with events: a segment straddling the
        # viewport edge still has to be drawn.
        if start is not None:
            clauses.append("ended_at >= ?")
            parameters.append(start)
        if end is not None:
            clauses.append("started_at <= ?")
            parameters.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return [
            {
                "id": row["id"],
                "channel": row["channel"],
                "starts": from_epoch(row["started_at"]).isoformat(),
                "finishes": from_epoch(row["ended_at"]).isoformat(),
                "size_bytes": row["size_bytes"],
                "playback_uri": row["playback_uri"],
            }
            for row in self.connection.execute(
                f"SELECT * FROM nvr_segment {where} ORDER BY started_at DESC LIMIT ?",
                (*parameters, limit),
            )
        ]

    def segment_runs(self, channel: str, start: int, end: int, max_gap: int) -> list[dict]:
        """The channel's footage over [start, end], merged into runs.

        The device records on events, so a busy day is hundreds of segments --
        far more than a timeline lane has pixels. Segments closer together than
        `max_gap` (the caller passes about a pixel's worth of seconds) merge into
        one run, so the payload tracks the zoom level rather than the recording's
        duty cycle.
        """
        merged: list[list[int]] = []  # [started_at, ended_at, segments, bytes]
        for row in self.connection.execute(
            "SELECT started_at, ended_at, size_bytes FROM nvr_segment "
            "WHERE channel = ? AND ended_at >= ? AND started_at <= ? "
            "ORDER BY started_at",
            (channel, start, end),
        ):
            if merged and row["started_at"] - merged[-1][1] <= max_gap:
                last = merged[-1]
                last[1] = max(last[1], row["ended_at"])
                last[2] += 1
                last[3] += row["size_bytes"]
            else:
                merged.append([row["started_at"], row["ended_at"], 1, row["size_bytes"]])
        return [
            {
                "starts": from_epoch(run_start).isoformat(),
                "finishes": from_epoch(run_end).isoformat(),
                "segments": count,
                "size_bytes": size,
            }
            for run_start, run_end, count, size in merged
        ]

    # --- housekeeping ---

    def table_counts(self) -> dict[str, int]:
        """How many rows each table holds, plus the span the events cover.

        For the status page, which needs a sense of what the index is carrying:
        `detection` outnumbers `event` by three orders of magnitude and is the
        one that decides how big the file gets, so seeing the ratio is what
        makes an over-generous `detection_retention_days` obvious.

        COUNT(*) with no WHERE is an index scan SQLite answers from the smallest
        index on the table, which at these row counts is milliseconds.
        """
        counts = {
            table: self.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("event", "detection", "plate", "identity", "signature", "watermark")
        }
        counts["named_identity"] = self.connection.execute(
            "SELECT COUNT(*) AS n FROM identity WHERE name IS NOT NULL"
        ).fetchone()["n"]

        span = self.connection.execute(
            "SELECT MIN(started_at) AS first, MAX(ended_at) AS last FROM event"
        ).fetchone()
        counts["oldest_event"] = span["first"]
        counts["newest_event"] = span["last"]
        return counts

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

    def recent_counts(self, start: int, end: int) -> dict[str, dict[str, int]]:
        """People and plates per channel over one window, for the camera wall.

        Two counts of different things, deliberately. A person count is events:
        one per arrival, so a visitor who stays is one. A plate count is rows in
        `plate`, which pooling already made one per car rather than per read --
        a car parked the whole hour is one plate, not four thousand.

        Whole-catalogue rather than per channel: the wall asks for every camera
        at once, and one grouped scan beats six round trips.
        """
        counts: dict[str, dict[str, int]] = {}

        def channel_counts(channel: str) -> dict[str, int]:
            return counts.setdefault(channel, {"person": 0, "vehicle": 0, "plate": 0})

        # Overlap, as everywhere else: someone who walked in before the window
        # opened and has not left is in it.
        for row in self.connection.execute(
            "SELECT channel, kind, COUNT(*) AS n FROM event "
            "WHERE ended_at >= ? AND started_at <= ? GROUP BY channel, kind",
            (start, end),
        ):
            if row["kind"] in ("person", "vehicle"):
                channel_counts(row["channel"])[row["kind"]] = row["n"]

        # `last_seen_at` is a column pooling added, and the viewer opens the
        # index read-only, so it can be reading one the analyzer has not
        # migrated yet. Where it is missing a plate is the moment it was first
        # read, which is what the rows meant before the span existed.
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(plate)")
        }
        last_seen = "MAX(captured_at, last_seen_at)" if "last_seen_at" in columns else "captured_at"
        for row in self.connection.execute(
            f"SELECT channel, COUNT(*) AS n FROM plate "
            f"WHERE {last_seen} >= ? AND captured_at <= ? GROUP BY channel",
            (start, end),
        ):
            channel_counts(row["channel"])["plate"] = row["n"]

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
        where = "WHERE identity.kind = ?" if kind else ""
        parameters = (kind, limit) if kind else (limit,)
        # Carry a representative crop. Without one the caller has to guess a URL,
        # and the obvious guess -- treating the identity id as an event id --
        # silently shows somebody else's picture.
        thumb_for = (
            "(SELECT event.id FROM event WHERE event.identity_id = identity.id "
            " AND event.thumb_path IS NOT NULL ORDER BY event.started_at DESC LIMIT 1)"
        )
        return [
            {
                "id": row["id"], "kind": row["kind"], "name": row["name"],
                "first_seen": from_epoch(row["created_at"]).isoformat(),
                "last_seen": from_epoch(row["last_seen_at"]).isoformat(),
                "sightings": row["sighting_count"],
                "thumb": f"/crop/event/{row['thumb_event']}.jpg" if row["thumb_event"] else None,
            }
            # Most-seen first: at a precision-first threshold most groups are
            # single sightings, and a list led by one-offs buries the few groups
            # that are actually worth naming.
            for row in self.connection.execute(
                f"SELECT identity.*, {thumb_for} AS thumb_event FROM identity {where} "
                f"ORDER BY sighting_count DESC, last_seen_at DESC LIMIT ?", parameters
            )
        ]

    def identity_signatures(self, kind: str, since: int | None = None) -> dict[int, list[bytes]]:
        """Every signature, grouped by identity, for the consolidation pass."""
        if since is None:
            rows = self.connection.execute(
                "SELECT identity_id, vector, captured_at FROM signature WHERE kind = ?", (kind,)
            )
        else:
            rows = self.connection.execute(
                "SELECT identity_id, vector, captured_at FROM signature "
                "WHERE kind = ? AND captured_at >= ?", (kind, since),
            )
        grouped: dict[int, list[bytes]] = {}
        for row in rows:
            grouped.setdefault(row["identity_id"], []).append(row["vector"])
        return grouped

    def identity_spans(self, kind: str) -> dict[int, tuple[int, int]]:
        """(first, last) signature time per identity, so merging can respect the
        appearance window rather than joining today's shirt to yesterday's."""
        return {
            row["identity_id"]: (row["first_at"], row["last_at"])
            for row in self.connection.execute(
                "SELECT identity_id, MIN(captured_at) AS first_at, MAX(captured_at) AS last_at "
                "FROM signature WHERE kind = ? GROUP BY identity_id", (kind,)
            )
        }

    def merge_identities(self, keep_id: int, drop_id: int) -> None:
        """Fold one identity into another, keeping every sighting and signature.

        The kept name wins unless it has none, so consolidating never silently
        discards a name somebody typed.
        """
        if keep_id == drop_id:
            return
        with self.connection:
            kept = self.connection.execute(
                "SELECT name FROM identity WHERE id = ?", (keep_id,)
            ).fetchone()
            dropped = self.connection.execute(
                "SELECT name FROM identity WHERE id = ?", (drop_id,)
            ).fetchone()
            if kept is None or dropped is None:
                return
            if not kept["name"] and dropped["name"]:
                self.connection.execute(
                    "UPDATE identity SET name = ? WHERE id = ?", (dropped["name"], keep_id)
                )

            self.connection.execute(
                "UPDATE signature SET identity_id = ? WHERE identity_id = ?", (keep_id, drop_id)
            )
            self.connection.execute(
                "UPDATE event SET identity_id = ? WHERE identity_id = ?", (keep_id, drop_id)
            )
            self.connection.execute(
                "UPDATE identity SET "
                "  sighting_count = (SELECT COUNT(*) FROM signature WHERE identity_id = ?), "
                "  created_at = MIN(created_at, (SELECT created_at FROM identity WHERE id = ?)), "
                "  last_seen_at = MAX(last_seen_at, (SELECT last_seen_at FROM identity WHERE id = ?)) "
                "WHERE id = ?",
                (keep_id, drop_id, drop_id, keep_id),
            )
            self.connection.execute("DELETE FROM identity WHERE id = ?", (drop_id,))

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
        box: tuple[int, int, int, int] | None = None,
        reads: int = 1, tally: str | None = None,
    ) -> int:
        x, y, w, h = box if box else (None, None, None, None)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO plate (event_id, channel, captured_at, last_seen_at, text, "
                "confidence, votes, reads, tally, x, y, w, h, crop_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, channel, at, at, text, confidence, votes, reads, tally,
                 x, y, w, h, crop_path),
            )
        return int(cursor.lastrowid)

    def extend_plate(
        self, plate_id: int, event_id: int, at: int, text: str, confidence: float,
        votes: int, reads: int, tally: str, box: tuple[int, int, int, int] | None,
        crop_path: str | None,
    ) -> None:
        """Fold another sighting into a plate already on record.

        `captured_at` is left alone: it is when this car first turned up in this
        spot, and it is what the row links to in the viewer, so it must not walk
        forward every time the car is seen again.
        """
        x, y, w, h = box if box else (None, None, None, None)
        with self.connection:
            self.connection.execute(
                "UPDATE plate SET event_id = ?, last_seen_at = ?, text = ?, confidence = ?, "
                "votes = ?, reads = ?, tally = ?, "
                "x = COALESCE(?, x), y = COALESCE(?, y), w = COALESCE(?, w), h = COALESCE(?, h), "
                "crop_path = COALESCE(?, crop_path) WHERE id = ?",
                (event_id, at, text, confidence, votes, reads, tally,
                 x, y, w, h, crop_path, plate_id),
            )

    def plate_tracks(self, channel: str, since: int) -> list[dict]:
        """Plates last seen on this channel recently, with where they sat.

        Rows written before the plate box was recorded have nothing to match a
        position against, so they are left out rather than guessed at.
        """
        return [
            {
                "id": row["id"], "text": row["text"], "tally": row["tally"],
                "box": (row["x"], row["y"], row["w"], row["h"]),
                "has_crop": row["crop_path"] is not None,
            }
            for row in self.connection.execute(
                "SELECT id, text, tally, x, y, w, h, crop_path FROM plate "
                "WHERE channel = ? AND last_seen_at >= ? AND x IS NOT NULL "
                "ORDER BY last_seen_at DESC",
                (channel, since),
            )
        ]

    def plates(self, text: str | None = None, channel: str | None = None, limit: int = 500) -> list[dict]:
        clauses, parameters = [], []
        if text:
            clauses.append("text LIKE ?")
            parameters.append(f"%{text.upper()}%")
        if channel:
            clauses.append("channel = ?")
            parameters.append(channel)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM plate {where} ORDER BY captured_at DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        # The viewer opens the index read-only, so it never migrates it. It can
        # therefore be looking at a schema older than the code it is running --
        # the analyzer is off, or has not been restarted yet -- and the columns
        # pooling added would not be there to read.
        columns = set(rows[0].keys()) if rows else set()

        def value(row, column, fallback):
            return row[column] if column in columns else fallback

        return [
            {
                "id": row["id"], "event_id": row["event_id"], "channel": row["channel"],
                "seen_at": from_epoch(row["captured_at"]).isoformat(),
                "last_seen_at": from_epoch(
                    value(row, "last_seen_at", 0) or row["captured_at"]
                ).isoformat(),
                "text": row["text"], "confidence": round(row["confidence"], 3),
                "votes": row["votes"], "reads": value(row, "reads", row["votes"]),
                "crop": f"/crop/plate/{row['id']}.jpg" if row["crop_path"] else None,
            }
            for row in rows
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
