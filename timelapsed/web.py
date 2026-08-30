"""A small, dependency-free web viewer for rendered timelapses.

Serves the timelapse directory of the capture library as a browsable page with
inline playback. Intended to sit behind Tailscale Serve rather than be exposed
to the internet: there is no authentication here on purpose.

Run with:  python -m timelapsed.web
"""
import json
import logging
import mimetypes
import re
import signal
import sqlite3
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from timelapsed.analysis.index import AnalysisIndex, from_epoch, to_epoch
from timelapsed.archiver import parse_segment_filename
from timelapsed.config import get_config
from timelapsed.image_capture_library import ImageCaptureLibrary, parse_timelapse_filename
from timelapsed.library_page import render_library
from timelapsed.live_page import render_live
from timelapsed.pages import load_page
from timelapsed.schema import CADENCES, Config
from timelapsed.status_page import render_status
from timelapsed.system_status import SystemStatusCollector, status_json

logger = logging.getLogger(__name__)

RANGE_HEADER = re.compile(r"bytes=(\d*)-(\d*)")
STREAM_CHUNK_SIZE = 256 * 1024

THUMBNAIL_WIDTH = 384
THUMBNAIL_QUALITY = "6"  # ffmpeg -q:v, 2 best to 31 worst
THUMBNAIL_CACHE_SIZE = 64


class ThumbnailCache:
    """Downscaled camera stills, keyed by source path and mtime.

    The sidebar polls these every 30 seconds across every camera, and a 1080p
    still is ~230 KB, so serving them raw would be over a megabyte a refresh for
    a 170-pixel-wide box. ffmpeg is already a hard dependency for rendering, so
    it does the scaling; the cache means it runs once per new still rather than
    once per request.
    """

    def __init__(self, maximum_entries: int = THUMBNAIL_CACHE_SIZE):
        self._entries: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._maximum_entries = maximum_entries
        self._lock = threading.Lock()

    def get(self, source: Path) -> bytes | None:
        try:
            key = (str(source), source.stat().st_mtime_ns)
        except OSError:
            return None

        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key]

        thumbnail = self._render(source)
        if thumbnail is None:
            return None

        with self._lock:
            self._entries[key] = thumbnail
            self._entries.move_to_end(key)
            while len(self._entries) > self._maximum_entries:
                self._entries.popitem(last=False)
        return thumbnail

    @staticmethod
    def _render(source: Path) -> bytes | None:
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-loglevel", "error", "-i", str(source),
                    # min() so a still smaller than the tile is never upscaled
                    # into a file bigger than the original.
                    "-vf", f"scale='min({THUMBNAIL_WIDTH},iw)':-2",
                    "-q:v", THUMBNAIL_QUALITY, "-f", "image2", "-vcodec", "mjpeg", "pipe:1",
                ],
                capture_output=True, timeout=15, check=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            logger.warning("Could not build a thumbnail for %s: %s", source, error)
            return None
        return result.stdout or None


@dataclass(frozen=True)
class TimelapseEntry:
    channel_id: str
    cadence: str
    starts: datetime
    finishes: datetime
    path: Path

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size

    def as_dict(self) -> dict:
        return {
            "channel": self.channel_id,
            "cadence": self.cadence,
            "starts": self.starts.isoformat(),
            "finishes": self.finishes.isoformat(),
            "size_bytes": self.size_bytes,
            "url": f"/video/{self.channel_id}/{self.path.name}",
        }


