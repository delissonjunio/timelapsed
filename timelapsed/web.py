"""A small, dependency-free web viewer for rendered timelapses.

Serves the timelapse directory of the capture library as a browsable page with
inline playback. Intended to sit behind Tailscale Serve rather than be exposed
to the internet: there is no authentication here on purpose.

Run with:  python -m timelapsed.web
"""
import html
import json
import logging
import mimetypes
import re
import signal
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from timelapsed.config import get_config
from timelapsed.image_capture_library import ImageCaptureLibrary, parse_timelapse_filename
from timelapsed.schema import Config

logger = logging.getLogger(__name__)

RANGE_HEADER = re.compile(r"bytes=(\d*)-(\d*)")
STREAM_CHUNK_SIZE = 256 * 1024


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

    def resolve_video(self, channel_id: str, filename: str) -> Path | None:
        """Resolve a video path, refusing anything that escapes the library root."""
        base = (self.root_path / channel_id / "timelapse").resolve()
        try:
            candidate = (base / filename).resolve()
            candidate.relative_to(base)
        except (ValueError, OSError):
            return None
        return candidate if candidate.is_file() else None


def _format_size(size_bytes: int) -> str:
    megabytes = size_bytes / (1024 * 1024)
    return f"{megabytes:.0f} MB" if megabytes >= 1 else f"{size_bytes / 1024:.0f} KB"


def render_index(catalogue: TimelapseCatalogue, channel_id: str | None, cadence: str | None) -> bytes:
    channels = catalogue.channels()
    entries = catalogue.entries(channel_id, cadence)
    cadences = sorted({entry.cadence for entry in catalogue.entries()})

    def chip(label: str, target_channel: str | None, target_cadence: str | None, active: bool) -> str:
        query = []
        if target_channel:
            query.append(f"channel={target_channel}")
        if target_cadence:
            query.append(f"cadence={target_cadence}")
        href = "/?" + "&".join(query) if query else "/"
        return f'<a class="chip{" active" if active else ""}" href="{html.escape(href)}">{html.escape(label)}</a>'

    channel_chips = [chip("All channels", None, cadence, channel_id is None)]
    channel_chips += [
        chip(f"Channel {name}", name, cadence, channel_id == name) for name in channels
    ]

    cadence_chips = [chip("All cadences", channel_id, None, cadence is None)]
    cadence_chips += [
        chip(name.capitalize(), channel_id, name, cadence == name) for name in cadences
    ]

    if entries:
        cards = "\n".join(
            f'''<article class="card">
      <video controls preload="none" playsinline poster="" src="{html.escape(entry.as_dict()["url"])}"></video>
      <div class="meta">
        <span class="badge {html.escape(entry.cadence)}">{html.escape(entry.cadence)}</span>
        <span class="channel">Channel {html.escape(entry.channel_id)}</span>
        <time datetime="{entry.starts.isoformat()}">{entry.starts.strftime("%a %d %b %Y, %H:%M")} UTC</time>
        <span class="size">{_format_size(entry.size_bytes)}</span>
        <a class="download" href="{html.escape(entry.as_dict()["url"])}" download>Download</a>
      </div>
    </article>'''
            for entry in entries
        )
    else:
        cards = '<p class="empty">No timelapses rendered yet. They appear here as each cadence rolls over.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Timelapsed</title>
<style>
  :root {{ color-scheme: dark light; --bg:#0f1115; --fg:#e6e8ec; --muted:#9aa3b2; --line:#232733; --accent:#5b9dff; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:1.5rem; background:var(--bg); color:var(--fg);
         font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  header {{ display:flex; align-items:baseline; gap:.75rem; margin-bottom:1rem; flex-wrap:wrap; }}
  h1 {{ font-size:1.35rem; margin:0; letter-spacing:-.02em; }}
  .count {{ color:var(--muted); font-size:.9rem; }}
  nav {{ display:flex; flex-wrap:wrap; gap:.4rem; margin-bottom:.6rem; }}
  .chip {{ padding:.3rem .7rem; border:1px solid var(--line); border-radius:999px;
           color:var(--muted); text-decoration:none; font-size:.85rem; }}
  .chip.active {{ background:var(--accent); border-color:var(--accent); color:#0b1020; font-weight:600; }}
  .grid {{ display:grid; gap:1rem; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); margin-top:1rem; }}
  .card {{ border:1px solid var(--line); border-radius:12px; overflow:hidden; background:#151923; }}
  video {{ width:100%; display:block; aspect-ratio:16/9; background:#000; }}
  .meta {{ display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; padding:.6rem .75rem; font-size:.8rem; color:var(--muted); }}
  .badge {{ text-transform:uppercase; letter-spacing:.06em; font-size:.68rem; font-weight:700;
            padding:.15rem .45rem; border-radius:4px; background:#243047; color:#a9c6ff; }}
  .badge.daily {{ background:#2b2a45; color:#c3b6ff; }}
  .badge.weekly {{ background:#173a30; color:#8ee0b8; }}
  .channel {{ color:var(--fg); font-weight:600; }}
  time {{ flex:1 1 100%; }}
  .download {{ color:var(--accent); text-decoration:none; }}
  .empty {{ color:var(--muted); border:1px dashed var(--line); border-radius:12px; padding:2rem; text-align:center; }}
</style>
</head>
<body>
<header><h1>Timelapsed</h1><span class="count">{len(entries)} video{"s" if len(entries) != 1 else ""}</span></header>
<nav>{"".join(channel_chips)}</nav>
<nav>{"".join(cadence_chips)}</nav>
<div class="grid">
{cards}
</div>
</body>
</html>
""".encode()


class TimelapseRequestHandler(BaseHTTPRequestHandler):
    server_version = "timelapsed"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, catalogue: TimelapseCatalogue, **kwargs):
        self.catalogue = catalogue
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args) -> None:
        logger.debug("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = dict(
            pair.split("=", 1) for pair in parsed.query.split("&") if "=" in pair
        )

        try:
            if path == "/":
                body = render_index(self.catalogue, query.get("channel"), query.get("cadence"))
                self._send_bytes(body, "text/html; charset=utf-8")
            elif path == "/api/timelapses":
                entries = self.catalogue.entries(query.get("channel"), query.get("cadence"))
                body = json.dumps([entry.as_dict() for entry in entries], indent=2).encode()
                self._send_bytes(body, "application/json")
            elif path.startswith("/video/"):
                self._serve_video(path)
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

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_video(self, path: str) -> None:
        parts = path.removeprefix("/video/").split("/", 1)
        if len(parts) != 2:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        video_path = self.catalogue.resolve_video(parts[0], parts[1])
        if video_path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

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
    handler = partial(TimelapseRequestHandler, catalogue=catalogue)
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
