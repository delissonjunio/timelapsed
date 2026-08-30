"""A small, dependency-free web viewer for rendered timelapses.

Serves the timelapse directory of the capture library as a browsable page with
inline playback. Intended to sit behind Tailscale Serve rather than be exposed
to the internet: there is no authentication here on purpose.

This module is the HTTP side: routing, range streaming, and the payloads baked
into the viewer shell. What it serves comes from elsewhere -- the catalogues in
catalogue.py, the recognition index via recognition_reader.py, the markup from
templates/ via pages.py.

Run with:  python -m timelapsed.web
"""
import json
import logging
import mimetypes
import re
import signal
from datetime import datetime, timedelta, timezone
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from timelapsed.analysis.index import from_epoch
from timelapsed.catalogue import ArchiveCatalogue, ThumbnailCache, TimelapseCatalogue
from timelapsed.config import get_config
from timelapsed.library_page import render_library
from timelapsed.live_page import render_live
from timelapsed.pages import load_page
from timelapsed.recognition_reader import RecognitionReader
from timelapsed.schema import CADENCES, Config
from timelapsed.status_page import render_status
from timelapsed.system_status import SystemStatusCollector, status_json

logger = logging.getLogger(__name__)

RANGE_HEADER = re.compile(r"bytes=(\d*)-(\d*)")
STREAM_CHUNK_SIZE = 256 * 1024

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