class TimelapseCatalogue:
    """Reads the capture library from disk on every request.

    No caching: the directory is small (one file per cadence per period) and a
    stale list is more annoying than a directory scan is expensive.
    """

    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.library = ImageCaptureLibrary(root_path)

    def channels(self) -> list[str]:
        if not self.root_path.is_dir():
            return []
        return sorted(
            entry.name for entry in self.root_path.iterdir()
            if entry.is_dir() and (entry / "timelapse").is_dir()
        )

    def entries(self, channel_id: str | None = None, cadence: str | None = None) -> list[TimelapseEntry]:
        found = []
        for candidate_channel in self.channels():
            if channel_id is not None and candidate_channel != channel_id:
                continue

            for path in (self.root_path / candidate_channel / "timelapse").iterdir():
                if not path.is_file():
                    continue
                try:
                    entry_cadence, starts, finishes = parse_timelapse_filename(path.stem)
                except ValueError:
                    continue
                if cadence is not None and entry_cadence != cadence:
                    continue
                found.append(TimelapseEntry(candidate_channel, entry_cadence, starts, finishes, path))

        found.sort(key=lambda entry: entry.starts, reverse=True)
        return found

    def latest_still(self, channel_id: str) -> Path | None:
        """The most recent captured image for a channel, or None if it has none."""
        if channel_id not in self.channels_with_images():
            return None
        entries = self.library._timestamped_paths(channel_id, "image")
        return entries[-1][1] if entries else None

    def channels_with_images(self) -> list[str]:
        if not self.root_path.is_dir():
            return []
        return sorted(
            entry.name for entry in self.root_path.iterdir()
            if entry.is_dir() and (entry / "image").is_dir()
        )

    def resolve_video(self, channel_id: str, filename: str) -> Path | None:
        """Resolve a video path, refusing anything that escapes the library root."""
        base = (self.root_path / channel_id / "timelapse").resolve()
        try:
            candidate = (base / filename).resolve()
            candidate.relative_to(base)
        except (ValueError, OSError):
            return None
        return candidate if candidate.is_file() else None


class ArchiveCatalogue:
    """What the archiver has replicated, read straight off its filenames.

    No database and no caching: the archive is indexed by its filenames the way
    the still library is, and the only questions asked of it are tiny --
    "what covers this moment" is one or two day-directory listings.
    """

    def __init__(self, root: Path):
        self.root = root

    def segments(self, channel: str, start: datetime, end: datetime, limit: int = 500) -> list[dict]:
        """Archived segments overlapping [start, end], oldest first.

        Walks only the day directories the window touches, plus the day before
        the window opens -- a segment can straddle midnight, but never more
        than one (they run minutes, not days).
        """
        found = []
        day = start.date() - timedelta(days=1)
        while day <= end.date() and len(found) < limit:
            directory = self.root / channel / day.strftime("%Y%m%d")
            day += timedelta(days=1)
            if not directory.is_dir():
                continue
            for stored in directory.iterdir():
                if stored.suffix != ".mp4":
                    continue
                try:
                    started_at, ended_at, _ = parse_segment_filename(stored.stem)
                except ValueError:
                    continue
                if ended_at < start or started_at > end:
                    continue
                found.append({
                    "starts": started_at.isoformat(),
                    "finishes": ended_at.isoformat(),
                    "size_bytes": stored.stat().st_size,
                    "url": f"/archive/{channel}/{directory.name}/{stored.name}",
                })
        found.sort(key=lambda segment: segment["starts"])
        return found[:limit]

    def resolve(self, channel: str, day: str, filename: str) -> Path | None:
        """Resolve an archived file, refusing anything that escapes the root."""
        if not re.fullmatch(r"\d{8}", day) or not filename.endswith(".mp4"):
            return None
        base = self.root.resolve()
        try:
            candidate = (base / channel / day / filename).resolve()
            candidate.relative_to(base)
        except (ValueError, OSError):
            return None
        return candidate if candidate.is_file() else None


# The viewer shell. Markup lives in templates/index.html; load_page stitches
# in the shared fragments once, at import.
PAGE_TEMPLATE = load_page("index.html")
PAYLOAD_MARKER = "<!-- __PAYLOADS__ -->"


