"""The viewer's window onto the recognition index."""
import logging
import sqlite3
import threading
from pathlib import Path

from timelapsed.analysis.index import AnalysisIndex, from_epoch
from timelapsed.schema import Config

logger = logging.getLogger(__name__)


class RecognitionReader:
    """Read access to the recognition index, shared across handler threads.

    The analyzer owns the database; this only reads it, plus the one write the
    viewer allows (naming an identity). SQLite is happy with concurrent readers
    under WAL, but a single connection is not, so every call takes a lock. The
    queries are indexed lookups measured in microseconds, so the contention does
    not matter and one connection beats a pool of them.

    The index may legitimately not exist yet -- recognition is optional, and the
    analyzer creates the file on first run -- so `open` reports that rather than
    failing the whole viewer.
    """

    def __init__(self, index_path: Path, crops_root: Path):
        self.index_path = index_path
        self.crops_root = crops_root.resolve()
        self._lock = threading.Lock()
        self._index: AnalysisIndex | None = None

    @classmethod
    def open(cls, config: Config) -> "RecognitionReader | None":
        if not config.analysis_enabled:
            return None
        if not config.analysis_index_path.exists():
            logger.warning(
                "Recognition is enabled but %s does not exist yet. The viewer will "
                "serve timelapses only until the analyzer has run.",
                config.analysis_index_path,
            )
            return None
        return cls(config.analysis_index_path, config.analysis_crop_root)

    def _connection(self) -> AnalysisIndex:
        if self._index is None:
            self._index = AnalysisIndex(self.index_path, read_only=True)
        return self._index

    def activity(self, channel: str, start: int, end: int, buckets: int) -> dict:
        with self._lock:
            return self._connection().activity(channel, start, end, buckets)

    def events(self, **kwargs) -> list:
        with self._lock:
            return self._connection().events(**kwargs)

    def recent_counts(self, start: int, end: int) -> dict[str, dict[str, int]]:
        with self._lock:
            return self._connection().recent_counts(start, end)

    def footage_runs(self, channel: str, start: int, end: int, max_gap: int) -> list[dict]:
        with self._lock:
            try:
                return self._connection().segment_runs(channel, start, end, max_gap)
            except sqlite3.OperationalError:
                # The viewer reads the index without migrating it, so it can be
                # looking at a schema from before the footage mirror existed.
                # No table means no map, which the lane already draws as nothing.
                return []

    def segment_summary(self) -> dict[str, dict]:
        with self._lock:
            try:
                return self._connection().segment_summary()
            except sqlite3.OperationalError:
                # Same pre-mirror-schema tolerance as footage_runs.
                return {}

    def identities(self, kind: str | None = None) -> list[dict]:
        with self._lock:
            return self._connection().identities(kind=kind)

    def plates(self, text: str | None = None, channel: str | None = None) -> list[dict]:
        with self._lock:
            return self._connection().plates(text=text, channel=channel)

    def watermarks(self) -> dict[str, str]:
        with self._lock:
            return {
                channel: from_epoch(through).isoformat()
                for channel, through in self._connection().watermarks().items()
            }

    def watermark_epochs(self) -> dict[str, int]:
        """The same watermarks unconverted, for callers doing arithmetic on them.

        `watermarks` formats for the timeline, which wants a string it can hand
        straight to Date.parse. The status page subtracts them from frame
        timestamps, so it wants the seconds.
        """
        with self._lock:
            return self._connection().watermarks()

    def table_counts(self) -> dict[str, int]:
        with self._lock:
            return self._connection().table_counts()

    def rename_identity(self, identity_id: int, name: str | None) -> bool:
        # The one write. Opened separately so the read connection stays read-only
        # and a bug in a GET handler cannot mutate anything.
        with self._lock:
            with AnalysisIndex(self.index_path) as writable:
                return writable.rename_identity(identity_id, name)

    def crop_file(self, kind: str, row_id: int) -> Path | None:
        with self._lock:
            relative = self._connection().crop_path(kind, row_id)
        if not relative:
            return None
        # The path comes from the database, but resolve-then-check anyway: it is
        # the same guard resolve_video uses, and it costs nothing.
        candidate = (self.crops_root / relative).resolve()
        try:
            candidate.relative_to(self.crops_root)
        except ValueError:
            logger.warning("Refusing crop path outside the crop root: %s", relative)
            return None
        return candidate
