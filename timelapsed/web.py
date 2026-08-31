"""The web viewer for rendered timelapses.

Serves the timelapse directory of the capture library as a browsable page with
inline playback. Intended to sit behind Tailscale Serve rather than be exposed
to the internet: there is no authentication here on purpose.

This module is the HTTP side: routing, range streaming, and the payloads baked
into the viewer shell. What it serves comes from elsewhere -- the catalogues in
catalogue.py, the recognition index via recognition_reader.py, the markup from
templates/ via pages.py.

Flask carries the routing and waitress the sockets, both chosen for the same
reason: boring, threaded, and on every APM agent's instrumentation list. New
Relic times a Flask view for free; the stdlib handler this replaced was
invisible to it. The tests still drive the app through werkzeug's threaded
server, whose interface matches the ThreadingHTTPServer they were written for.

Run with:  python -m timelapsed.web
"""
import json
import logging
import mimetypes
import re
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, request
from waitress.server import create_server

from timelapsed import telemetry
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
CROP_FILENAME = re.compile(r"\d+\.jpg")
STREAM_CHUNK_SIZE = 256 * 1024
WEB_THREADS = 8

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


def _html(body: bytes) -> Response:
    return Response(body, mimetype="text/html")


def _api_json(payload) -> Response:
    return Response(
        json.dumps(payload, separators=(",", ":")),
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _read_json() -> dict:
    length = request.content_length or 0
    if length <= 0 or length > 64 * 1024:
        abort(400, description="Expected a small JSON body")
    try:
        payload = json.loads(request.get_data())
    except (ValueError, UnicodeDecodeError):
        abort(400, description="Malformed JSON")
    if not isinstance(payload, dict):
        abort(400, description="Expected a JSON object")
    return payload


def _stream_file(video_path: Path) -> Response:
    """One video file with HTTP Range support, shared by /video/ and /archive/.

    Werkzeug turns this into a correct HEAD by itself: the headers below are
    produced exactly as for a GET and the body iterable is simply never
    started, so the generator's open() never runs.
    """
    total_size = video_path.stat().st_size
    content_type = mimetypes.guess_type(video_path.name)[0] or "application/octet-stream"

    # Range support is what makes scrubbing work; Safari refuses to play
    # video at all from a server that does not advertise it.
    start, end = 0, total_size - 1
    status = 200
    if match := RANGE_HEADER.match(request.headers.get("Range", "")):
        raw_start, raw_end = match.groups()
        if raw_start:
            start = int(raw_start)
            end = int(raw_end) if raw_end else total_size - 1
        elif raw_end:
            start = max(0, total_size - int(raw_end))

        if start >= total_size:
            return Response(
                b"",
                status=416,
                headers={"Content-Range": f"bytes */{total_size}", "Content-Length": "0"},
            )

        end = min(end, total_size - 1)
        status = 206

    length = end - start + 1

    def body():
        with open(video_path, "rb") as video_file:
            video_file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = video_file.read(min(STREAM_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
    return Response(body(), status=status, content_type=content_type, headers=headers)


def build_app(config: Config) -> Flask:
    """The viewer as a Flask app.

    Everything the routes share -- the catalogue, the thumbnail cache, the
    recognition reader -- is built once here and closed over, the one-cache-
    for-the-whole-server property the old per-request handler had to fake
    with functools.partial.
    """
    catalogue = TimelapseCatalogue(config.image_capture_library_root)
    thumbnails = ThumbnailCache()
    recognition = RecognitionReader.open(config)
    status = SystemStatusCollector(config)
    fps_by_cadence = {name: config.output_fps_for(name) for name in CADENCES}
    plate_channels = list(config.analysis_plate_channels)
    live_channels = list(config.channels)
    archive = ArchiveCatalogue(config.archive_root) if config.archive_root else None

    # No static route: the templates are stitched into pages by pages.py, and a
    # surprise /static/ mount is a route nothing here asked for.
    app = Flask(__name__, static_folder=None)

    @app.get("/")
    def index() -> Response:
        return _html(render_index(
            catalogue,
            recognition=recognition,
            fps_by_cadence=fps_by_cadence,
            plate_channels=plate_channels,
            archive_enabled=archive is not None,
        ))

    @app.get("/live")
    def live() -> Response:
        return _html(render_live(live_channels, recognition_enabled=recognition is not None))

    @app.get("/library")
    def library() -> Response:
        if recognition is None:
            abort(404, description="Recognition is not enabled")
        return _html(render_library())

    @app.get("/status")
    def status_page() -> Response:
        # Not gated on recognition, unlike /library: the disk filling up and a
        # camera going quiet are the viewer's problems whether or not anything
        # is analysing the frames.
        return _html(render_status())

    @app.get("/healthz")
    def healthz() -> Response:
        # Polled by nginx's check and the docs' curl; noise in an APM's
        # throughput and response-time numbers, so it reports to nobody.
        telemetry.ignore()
        return Response(b"ok\n", mimetype="text/plain")

    @app.get("/api/system")
    def api_system() -> Response:
        # A static rule, so it wins over the /api/ catch-all, which answers 404
        # when recognition is off -- this endpoint works either way.
        #
        # `refresh=1` is what the page's own button sends, so someone watching a
        # backlog drain is not held to the cache's TTL. Everything else, the
        # page's polling included, takes the cached answer.
        report = status.report(
            recognition=recognition, force=request.args.get("refresh") in ("1", "true")
        )
        return Response(
            status_json(report), mimetype="application/json",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/timelapses")
    def api_timelapses() -> Response:
        entries = catalogue.entries(request.args.get("channel"), request.args.get("cadence"))
        return Response(
            json.dumps([entry.as_dict() for entry in entries], indent=2),
            mimetype="application/json",
        )

    @app.get("/api/archive")
    def api_archive() -> Response:
        """Archived footage overlapping a window, for the viewer's jumps.

        Asked with tiny windows -- "what covers this click", "what covers this
        sighting" -- never for lane painting, which /api/footage already does
        from the mirror at run resolution. Static rule, ahead of the catch-all:
        the archive is independent of recognition being enabled.
        """
        if archive is None:
            abort(404, description="The archive is not enabled")
        channel = request.args.get("channel")
        try:
            start, end = int(request.args["start"]), int(request.args["end"])
        except (KeyError, ValueError):
            start = end = None
        if not channel or start is None or end is None:
            abort(400, description="channel, start and end are required")
        return _api_json(archive.segments(channel, from_epoch(start), from_epoch(end)))

    @app.get("/api/<path:endpoint>")
    def api_recognition(endpoint: str) -> Response:
        if recognition is None:
            abort(404, description="Recognition is not enabled")

        def number(name: str, default: int | None = None) -> int | None:
            raw = request.args.get(name)
            try:
                return int(raw) if raw is not None else default
            except ValueError:
                return default

        if endpoint == "activity":
            channel = request.args.get("channel")
            start, end = number("start"), number("end")
            if not channel or start is None or end is None:
                abort(400, description="channel, start and end are required")
            payload = recognition.activity(channel, start, end, number("buckets", 240) or 240)
        elif endpoint == "footage":
            # What the NVR itself holds, for the timeline's footage lane. Not
            # /api/events, which already means recognition events.
            channel = request.args.get("channel")
            start, end = number("start"), number("end")
            if not channel or start is None or end is None:
                abort(400, description="channel, start and end are required")
            # About a pixel of the lane. Zoomed out, the lane could not show a
            # smaller gap anyway; zoomed in, the runs fall apart into segments.
            payload = recognition.footage_runs(channel, start, end, max((end - start) // 1000, 1))
        elif endpoint == "events":
            payload = [
                event.as_dict()
                for event in recognition.events(
                    channel=request.args.get("channel"),
                    kind=request.args.get("kind"),
                    start=number("start"),
                    end=number("end"),
                    identity_id=number("identity"),
                    limit=min(number("limit", 500) or 500, 2000),
                )
            ]
        elif endpoint == "recent":
            # What each camera has seen lately, for the wall. The window is a
            # length rather than a pair of instants: it is always relative to
            # now, and now is the server's -- a phone with a wandering clock
            # should still be told what the cameras actually saw.
            minutes = min(max(number("minutes", 60) or 60, 1), 24 * 60)
            end = int(datetime.now(timezone.utc).timestamp())
            payload = recognition.recent_counts(
                end - int(timedelta(minutes=minutes).total_seconds()), end
            )
        elif endpoint == "status":
            # How far analysis has actually reached. Without this an empty
            # activity lane is indistinguishable from a window that simply has
            # not been analysed yet -- which, during a backfill, is most of them.
            payload = recognition.watermarks()
        elif endpoint == "identities":
            payload = recognition.identities(kind=request.args.get("kind"))
        elif endpoint == "plates":
            payload = recognition.plates(
                text=request.args.get("text"), channel=request.args.get("channel")
            )
        else:
            abort(404, description="Not found")

        return _api_json(payload)

    @app.post("/api/identities/<int:identity_id>")
    def rename_identity(identity_id: int) -> Response:
        """The only write the viewer accepts: naming and merging identities.

        Everything else here is read-only. Note there is still no
        authentication -- the viewer relies on Tailscale for that -- so this
        deliberately touches nothing but the recognition index.
        """
        if recognition is None:
            abort(404, description="Recognition is not enabled")
        payload = _read_json()
        name = payload.get("name")
        if name is not None:
            name = str(name).strip()[:120] or None
        if not recognition.rename_identity(identity_id, name):
            abort(404, description="No such identity")
        return Response(b'{"ok":true}', mimetype="application/json")

    @app.post("/api/<path:endpoint>")
    def api_post_fallback(endpoint: str) -> Response:
        # Parity with the old dispatcher: a POST anywhere but an identity
        # rename is a 404, never a 405.
        abort(404, description="Not found")

    @app.get("/video/<channel>/<path:filename>")
    def video(channel: str, filename: str) -> Response:
        video_path = catalogue.resolve_video(channel, filename)
        if video_path is None:
            abort(404, description="Not found")
        return _stream_file(video_path)

    @app.get("/archive/<channel>/<day>/<filename>")
    def archive_file(channel: str, day: str, filename: str) -> Response:
        if archive is None:
            abort(404, description="The archive is not enabled")
        stored = archive.resolve(channel, day, filename)
        if stored is None:
            abort(404, description="Not found")
        return _stream_file(stored)

    @app.get("/thumb/<name>")
    def thumb(name: str) -> Response:
        """The latest still for a camera, downscaled, for the sidebar."""
        channel_id = name.removesuffix(".jpg")
        # Channel ids are directory names; the route already refuses a
        # separator, and dot-names are not directories a channel may claim.
        if not channel_id or channel_id in (".", ".."):
            abort(404, description="Not found")

        source = catalogue.latest_still(channel_id)
        thumbnail = thumbnails.get(source) if source else None
        if thumbnail is None:
            abort(404, description="No still available")

        # The sidebar cache-busts with a query parameter, so never cache these.
        return Response(
            thumbnail, mimetype="image/jpeg", headers={"Cache-Control": "no-store"}
        )

    @app.get("/crop/<kind>/<filename>")
    def crop(kind: str, filename: str) -> Response:
        """Event and plate crops from the recognition index."""
        if recognition is None:
            abort(404, description="Recognition is not enabled")
        if kind not in ("event", "plate") or not CROP_FILENAME.fullmatch(filename):
            abort(404, description="Not found")

        source = recognition.crop_file(kind, int(filename.removesuffix(".jpg")))
        if source is None or not source.is_file():
            abort(404, description="Not found")

        # Immutable once written: a crop belongs to one event and is never
        # rewritten, unlike the camera thumbnails.
        return Response(
            source.read_bytes(), mimetype="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return app


def build_server(config: Config):
    """The viewer on werkzeug's threaded server, for the tests.

    serve_forever/shutdown/server_close/server_address: the same interface as
    the ThreadingHTTPServer the suite was written against, because werkzeug's
    server subclasses the same stdlib class. Production run() puts the same
    app on waitress instead.
    """
    from werkzeug.serving import make_server

    return make_server(config.web_host, config.web_port, build_app(config), threaded=True)


def run() -> None:
    from timelapsed.timelapsed import apply_logging_config

    config = get_config()
    apply_logging_config(config)

    server = create_server(
        build_app(config),
        host=config.web_host,
        port=config.web_port,
        threads=WEB_THREADS,
        ident="timelapsed",
    )
    logger.info(
        "Timelapse viewer listening on http://%s:%d serving %s",
        config.web_host, config.web_port, config.image_capture_library_root,
    )

    def shutdown(signum, _frame):
        logger.info("Received signal %d; stopping viewer", signum)
        server.close()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.run()
    finally:
        logger.info("Viewer stopped")


if __name__ == "__main__":
    run()