def render_index(
    catalogue: TimelapseCatalogue,
    recognition: "RecognitionReader | None" = None,
    fps_by_cadence: dict[str, int] | None = None,
    plate_channels: list[str] | None = None,
    archive_enabled: bool = False,
) -> bytes:
    """The viewer shell with the whole catalogue embedded.

    Everything is served in one request: the catalogue is small (retention caps
    it at a few thousand entries) and embedding it means no second round trip
    and no loading state. The page filters and lays out client-side.

    The whole catalogue, deliberately. `?channel=` and `?cadence=` say what to
    open on, not what to send: they used to filter here, which left the page
    holding one camera's clips while the wall down the side still offered all
    six. Clicking any of the others found nothing, and since the library page
    links back as `/?channel=5&at=...`, arriving from a sighting made every
    other camera look as though its videos had been deleted.
    """
    entries = catalogue.entries()

    # The union, so a camera that is capturing but has not rendered anything yet
    # still gets a tile instead of vanishing from the wall.
    channels = sorted(
        set(catalogue.channels()) | set(catalogue.channels_with_images()),
        key=lambda name: (len(name), name),
    )

    def block(element_id: str, value: object) -> str:
        payload = json.dumps(value, separators=(",", ":"))
        # </script> inside a script block would close it early. A filename cannot
        # contain a slash, so this is belt and braces, but the payload is
        # disk-derived and this is the one sequence that could escape.
        payload = payload.replace("</", "<\\/")
        return f'<script type="application/json" id="{element_id}">{payload}</script>'

    # Activity is fetched rather than embedded: unlike the catalogue it depends
    # on the current zoom, so there is nothing sensible to bake into the page.
    # This flag just tells the page whether those endpoints exist at all.
    payloads = "\n".join((
        block("payload", [entry.as_dict() for entry in entries]),
        block("channels-payload", channels),
        block("recognition-payload", recognition is not None),
        # Lane order, colour lookup and filter chips all read this one array,
        # so the registry stays the single source of truth and a cadence added
        # there gets a lane without touching the viewer. Reversed: widest on top.
        block("cadences-payload", list(reversed(CADENCES))),
        # Frame stepping needs the rate the clip was rendered at, and that is a
        # per-cadence setting the browser has no way to find out: an MP4 carries
        # no frame rate the media element will admit to. Send it rather than
        # letting the player guess at 30 and land between frames.
        block("fps-payload", fps_by_cadence or {}),
        # Which cameras read plates. Only those get a plate counter on the wall:
        # elsewhere a plate count is zero every hour of every day, which reads as
        # "no cars" rather than as "nobody is looking".
        block("plate-channels-payload", plate_channels or []),
        # Whether real footage can be jumped to. Off, the footage lane stays a
        # map; on, it and every sighting become a way into the replica.
        block("archive-payload", archive_enabled),
    ))
    # An explicit marker, not an anchor into the page's own markup: splicing on
    # "<script>\nconst ENTRIES" broke silently the moment the template was
    # reformatted, serving a shell with no data in it.
    if PAYLOAD_MARKER not in PAGE_TEMPLATE:
        raise ValueError(f"index.html has no {PAYLOAD_MARKER} marker")
    return PAGE_TEMPLATE.replace(PAYLOAD_MARKER, payloads).encode()


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


class TimelapseRequestHandler(BaseHTTPRequestHandler):
    server_version = "timelapsed"
    protocol_version = "HTTP/1.1"

    # Set for the duration of a HEAD. Every response path checks it instead of
    # writing its body; the headers are produced exactly as they would be for a
    # GET, which is the whole point of HEAD.
    body_suppressed = False

    def __init__(
        self,
        *args,
        catalogue: TimelapseCatalogue,
        thumbnails: ThumbnailCache,
        recognition: "RecognitionReader | None" = None,
        status: SystemStatusCollector | None = None,
        fps_by_cadence: dict[str, int] | None = None,
        plate_channels: list[str] | None = None,
        live_channels: list[str] | None = None,
        archive: ArchiveCatalogue | None = None,
        **kwargs,
    ):
        self.catalogue = catalogue
        self.thumbnails = thumbnails
        self.recognition = recognition
        self.status = status
        self.fps_by_cadence = fps_by_cadence or {}
        self.plate_channels = plate_channels or []
        self.live_channels = live_channels or []
        self.archive = archive
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args) -> None:
        logger.debug("%s %s", self.address_string(), format % args)

    def do_HEAD(self) -> None:
        """HEAD is GET with the body left off.

        BaseHTTPRequestHandler answers 501 to any method it has no `do_` for, so
        without this the viewer refused the request every monitoring tool and
        every `curl -I` opens with -- including the check `nginx-setup.sh` prints
        and the ones in the docs, none of which could ever have passed against
        the Python side.

        Running the real do_GET is what keeps the two consistent: the routing,
        the 404s and every header including Content-Length are produced by the
        same code, so a HEAD can never drift from the GET it describes.
        """
        self.body_suppressed = True
        try:
            self.do_GET()
        finally:
            self.body_suppressed = False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        # parse_qs, not a hand-rolled split: identity names and plate searches
        # carry spaces and accents, which arrive percent-encoded.
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}

        try:
            if path == "/":
                body = render_index(
                    self.catalogue,
                    recognition=self.recognition,
                    fps_by_cadence=self.fps_by_cadence,
                    plate_channels=self.plate_channels,
                    archive_enabled=self.archive is not None,
                )
                self._send_bytes(body, "text/html; charset=utf-8")
            elif path == "/live":
                body = render_live(self.live_channels, recognition_enabled=self.recognition is not None)
                self._send_bytes(body, "text/html; charset=utf-8")
            elif path == "/library":
                if self.recognition is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Recognition is not enabled")
                    return
                self._send_bytes(render_library(), "text/html; charset=utf-8")
            elif path == "/status":
                # Not gated on recognition, unlike /library: the disk filling up
                # and a camera going quiet are the viewer's problems whether or
                # not anything is analysing the frames.
                self._send_bytes(render_status(), "text/html; charset=utf-8")
            elif path == "/api/system":
                # Ahead of the /api/ catch-all, which answers 404 when
                # recognition is off -- this endpoint works either way.
                self._serve_system_status(query)
            elif path == "/api/timelapses":
                entries = self.catalogue.entries(query.get("channel"), query.get("cadence"))
                body = json.dumps([entry.as_dict() for entry in entries], indent=2).encode()
                self._send_bytes(body, "application/json")
            elif path == "/api/archive":
                # Ahead of the /api/ catch-all: the archive is independent of
                # recognition being enabled.
                self._serve_archive_api(query)
            elif path.startswith("/api/"):
                self._serve_recognition_api(path, query)
            elif path.startswith("/video/"):
                self._serve_video(path)
            elif path.startswith("/archive/"):
                self._serve_archive_file(path)
            elif path.startswith("/thumb/"):
                self._serve_thumbnail(path)
            elif path.startswith("/crop/"):
                self._serve_crop(path)
            elif path == "/healthz":
                self._send_bytes(b"ok\n", "text/plain")
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except BrokenPipeError:
            # The browser closed the connection mid-seek; routine for <video>.
            pass
        except Exception:
            logger.exception("Error handling %s", self.path)
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal error")

    def do_POST(self) -> None:
        """The only writes the viewer accepts: naming and merging identities.

        Everything else here is read-only. Note there is still no authentication
        -- the viewer relies on Tailscale for that -- so this deliberately
        touches nothing but the recognition index.
        """
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if self.recognition is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Recognition is not enabled")
                return

            match = re.fullmatch(r"/api/identities/(\d+)", path)
            if not match:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            payload = self._read_json()
            if payload is None:
                return
            name = payload.get("name")
            if name is not None:
                name = str(name).strip()[:120] or None

            if self.recognition.rename_identity(int(match.group(1)), name):
                self._send_bytes(b'{"ok":true}', "application/json")
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "No such identity")
        except BrokenPipeError:
            pass
        except Exception:
            logger.exception("Error handling POST %s", self.path)
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal error")

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 64 * 1024:
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected a small JSON body")
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Malformed JSON")
            return None
        if not isinstance(payload, dict):
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected a JSON object")
            return None
        return payload

    def _serve_system_status(self, query: dict[str, str]) -> None:
        if self.status is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Status reporting is not configured")
            return
        # `refresh=1` is what the page's own button sends, so someone watching a
        # backlog drain is not held to the cache's TTL. Everything else, the
        # page's polling included, takes the cached answer.
        report = self.status.report(
            recognition=self.recognition, force=query.get("refresh") in ("1", "true")
        )
        self._send_bytes(status_json(report), "application/json", cache_control="no-store")

    def _serve_recognition_api(self, path: str, query: dict[str, str]) -> None:
        if self.recognition is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Recognition is not enabled")
            return

        def number(name: str, default: int | None = None) -> int | None:
            raw = query.get(name)
            try:
                return int(raw) if raw is not None else default
            except ValueError:
                return default

        if path == "/api/activity":
            channel = query.get("channel")
            start, end = number("start"), number("end")
            if not channel or start is None or end is None:
                self.send_error(HTTPStatus.BAD_REQUEST, "channel, start and end are required")
                return
            payload = self.recognition.activity(
                channel, start, end, number("buckets", 240) or 240
            )
        elif path == "/api/footage":
            # What the NVR itself holds, for the timeline's footage lane. Not
            # /api/events, which already means recognition events.
            channel = query.get("channel")
            start, end = number("start"), number("end")
            if not channel or start is None or end is None:
                self.send_error(HTTPStatus.BAD_REQUEST, "channel, start and end are required")
                return
            # About a pixel of the lane. Zoomed out, the lane could not show a
            # smaller gap anyway; zoomed in, the runs fall apart into segments.
            payload = self.recognition.footage_runs(
                channel, start, end, max((end - start) // 1000, 1)
            )
        elif path == "/api/events":
            payload = [
                event.as_dict()
                for event in self.recognition.events(
                    channel=query.get("channel"),
                    kind=query.get("kind"),
                    start=number("start"),
                    end=number("end"),
                    identity_id=number("identity"),
                    limit=min(number("limit", 500) or 500, 2000),
                )
            ]
        elif path == "/api/recent":
            # What each camera has seen lately, for the wall. The window is a
            # length rather than a pair of instants: it is always relative to
            # now, and now is the server's -- a phone with a wandering clock
            # should still be told what the cameras actually saw.
            minutes = min(max(number("minutes", 60) or 60, 1), 24 * 60)
            end = int(datetime.now(timezone.utc).timestamp())
            payload = self.recognition.recent_counts(
                end - int(timedelta(minutes=minutes).total_seconds()), end
            )
        elif path == "/api/status":
            # How far analysis has actually reached. Without this an empty
            # activity lane is indistinguishable from a window that simply has
            # not been analysed yet -- which, during a backfill, is most of them.
            payload = self.recognition.watermarks()
        elif path == "/api/identities":
            payload = self.recognition.identities(kind=query.get("kind"))
        elif path == "/api/plates":
            payload = self.recognition.plates(
                text=query.get("text"), channel=query.get("channel")
            )
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        self._send_bytes(
            json.dumps(payload, separators=(",", ":")).encode(),
            "application/json",
            cache_control="no-store",
        )

    def _serve_crop(self, path: str) -> None:
        """Event and plate crops from the recognition index."""
        if self.recognition is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Recognition is not enabled")
            return

        match = re.fullmatch(r"/crop/(event|plate)/(\d+)\.jpg", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        source = self.recognition.crop_file(match.group(1), int(match.group(2)))
        if source is None or not source.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        body = source.read_bytes()
        # Immutable once written: a crop belongs to one event and is never
        # rewritten, unlike the camera thumbnails.
        self._send_bytes(body, "image/jpeg", cache_control="public, max-age=86400")

    def _send_bytes(self, body: bytes, content_type: str, cache_control: str | None = None) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        if not self.body_suppressed:
            self.wfile.write(body)

    def _serve_thumbnail(self, path: str) -> None:
        """The latest still for a camera, downscaled, for the sidebar."""
        channel_id = path.removeprefix("/thumb/").removesuffix(".jpg")
        # Channel ids are directory names; anything with a separator is not one.
        if not channel_id or "/" in channel_id or channel_id in (".", ".."):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        source = self.catalogue.latest_still(channel_id)
        thumbnail = self.thumbnails.get(source) if source else None
        if thumbnail is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No still available")
            return

        # The sidebar cache-busts with a query parameter, so never cache these.
        self._send_bytes(thumbnail, "image/jpeg", cache_control="no-store")

    def _serve_archive_api(self, query: dict[str, str]) -> None:
        """Archived footage overlapping a window, for the viewer's jumps.

        Asked with tiny windows -- "what covers this click", "what covers this
        sighting" -- never for lane painting, which /api/footage already does
        from the mirror at run resolution.
        """
        if self.archive is None:
            self.send_error(HTTPStatus.NOT_FOUND, "The archive is not enabled")
            return
        channel = query.get("channel")
        try:
            start, end = int(query["start"]), int(query["end"])
        except (KeyError, ValueError):
            start = end = None
        if not channel or start is None or end is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "channel, start and end are required")
            return
        payload = self.archive.segments(channel, from_epoch(start), from_epoch(end))
        self._send_bytes(
            json.dumps(payload, separators=(",", ":")).encode(),
            "application/json",
            cache_control="no-store",
        )

    def _serve_archive_file(self, path: str) -> None:
        if self.archive is None:
            self.send_error(HTTPStatus.NOT_FOUND, "The archive is not enabled")
            return
        parts = path.removeprefix("/archive/").split("/")
        stored = self.archive.resolve(*parts) if len(parts) == 3 else None
        if stored is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._stream_file(stored)

    def _serve_video(self, path: str) -> None:
        parts = path.removeprefix("/video/").split("/", 1)
        if len(parts) != 2:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        video_path = self.catalogue.resolve_video(parts[0], parts[1])
        if video_path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._stream_file(video_path)

    def _stream_file(self, video_path: Path) -> None:
        """One video file with HTTP Range support, shared by /video/ and /archive/."""
        total_size = video_path.stat().st_size
        content_type = mimetypes.guess_type(video_path.name)[0] or "application/octet-stream"

        # Range support is what makes scrubbing work; Safari refuses to play
        # video at all from a server that does not advertise it.
        start, end = 0, total_size - 1
        status = HTTPStatus.OK
        if match := RANGE_HEADER.match(self.headers.get("Range", "")):
            raw_start, raw_end = match.groups()
            if raw_start:
                start = int(raw_start)
                end = int(raw_end) if raw_end else total_size - 1
            elif raw_end:
                start = max(0, total_size - int(raw_end))

            if start >= total_size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{total_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            end = min(end, total_size - 1)
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
        self.end_headers()
        # The range was still resolved and reported; only the bytes are skipped.
        if self.body_suppressed:
            return

        with open(video_path, "rb") as video_file:
            video_file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = video_file.read(min(STREAM_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def build_server(config: Config) -> ThreadingHTTPServer:
    catalogue = TimelapseCatalogue(config.image_capture_library_root)
    # One cache for the whole server: ThreadingHTTPServer builds a handler per
    # request, so per-handler state would never survive to be reused.
    handler = partial(
        TimelapseRequestHandler,
        catalogue=catalogue,
        thumbnails=ThumbnailCache(),
        recognition=RecognitionReader.open(config),
        status=SystemStatusCollector(config),
        fps_by_cadence={name: config.output_fps_for(name) for name in CADENCES},
        plate_channels=list(config.analysis_plate_channels),
        live_channels=list(config.channels),
        archive=ArchiveCatalogue(config.archive_root) if config.archive_root else None,
    )
    return ThreadingHTTPServer((config.web_host, config.web_port), handler)


def run() -> None:
    from timelapsed.timelapsed import apply_logging_config

    config = get_config()
    apply_logging_config(config)

    server = build_server(config)
    logger.info(
        "Timelapse viewer listening on http://%s:%d serving %s",
        config.web_host, config.web_port, config.image_capture_library_root,
    )

    def shutdown(signum, _frame):
        logger.info("Received signal %d; stopping viewer", signum)
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        logger.info("Viewer stopped")


if __name__ == "__main__":
    run()
