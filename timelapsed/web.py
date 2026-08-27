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
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from timelapsed.analysis.index import AnalysisIndex, from_epoch, to_epoch
from timelapsed.config import get_config
from timelapsed.image_capture_library import ImageCaptureLibrary, parse_timelapse_filename
from timelapsed.library_page import render_library
from timelapsed.schema import CADENCES, Config

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


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0b0d12">
<title>Timelapsed</title>
<!-- Three lanes narrowing from weekly to hourly, with the now marker: the
     timeline itself, legible at 16px. Inlined so the page stays one request. -->
<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iNyIgZmlsbD0iIzEyMTUxZCIvPjxyZWN0IHdpZHRoPSIzMiIgaGVpZ2h0PSIzMiIgcng9IjciIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzI0MmEzNyIvPjxyZWN0IHg9IjYiIHk9IjgiIHdpZHRoPSIyMCIgaGVpZ2h0PSI0IiByeD0iMiIgZmlsbD0iIzM0ZDM5OSIvPjxyZWN0IHg9IjYiIHk9IjE0IiB3aWR0aD0iMTMiIGhlaWdodD0iNCIgcng9IjIiIGZpbGw9IiNhNzhiZmEiLz48cmVjdCB4PSI2IiB5PSIyMCIgd2lkdGg9IjciIGhlaWdodD0iNCIgcng9IjIiIGZpbGw9IiM1YjlkZmYiLz48cmVjdCB4PSIyNyIgeT0iNiIgd2lkdGg9IjEiIGhlaWdodD0iMjAiIGZpbGw9IiNmZjZiNmIiLz48L3N2Zz4=">
<style>
:root {
  color-scheme: dark;
  --bg:#0b0d12; --panel:#12151d; --panel-2:#171b25; --fg:#e8ebf1; --muted:#8b93a5;
  --line:#242a37; --accent:#5b9dff;
  --hourly:#5b9dff; --daily:#a78bfa; --weekly:#34d399; --monthly:#f59e0b; --progress:#ec4899;
  --person:#22d3ee; --vehicle:#a3e635;
}
* { box-sizing:border-box; }
html, body { height:100%; }
body {
  margin:0; background:var(--bg); color:var(--fg); overflow:hidden;
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  display:grid; grid-template-rows:auto minmax(0,1fr) auto; grid-template-columns:210px minmax(0,1fr);
}
header {
  grid-column:1/-1; display:flex; align-items:center; gap:.75rem;
  padding:.7rem 1rem; border-bottom:1px solid var(--line); background:var(--panel);
}
header h1 { font-size:1rem; margin:0; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); font-weight:700; }
header .dot { width:7px; height:7px; border-radius:50%; background:var(--weekly); box-shadow:0 0 8px var(--weekly); }
header .spacer { flex:1; }
header .stat { color:var(--muted); font-size:.8rem; font-variant-numeric:tabular-nums; }

#channels { background:var(--panel); border-right:1px solid var(--line); overflow-y:auto; padding:.5rem; }
#channels h2 { font-size:.65rem; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); margin:.4rem .5rem .5rem; }
.cam {
  display:block; width:100%; text-align:left; cursor:pointer; margin-bottom:.45rem;
  background:none; border:1px solid var(--line); border-radius:9px; padding:0; overflow:hidden;
  color:var(--fg); font:inherit; font-size:.8rem;
}
.cam:hover { border-color:#3a4459; }
.cam[aria-pressed="true"] { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent); }
.cam .thumb { display:block; aspect-ratio:16/9; background:#05070b; position:relative; }
.cam .thumb img { width:100%; height:100%; object-fit:cover; display:block; }
.cam .thumb.blank::after {
  content:"no signal"; position:absolute; inset:0; display:flex; align-items:center;
  justify-content:center; font-size:.6rem; letter-spacing:.1em; text-transform:uppercase; color:#3d4557;
}
.cam .row { display:flex; align-items:center; gap:.45rem; padding:.4rem .55rem; background:var(--panel-2); }
.cam .led { width:6px; height:6px; border-radius:50%; background:var(--muted); flex:none; }
.cam[aria-pressed="true"] .led { background:var(--accent); box-shadow:0 0 6px var(--accent); }
.cam .n { flex:1; }
.cam .c { color:var(--muted); font-size:.72rem; font-variant-numeric:tabular-nums; }

#stage { display:flex; flex-direction:column; min-height:0; padding:1rem; gap:.75rem; }
#screen { flex:1; min-height:0; display:flex; align-items:center; justify-content:center; background:#000;
          border:1px solid var(--line); border-radius:10px; overflow:hidden; position:relative; }
/* A <video> is 300x150 until metadata loads, and max-width alone never grows it
     back up; object-fit keeps the aspect ratio while it fills the screen. */
#screen video { width:100%; height:100%; object-fit:contain; display:block; background:#000; cursor:pointer; }
#placeholder { color:var(--muted); font-size:.85rem; text-align:center; padding:2rem; position:absolute; }
/* Autoplay is normally allowed for a muted video, but not on every browser and
   not after every navigation. When it is refused the player must say so: a
   picture sitting silently paused is indistinguishable from a dead click, which
   is the bug this whole player exists to fix. */
#tapplay { position:absolute; inset:0; z-index:5; display:flex; flex-direction:column; gap:.4rem;
           align-items:center; justify-content:center; cursor:pointer; border:none;
           background:rgba(5,7,11,.55); color:var(--fg); font:inherit; font-size:.8rem; }
#tapplay .glyph { font-size:2rem; line-height:1; }

/* The transport borrows .tlbar so the two bars read as one instrument. */
#transport { margin-bottom:0; }
#transport .at { color:var(--fg); font-size:.8rem; font-variant-numeric:tabular-nums; }
/* `hidden` is only the UA rule [hidden] { display:none }, and any author
   `display` beats it -- #tapplay sets one directly and #transport inherits one
   from .tlbar. Without these two the attribute silently does nothing, which is
   how the tap-to-play overlay came to sit on top of a video playing perfectly
   well underneath it. Anything given a `display` here needs the same line. */
#tapplay[hidden], #transport[hidden] { display:none; }
/* Says which way a double tap went. Without it the picture just jumps and the
   gesture is invisible -- and an invisible gesture is an undiscoverable one. */
#skipflash { position:absolute; top:50%; transform:translateY(-50%); z-index:4;
             pointer-events:none; padding:.5rem .9rem; border-radius:999px;
             background:rgba(5,7,11,.72); color:var(--fg); font-size:.85rem;
             font-variant-numeric:tabular-nums; animation:skipfade .6s ease-out forwards; }
#skipflash.back { left:7%; }
#skipflash.fwd { right:7%; }
@keyframes skipfade { from { opacity:1; } to { opacity:0; } }
#nowplaying { display:flex; align-items:center; gap:.6rem; flex-wrap:wrap; font-size:.8rem; color:var(--muted); min-height:1.5rem; }
#nowplaying .tag { text-transform:uppercase; letter-spacing:.07em; font-size:.65rem; font-weight:700;
                   padding:.15rem .45rem; border-radius:4px; color:#07101f; }
#nowplaying .when { color:var(--fg); font-variant-numeric:tabular-nums; }
#nowplaying a { color:var(--accent); text-decoration:none; margin-left:auto; }

#timeline { grid-column:1/-1; border-top:1px solid var(--line); background:var(--panel); padding:.6rem 1rem .8rem; }
.tlbar { display:flex; align-items:center; gap:.4rem; flex-wrap:wrap; margin-bottom:.55rem; }
.tlbar .grp { display:flex; gap:2px; background:var(--panel-2); border-radius:7px; padding:2px; }
.tlbar button, .tlbar .toggle {
  background:none; border:none; color:var(--muted); font:inherit; font-size:.75rem; cursor:pointer;
  padding:.3rem .55rem; border-radius:5px;
}
.tlbar button:hover, .tlbar .toggle:hover { color:var(--fg); }
.tlbar button[aria-pressed="true"], .tlbar .toggle[aria-pressed="true"] { background:#28303f; color:var(--fg); }
.tlbar .toggle[data-cadence] { display:flex; align-items:center; gap:.35rem; }
.tlbar .toggle .sw { width:8px; height:8px; border-radius:2px; opacity:.35; }
.tlbar .toggle[aria-pressed="true"] .sw { opacity:1; }
.tlbar .spacer { flex:1; }
.tlbar .range { color:var(--muted); font-size:.75rem; font-variant-numeric:tabular-nums; }

#lanes { position:relative; cursor:grab; user-select:none; touch-action:none; }
#lanes.dragging { cursor:grabbing; }
.lane { position:relative; height:22px; margin-bottom:4px; display:flex; align-items:center; }
.lane .label { position:absolute; left:0; width:52px; font-size:.63rem; text-transform:uppercase;
                letter-spacing:.09em; color:var(--muted); pointer-events:none; z-index:2; }
.track { position:absolute; left:56px; right:0; top:0; bottom:0; background:var(--panel-2);
         border-radius:4px; overflow:hidden; }
.clip { position:absolute; top:3px; bottom:3px; border-radius:2px; cursor:pointer; opacity:.75; min-width:2px; }
.clip:hover { opacity:1; }
.clip.sel { opacity:1; outline:1.5px solid var(--fg); outline-offset:1px; z-index:3; cursor:ew-resize; }
#nowline { position:absolute; top:0; bottom:18px; width:1px; background:#ff6b6b; pointer-events:none; z-index:4; }
/* Where the video is, drawn back onto the timeline. Same geometry as #nowline
   but in the accent colour, because it is emphatically not "now". */
#playhead { position:absolute; top:0; bottom:18px; width:1px; background:var(--accent);
            pointer-events:none; z-index:5; box-shadow:0 0 6px var(--accent); }
#playhead::before { content:""; position:absolute; left:-3px; top:-3px; width:7px; height:7px;
                    border-radius:50%; background:var(--accent); }
/* What is actually downloaded, over the clip it belongs to. These files run to
   tens of megabytes, so "will this seek stall" is worth being able to see. */
.clip .buf { position:absolute; top:0; bottom:0; background:var(--fg); opacity:.3; pointer-events:none; }
#axis { position:relative; height:16px; margin-left:56px; }
#axis span { position:absolute; top:0; font-size:.63rem; color:var(--muted); transform:translateX(-50%);
             white-space:nowrap; font-variant-numeric:tabular-nums; }
#axis span::before { content:""; position:absolute; left:50%; top:-4px; height:3px; width:1px; background:var(--line); }
#empty { color:var(--muted); font-size:.8rem; padding:1rem 0 0 56px; }

/* Activity lanes. Density strips rather than discrete clips: at a 30-day zoom
   a single sighting is well under a pixel, so what reads is the shading. */
.lane.activity .track { background:var(--panel-2); }
/* The shading is painted from bucket counts, so it cannot resolve a click to a
   sighting by itself. The track above it does that, and owns the cursor. */
.bucket { position:absolute; top:0; bottom:0; pointer-events:none; }
.lane.activity .track.clickable { cursor:pointer; }
.lane.activity .track.clickable:hover { outline:1px solid var(--line); }
.lane.activity .label { color:var(--muted); }
.evt { position:absolute; top:2px; bottom:2px; border-radius:2px; cursor:pointer;
       opacity:.9; min-width:3px; }
.evt:hover { opacity:1; outline:1px solid var(--fg); }
.evt.sel { outline:1.5px solid var(--fg); outline-offset:1px; z-index:3; }
/* Lit while the playhead is inside the sighting, so the lane reads as part of
   the picture rather than as a separate index of it. */
.evt.live { outline:1.5px solid var(--fg); outline-offset:2px; opacity:1; z-index:4; }
/* Hatching, so an unanalysed stretch never reads as a quiet one. */
.pending { position:absolute; top:0; bottom:0; pointer-events:none; display:flex;
           align-items:center; justify-content:center; overflow:hidden;
           background:repeating-linear-gradient(135deg, transparent 0 6px,
                      rgba(255,255,255,.045) 6px 12px); }
.pending span { font-size:.6rem; color:var(--muted); letter-spacing:.04em;
                white-space:nowrap; }

header .navlink { color:var(--muted); text-decoration:none; font-size:.75rem;
                  padding:.3rem .6rem; border-radius:6px; border:1px solid var(--line); }
header .navlink:hover { color:var(--fg); background:var(--panel-2); }

@media (max-width:760px) {
  body { grid-template-columns:1fr; grid-template-rows:auto auto minmax(0,1fr) auto; }
  header { grid-column:1; }
  #channels { grid-column:1; border-right:none; border-bottom:1px solid var(--line);
              display:flex; gap:.4rem; overflow-x:auto; padding:.5rem; }
  #channels h2 { display:none; }
  .cam { width:132px; flex:none; margin-bottom:0; }
  #stage { grid-column:1; }
  #timeline { grid-column:1; }
}
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <h1>Timelapsed</h1>
  <a class="navlink" id="librarylink" href="/library" hidden>People &amp; plates</a>
  <span class="spacer"></span>
  <span class="stat" id="stat"></span>
</header>

<aside id="channels"><h2>Cameras</h2></aside>

<main id="stage">
  <div id="screen">
    <div id="placeholder">Select a clip on the timeline below.</div>
    <button id="tapplay" hidden><span class="glyph">&#9654;</span><span>Tap to play</span></button>
    <span id="skipflash" hidden></span>
  </div>
  <div id="transport" class="tlbar" hidden></div>
  <div id="nowplaying"></div>
</main>

<section id="timeline">
  <div class="tlbar">
    <div class="grp" id="ranges"></div>
    <div class="grp"><button id="panl" title="Back">&#9664;</button><button id="panr" title="Forward">&#9654;</button></div>
    <div class="grp" id="cadences"></div>
    <span class="spacer"></span>
    <span class="range" id="viewrange"></span>
    <div class="grp"><button id="tz" aria-pressed="false">UTC</button></div>
  </div>
  <div id="lanes"></div>
  <div id="axis"></div>
  <div id="empty" hidden>Nothing rendered for this camera yet. Clips appear as each cadence rolls over.</div>
</section>

<noscript><p style="padding:1rem">This viewer needs JavaScript. The raw list is at <a href="/api/timelapses">/api/timelapses</a>.</p></noscript>

<script>
const ENTRIES = JSON.parse(document.getElementById("payload").textContent).map(e => ({
  ...e, s: Date.parse(e.starts), f: Date.parse(e.finishes),
}));
const CADENCES = JSON.parse(document.getElementById("cadences-payload").textContent);
const MIN = 60e3, HOUR = 60 * MIN, DAY = 24 * HOUR;
const RANGES = [["6h", 6 * HOUR], ["24h", DAY], ["7d", 7 * DAY], ["30d", 30 * DAY], ["90d", 90 * DAY], ["1y", 365 * DAY], ["All", 0]];
const TICKS = [5 * MIN, 15 * MIN, 30 * MIN, HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY, 90 * DAY, 180 * DAY, 365 * DAY];

const channels = JSON.parse(document.getElementById("channels-payload").textContent);
const HAS_RECOGNITION = JSON.parse(document.getElementById("recognition-payload").textContent);
const FPS = JSON.parse(document.getElementById("fps-payload").textContent);
const params = new URLSearchParams(location.search);
const KINDS = ["person", "vehicle"];
const KIND_LABEL = {person: "people", vehicle: "vehicles"};

const state = {
  channel: params.get("channel") && channels.includes(params.get("channel")) ? params.get("channel") : channels[0] || null,
  show: Object.fromEntries(CADENCES.map(c => [c, true])),
  utc: true,
  start: 0, end: 0,
  selected: null,
  // Recognition state is kept apart from ENTRIES on purpose: clip selection is
  // reference equality against those objects, so anything that re-derives or
  // re-maps them silently breaks selection and arrow-key stepping.
  activity: null,
  events: [],
  identities: [],
  plates: [],
  focusIdentity: null,
  selectedEvent: null,
  analysedThrough: null,
};

const $ = id => document.getElementById(id);
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
// Only the known cadences get a colour; anything else falls back, so a stray
// filename on disk can never inject a CSS value.
const cadenceColour = name => CADENCES.includes(name) ? "var(--" + name + ")" : "var(--muted)";
const forChannel = () => ENTRIES.filter(e => e.channel === state.channel);
// Cache-busted rather than cached: the still behind it changes every capture.
const thumbUrl = id => "/thumb/" + encodeURIComponent(id) + ".jpg?t=" + Date.now();
const visible = () => forChannel().filter(e => state.show[e.cadence]);

function fmtSize(bytes) {
  const mb = bytes / 1048576;
  return mb >= 1 ? mb.toFixed(1) + " MB" : Math.round(bytes / 1024) + " KB";
}
function parts(ms) {
  const d = new Date(ms);
  return state.utc
    ? {y: d.getUTCFullYear(), mo: d.getUTCMonth(), da: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), wd: d.getUTCDay()}
    : {y: d.getFullYear(), mo: d.getMonth(), da: d.getDate(), h: d.getHours(), mi: d.getMinutes(), wd: d.getDay()};
}
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const pad = n => String(n).padStart(2, "0");
const clock = p => pad(p.h) + ":" + pad(p.mi);
const datestr = p => DAYS[p.wd] + " " + p.da + " " + MONTHS[p.mo];
const fmtFull = ms => { const p = parts(ms); return datestr(p) + " " + p.y + ", " + clock(p) + (state.utc ? " UTC" : ""); };
const fmtShort = ms => { const p = parts(ms); return datestr(p) + " " + clock(p); };

// A timelapse is a linear compression of its window, so wall-clock time and
// position in the video are the same coordinate at two scales. Every agreement
// between the player and the timeline goes through this pair: toMedia to act on
// a moment, toMoment to report one back.
//
// Linear because select_frames samples at even intervals -- but it samples
// across the stills that *exist*, so a gap in capture compresses unevenly and
// the mapping drifts across it. Correcting that needs a per-clip frame-time
// sidecar written at render; until then the drift is visible in the playhead
// rather than hidden as it was when nothing read the clock back.
const spanOf = e => e.f - e.s;
const clamp01 = x => Math.min(Math.max(x, 0), 1);
const toMedia = (e, atMs, duration) => clamp01((atMs - e.s) / spanOf(e)) * duration;
const toMoment = (e, t, duration) => e.s + (t / duration) * spanOf(e);

function setView(span) {
  const all = forChannel();
  if (!all.length) { const now = Date.now(); state.start = now - DAY; state.end = now; return; }
  const last = Math.max(...all.map(e => e.f));
  const first = Math.min(...all.map(e => e.s));
  if (!span) { const pad = Math.max((last - first) * 0.03, 30 * MIN); state.start = first - pad; state.end = last + pad; }
  else { state.end = Math.max(last, Date.now()) + span * 0.03; state.start = state.end - span; }
}
function clampView() {
  const span = state.end - state.start;
  if (span < 15 * MIN) { const mid = (state.start + state.end) / 2; state.start = mid - 7.5 * MIN; state.end = mid + 7.5 * MIN; }
}

// Built once. Selecting a camera only flips aria-pressed, because rebuilding the
// wall would hand every tile a fresh <img> and blank the whole thing while six
// thumbnails refetched.
function buildChannels() {
  const box = $("channels");
  for (const id of channels) {
    const n = ENTRIES.filter(e => e.channel === id).length;
    const b = document.createElement("button");
    b.className = "cam";
    b.dataset.channel = id;

    const thumb = el("span", "thumb blank");
    thumb.appendChild(newThumbImage(id, () => thumb.classList.remove("blank")));

    // textContent throughout: channel ids and cadence names come from filenames
    // on disk, so they are never interpolated into markup.
    const row = el("span", "row");
    row.append(el("span", "led"), el("span", "n", "Camera " + id), el("span", "c", String(n)));

    b.append(thumb, row);
    b.onclick = () => {
      if (state.channel === id) return;
      state.channel = id;
      state.selected = null;
      setView(state.end - state.start || DAY);
      syncChannels();
      drawTimeline();
      drawNowPlaying();
    };
    box.appendChild(b);
  }
  syncChannels();
}

// onReady fires once the image has decoded, which is when it is safe to put it
// on the page. src is assigned last so no handler is attached after the load.
function newThumbImage(id, onReady) {
  const img = document.createElement("img");
  img.alt = "";
  img.decoding = "async";
  img.onload = () => onReady(img);
  img.onerror = () => {
    const parent = img.parentNode;
    img.remove();
    if (parent) parent.classList.add("blank");
  };
  img.src = thumbUrl(id);
  return img;
}

function syncChannels() {
  for (const card of document.querySelectorAll(".cam")) {
    card.setAttribute("aria-pressed", String(card.dataset.channel === state.channel));
  }
}

function drawControls() {
  const rbox = $("ranges");
  rbox.innerHTML = "";
  for (const [label, span] of RANGES) {
    const b = document.createElement("button");
    b.textContent = label;
    b.onclick = () => { setView(span); drawTimeline(); };
    rbox.appendChild(b);
  }
  const cbox = $("cadences");
  cbox.innerHTML = "";
  for (const c of CADENCES) {
    const b = document.createElement("button");
    b.className = "toggle";
    b.dataset.cadence = c;
    b.setAttribute("aria-pressed", String(state.show[c]));
    const swatch = el("span", "sw");
    swatch.style.background = cadenceColour(c);
    b.append(swatch, document.createTextNode(c));
    b.onclick = () => { state.show[c] = !state.show[c]; b.setAttribute("aria-pressed", String(state.show[c])); drawTimeline(); };
    cbox.appendChild(b);
  }
  $("panl").onclick = () => pan(-0.5);
  $("panr").onclick = () => pan(0.5);
  $("tz").onclick = () => {
    state.utc = !state.utc;
    $("tz").textContent = state.utc ? "UTC" : "Local";
    $("tz").setAttribute("aria-pressed", String(!state.utc));
    drawTimeline(); drawNowPlaying();
  };
}
function pan(fraction) {
  const span = state.end - state.start;
  state.start += span * fraction; state.end += span * fraction;
  drawTimeline();
}

// --- recognition ---

// Activity depends on the viewport, so it is fetched rather than embedded. Pan
// and zoom fire continuously; without debouncing a drag would queue a request
// per frame and the answers would arrive out of order.
let activityTimer = null;
let activityToken = 0;
let activityKey = null;

function refreshActivity() {
  if (!HAS_RECOGNITION || !state.channel) return;
  const start = Math.floor(state.start / 1000), end = Math.ceil(state.end / 1000);
  const key = state.channel + "|" + start + "|" + end;
  // Same window as last time means nothing to fetch. This is also what makes it
  // safe for drawTimeline to call this unconditionally: the redraw that follows
  // a fetch asks for the same window and stops here instead of looping.
  if (key === activityKey) return;
  activityKey = key;

  clearTimeout(activityTimer);
  activityTimer = setTimeout(async () => {
    const token = ++activityToken;
    const query = "channel=" + encodeURIComponent(state.channel) + "&start=" + start + "&end=" + end;
    try {
      const [activity, events, status] = await Promise.all([
        fetch("/api/activity?" + query + "&buckets=240").then(r => r.json()),
        fetch("/api/events?" + query + "&limit=800").then(r => r.json()),
        fetch("/api/status").then(r => r.json()),
      ]);
      // A slower earlier request must not overwrite a newer answer.
      if (token !== activityToken) return;
      state.activity = activity;
      state.events = events.map(e => ({...e, s: Date.parse(e.starts), f: Date.parse(e.finishes)}));
      const through = status[state.channel];
      state.analysedThrough = through ? Date.parse(through) : null;
      drawTimeline();
    } catch (err) {
      console.warn("activity fetch failed", err);
    }
  }, 120);
}


function drawActivityLanes(lanes, pct) {
  if (!HAS_RECOGNITION) return;

  for (const kind of KINDS) {
    const lane = el("div", "lane activity");
    lane.append(el("span", "label", KIND_LABEL[kind]));
    const track = el("div", "track");

    const counts = state.activity ? state.activity[kind] : null;
    if (counts && counts.length) {
      const peak = Math.max(...counts, 1);
      const width = 100 / counts.length;
      counts.forEach((count, index) => {
        if (!count) return;
        const bar = el("div", "bucket");
        bar.style.left = (index * width) + "%";
        bar.style.width = width + "%";
        // Floor the alpha so a single sighting is still visible against the
        // track, rather than fading to nothing next to a busy bucket.
        bar.style.background = "var(--" + kind + ")";
        bar.style.opacity = String(0.25 + 0.75 * (count / peak));
        track.appendChild(bar);
      });
    }

    // Individual events sit on top of the density, so a sighting stays
    // clickable even where the strip is dense.
    const focused = state.focusIdentity;
    for (const event of state.events) {
      if (event.kind !== kind) continue;
      if (event.f < state.start || event.s > state.end) continue;
      if (focused !== null && event.identity_id !== focused) continue;
      const mark = el("div", "evt" + (state.selectedEvent === event.id ? " sel" : ""));
      mark.style.left = pct(event.s) + "%";
      mark.style.width = Math.max(pct(event.f) - pct(event.s), 0.2) + "%";
      mark.style.background = "var(--" + kind + ")";
      mark.title = kind + "  " + fmtFull(event.s) + "  to  " + fmtFull(event.f)
        + "  (" + event.frame_count + " frames)";
      mark.onclick = ev => { ev.stopPropagation(); selectEvent(event); };
      // The player tick lights these as the playhead passes through them. It
      // only ever touches nodes it was handed here, never the DOM at large.
      liveMarks.push({node: mark, event});
      track.appendChild(mark);
    }

    // An empty lane and an unanalysed one look identical, and during a backfill
    // most of them are the latter. Shade what has not been reached yet and say
    // so, rather than letting it read as "nothing was there".
    const through = state.analysedThrough;
    if (through != null && through < state.end) {
      const pending = el("div", "pending");
      pending.style.left = Math.max(pct(through), 0) + "%";
      pending.style.right = "0";
      if (through <= state.start) {
        pending.append(el("span", "", "not analysed yet · through " + fmtShort(through)));
      }
      track.appendChild(pending);
    }

    // The whole lane is the target, not just the marks. What reads as clickable
    // is the density shading, and that is drawn from bucket counts rather than
    // from individual events -- so clicking it has to resolve to the nearest
    // sighting itself. Without this you get the grab cursor over something that
    // plainly looks like a button.
    const inLane = state.events.filter(e => e.kind === kind);
    if (inLane.length) {
      track.classList.add("clickable");
      track.onclick = ev => {
        if (ev.target.classList.contains("evt")) return;  // the mark handles itself
        const box = track.getBoundingClientRect();
        const at = state.start + ((ev.clientX - box.left) / box.width) * (state.end - state.start);
        const distance = e => (e.s <= at && at <= e.f) ? 0 : Math.min(Math.abs(e.s - at), Math.abs(e.f - at));
        const nearest = inLane.reduce((best, e) => distance(e) < distance(best) ? e : best);
        selectEvent(nearest);
      };
    }

    lane.appendChild(track);
    lanes.appendChild(lane);
  }
}

function selectEvent(event) {
  state.selectedEvent = event.id;
  // Play the footage of this sighting, seeked to the moment it happened.
  // Shortest covering clip first: an hourly compresses an hour into 60s, so it
  // gives far finer resolution on the moment than the daily covering the same
  // instant. Fall back past the cadence toggles rather than refusing to play.
  const covers = e => e.s <= event.s && e.f >= event.s;
  const byLength = (a, b) => (a.f - a.s) - (b.f - b.s);
  const covering = visible().filter(covers).sort(byLength)[0]
    || forChannel().filter(covers).sort(byLength)[0];

  if (covering) {
    select(covering, event.s);
  } else {
    // Nothing rendered covers this moment -- an hourly aged out, or the window
    // has not been rendered yet. Say so instead of appearing to ignore a click.
    drawTimeline();
    const box = $("nowplaying");
    box.textContent = "";
    box.append(
      el("span", "tag", event.kind),
      el("span", "when", fmtFull(event.s) + "  →  " + fmtFull(event.f)),
      el("span", "", "no rendered clip covers this sighting"),
    );
    return;
  }
  drawTimeline();
}




function drawTimeline() {
  clampView();
  const span = state.end - state.start;
  const pct = ms => ((ms - state.start) / span) * 100;
  const lanes = $("lanes");
  lanes.innerHTML = "";
  // The tick writes to these and nothing else. The redraw that just destroyed
  // them is what owns handing over the replacements.
  playheadEl = null; bufferedEl = null; liveMarks = [];

  const entries = visible();
  $("empty").hidden = forChannel().length > 0;

  for (const cadence of CADENCES) {
    if (!state.show[cadence]) continue;
    const lane = document.createElement("div");
    lane.className = "lane";
    lane.append(el("span", "label", cadence));
    const track = document.createElement("div");
    track.className = "track";
    for (const e of entries.filter(x => x.cadence === cadence)) {
      if (e.f < state.start || e.s > state.end) continue;
      const clip = document.createElement("div");
      clip.className = "clip" + (state.selected === e ? " sel" : "");
      clip.style.left = pct(e.s) + "%";
      clip.style.width = Math.max(pct(e.f) - pct(e.s), 0.15) + "%";
      clip.style.background = cadenceColour(cadence);
      clip.title = cadence + "  " + fmtFull(e.s) + "  to  " + fmtFull(e.f) + "  (" + fmtSize(e.size_bytes) + ")";
      // Where in the window the click landed is the moment to play from.
      // Every click used to restart the clip at its first frame, so on a weekly
      // you could click Thursday and be shown Monday.
      clip.onclick = ev => { ev.stopPropagation(); select(e, momentAt(clip, e, ev.clientX)); };
      if (state.selected === e) {
        bufferedEl = el("div", "buf");
        bufferedEl.hidden = true;
        clip.appendChild(bufferedEl);
        attachScrub(clip, e);
      }
      track.appendChild(clip);
    }
    lane.appendChild(track);
    lanes.appendChild(lane);
  }

  drawActivityLanes(lanes, pct);

  const now = Date.now();
  if (now >= state.start && now <= state.end) {
    const line = document.createElement("div");
    line.id = "nowline";
    // The tracks start 56px in, past the lane labels, so the line has to too.
    line.style.left = "calc(56px + (100% - 56px) * " + (pct(now) / 100).toFixed(6) + ")";
    lanes.appendChild(line);
  }

  // Positioned by the tick rather than from here: this runs on every pan and
  // wheel event, and the playhead moves on its own clock.
  if (state.selected) {
    playheadEl = document.createElement("div");
    playheadEl.id = "playhead";
    playheadEl.hidden = true;
    lanes.appendChild(playheadEl);
  }

  drawAxis(span, pct);
  $("viewrange").textContent = fmtFull(state.start) + "  →  " + fmtFull(state.end);
  document.querySelectorAll("#ranges button").forEach((b, i) => {
    const target = RANGES[i][1];
    b.setAttribute("aria-pressed", String(target !== 0 && Math.abs(span - target) / target < 0.15));
  });
  $("stat").textContent = entries.length + " clip" + (entries.length === 1 ? "" : "s") + " · "
    + fmtSize(entries.reduce((t, e) => t + e.size_bytes, 0));

  // No-ops unless the viewport actually moved, so every pan, zoom and range
  // button picks up new activity without each one having to remember to ask.
  refreshActivity();

  // Put the playhead, the buffered shading and the live sightings back where
  // playback actually is, rather than waiting for the next animation frame --
  // which for a paused player would never come.
  tickPlayer();
}

function drawAxis(span, pct) {
  const axis = $("axis");
  axis.innerHTML = "";
  const step = TICKS.find(t => span / t <= 10) || TICKS[TICKS.length - 1];
  const offset = state.utc ? 0 : new Date().getTimezoneOffset() * -MIN;
  for (let t = Math.ceil((state.start + offset) / step) * step - offset; t <= state.end; t += step) {
    const p = parts(t);
    const el = document.createElement("span");
    el.style.left = pct(t) + "%";
    el.textContent = step < DAY ? clock(p) : (step < 30 * DAY ? datestr(p) : MONTHS[p.mo] + " " + p.y);
    axis.appendChild(el);
  }
}

// --- the player -------------------------------------------------------------

// One <video> for the life of the page. Building a fresh one per selection threw
// away the buffer, refetched the moov and restarted from zero, which is what
// made clicking a sighting inside the clip already on screen look like a dead
// click. Swapping .src is the only thing a new selection needs.
const video = document.createElement("video");
video.muted = true; video.playsInline = true; video.loop = true;
// "metadata", not "auto". These clips are dense -- 1800 frames in 60s runs to
// tens of MB -- and "auto" starts pulling from byte zero immediately. On a deep
// link that download is wasted: the seek cannot be applied until the moov
// arrives, and then playback restarts from somewhere else entirely. Asking for
// metadata alone gets the moov (it is at the front, thanks to
// -movflags +faststart), fires loadedmetadata quickly, and lets buffering begin
// at the moment actually being looked at.
video.preload = "metadata";
$("screen").appendChild(video);

// Never played and never on screen: this exists so the successor's moov is
// already in hand when a clip ends. Without it every seam stalls on a round
// trip, which on hourly clips is once a minute.
const prefetch = document.createElement("video");
prefetch.preload = "metadata"; prefetch.muted = true;
let prefetchFor = null;

const duration = () => (video.duration && isFinite(video.duration)) ? video.duration : 0;
// The wall-clock moment currently on screen. Null until a clip is loaded and its
// duration known, because without a duration there is nothing to scale by.
const momentNow = () => (state.selected && duration())
  ? toMoment(state.selected, video.currentTime, duration()) : null;

// Muted autoplay is normally allowed, but not on every browser and not after
// every navigation. A refusal that leaves the picture sitting paused is exactly
// the failure this player was written to fix, so surface it and take the tap.
//
// A refusal has to be told apart from an AbortError, which is not one. That is
// what a pending play() reports when a newer src, seek or pause supersedes it,
// and this player supersedes its own play() constantly -- selecting a clip
// assigns .src while the previous play() is still settling. The rejection then
// arrives after the new play() has already succeeded, so treating every
// rejection as a refusal put the overlay up over a video playing perfectly well
// underneath it. Only NotAllowedError is the browser saying no.
let blocked = false;

function play() {
  const started = video.play();
  if (!started) return;
  started.catch(error => {
    if (error.name !== "NotAllowedError") return;
    blocked = true;
    tickPlayer();
  });
}

function togglePlay() {
  if (video.paused) play();
  else { blocked = false; video.pause(); }
}

// Ground truth, whatever the promises claimed: frames are reaching the screen.
video.addEventListener("playing", () => { blocked = false; tickPlayer(); });

function seekToMoment(entry, atMs) {
  if (atMs == null || !spanOf(entry)) return;
  const apply = () => { if (duration()) video.currentTime = toMedia(entry, atMs, duration()); };
  if (video.readyState >= 1) apply();
  else video.addEventListener("loadedmetadata", apply, {once: true});
}

// The next clip of the same cadence on this channel, in time order. Arrow-key
// stepping and end-of-clip chaining are the same question, asked twice.
function neighbour(entry, direction) {
  const pool = forChannel().filter(e => e.cadence === entry.cadence).sort((a, b) => a.s - b.s);
  const at = pool.indexOf(entry);
  return at < 0 ? null : (pool[at + direction] || null);
}

function select(entry, atMs) {
  const changed = state.selected !== entry;
  state.selected = entry;

  if (changed) {
    $("placeholder").hidden = true;
    $("transport").hidden = false;
    // Loop only where there is nowhere to go. With a successor the clip has to
    // be allowed to end, because "ended" is what hands over to it.
    video.loop = !neighbour(entry, 1);
    video.src = entry.url;
    prefetchFor = null;
  }
  seekToMoment(entry, atMs);
  play();
  if (changed) { drawNowPlaying(); drawTimeline(); }
  tickPlayer();
}

// Roll into the next hour rather than replaying this one.
video.addEventListener("ended", () => {
  const next = state.selected && neighbour(state.selected, 1);
  if (next) select(next, next.s);
});

// Warm the successor before the seam, not at it. timeupdate is coarse (~4Hz),
// which is ample for a decision made once per clip.
video.addEventListener("timeupdate", () => {
  if (!state.selected || !duration() || video.currentTime < duration() * 0.8) return;
  const next = neighbour(state.selected, 1);
  if (next && prefetchFor !== next.url) { prefetchFor = next.url; prefetch.src = next.url; }
});

// Double tap a side to jump, single tap the middle to play or pause -- the
// gesture every phone video player has trained people to expect.
//
// A single tap therefore cannot act immediately: a double tap is two clicks, so
// committing to the toggle on the first one would toggle twice on the way to the
// seek. It waits out the double-tap window instead. The transport button has no
// such delay, so there is always an instant way to pause.
const DOUBLE_TAP_MS = 280;
const SIDE_FRACTION = 0.3;
let tapTimer = null, tapSide = null;

function flashSkip(direction) {
  const flash = $("skipflash");
  flash.hidden = false;
  flash.className = direction < 0 ? "back" : "fwd";
  flash.textContent = (direction < 0 ? "\u00AB " : "") + SKIP_SECONDS + "s"
                    + (direction > 0 ? " \u00BB" : "");
  // Restarting a CSS animation needs the element out of the tree and back, or
  // a forced reflow. This is the cheap half of that.
  flash.style.animation = "none";
  void flash.offsetWidth;
  flash.style.animation = "";
}

$("screen").onclick = ev => {
  if (ev.target !== video) return;
  const box = video.getBoundingClientRect();
  const across = (ev.clientX - box.left) / box.width;
  const side = across < SIDE_FRACTION ? -1 : across > 1 - SIDE_FRACTION ? 1 : 0;

  if (tapTimer && side !== 0 && side === tapSide) {
    clearTimeout(tapTimer);
    tapTimer = null;
    seekBy(side * SKIP_SECONDS);
    flashSkip(side);
    return;
  }
  clearTimeout(tapTimer);
  tapSide = side;
  tapTimer = setTimeout(() => { tapTimer = null; togglePlay(); }, DOUBLE_TAP_MS);
};
$("tapplay").onclick = () => play();

// Ten seconds of the video, not of the world. On a weekly clip those are very
// different amounts -- ten seconds of a seven-day render is most of a day -- but
// the reason to reach for this control is "I missed something, back it up", and
// that is a distance measured in what you were just watching. The clock in the
// bar says what it came to in world time.
const SKIP_SECONDS = 10;

function seekBy(seconds) {
  if (!state.selected || !duration()) return;
  video.currentTime = Math.min(Math.max(video.currentTime + seconds, 0), duration());
  tickPlayer();
}

// The renders are fixed-rate and the rate is per cadence, so a frame is a known
// slice of media time. FPS comes from the server because an MP4 carries no frame
// rate a media element will admit to.
function frameStep(direction) {
  if (!state.selected || !duration()) return;
  video.pause();
  const fps = FPS[state.selected.cadence] || 30;
  video.currentTime = Math.min(Math.max(video.currentTime + direction / fps, 0), duration());
}

// Where along a clip a pointer is, expressed as the wall-clock moment there.
const momentAt = (clip, entry, clientX) => {
  const box = clip.getBoundingClientRect();
  return entry.s + clamp01((clientX - box.left) / box.width) * spanOf(entry);
};

// Dragging the selected clip scrubs it. #lanes owns the pointer everywhere else
// and pans with it, so this has to claim the event before it gets that far.
// It deliberately does not preventDefault: the click still lands afterwards,
// and that is what resumes playback wherever the drag was let go.
function attachScrub(clip, entry) {
  let scrubbing = false;
  clip.addEventListener("pointerdown", ev => {
    ev.stopPropagation();
    scrubbing = true;
    clip.setPointerCapture(ev.pointerId);
    // Seeking every frame of a drag while decoding is thrash, and the picture
    // cannot keep up anyway. The click at the end starts it again.
    video.pause();
    seekToMoment(entry, momentAt(clip, entry, ev.clientX));
    tickPlayer();
  });
  clip.addEventListener("pointermove", ev => {
    if (!scrubbing) return;
    seekToMoment(entry, momentAt(clip, entry, ev.clientX));
    tickPlayer();
  });
  const release = ev => {
    if (!scrubbing) return;
    scrubbing = false;
    if (clip.hasPointerCapture(ev.pointerId)) clip.releasePointerCapture(ev.pointerId);
  };
  clip.addEventListener("pointerup", release);
  clip.addEventListener("pointercancel", release);
}

// --- the tick ---------------------------------------------------------------

// Everything below writes only to nodes drawTimeline handed over. It must never
// call drawTimeline itself: that empties #lanes and rebuilds every clip, bucket
// and mark, which at animation rate is a redraw storm and would tear a node out
// from under the click that is landing on it.
let playheadEl = null, bufferedEl = null, liveMarks = [];
let pumping = false;

function tickPlayer() {
  const at = momentNow();
  $("tapplay").hidden = !blocked;

  const clock = $("atclock");
  if (clock) clock.textContent = at == null ? "" : fmtFull(at);
  const button = $("playpause");
  if (button) {
    button.textContent = video.paused ? "▶" : "⏸";
    button.title = (video.paused ? "Play" : "Pause") + " (space)";
  }

  if (playheadEl) {
    const fraction = at == null ? -1 : (at - state.start) / (state.end - state.start);
    playheadEl.hidden = fraction < 0 || fraction > 1;
    // The tracks start 56px in, past the lane labels, so the line has to too.
    if (!playheadEl.hidden) {
      playheadEl.style.left = "calc(56px + (100% - 56px) * " + fraction.toFixed(6) + ")";
    }
  }

  if (bufferedEl && duration()) {
    // Only the range holding the playhead. The others are some earlier seek's
    // leavings, and shading them would claim the clip is ready when it is not.
    let found = false;
    for (let i = 0; i < video.buffered.length && !found; i++) {
      const from = video.buffered.start(i), to = video.buffered.end(i);
      if (from > video.currentTime || to < video.currentTime) continue;
      bufferedEl.style.left = (from / duration() * 100) + "%";
      bufferedEl.style.width = ((to - from) / duration() * 100) + "%";
      found = true;
    }
    bufferedEl.hidden = !found;
  }

  for (const mark of liveMarks) {
    mark.node.classList.toggle("live", at != null && mark.event.s <= at && at <= mark.event.f);
  }
}

// requestAnimationFrame rather than timeupdate: timeupdate fires around 4Hz,
// and at a six-hour zoom that reads as a playhead lurching rather than moving.
function pump() {
  tickPlayer();
  pumping = !video.paused;
  if (pumping) requestAnimationFrame(pump);
}
video.addEventListener("play", () => { if (!pumping) { pumping = true; requestAnimationFrame(pump); } });
for (const event of ["pause", "seeked", "loadedmetadata", "progress", "ratechange", "playing"]) {
  video.addEventListener(event, tickPlayer);
}

// --- the transport ----------------------------------------------------------

const RATES = [0.5, 1, 2, 4];

function syncRates() {
  for (const button of document.querySelectorAll("#transport [data-rate]")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.rate) === video.playbackRate));
  }
}

function drawTransport() {
  const bar = $("transport");
  bar.textContent = "";

  const transport = el("div", "grp");
  const playpause = el("button");
  playpause.id = "playpause";
  playpause.onclick = togglePlay;
  transport.appendChild(playpause);

  // Ten seconds either way, and the same jump the sides of the picture make.
  const skips = el("div", "grp");
  for (const direction of [-1, 1]) {
    const button = el("button", "",
      (direction < 0 ? "\u21BA " : "") + SKIP_SECONDS + (direction > 0 ? " \u21BB" : ""));
    button.title = (direction < 0 ? "Back " : "Forward ") + SKIP_SECONDS
                 + "s (double tap the " + (direction < 0 ? "left" : "right") + " of the picture)";
    button.onclick = () => { seekBy(direction * SKIP_SECONDS); flashSkip(direction); };
    skips.appendChild(button);
  }

  // A frame at a time is how you read a plate off a still that went past in
  // two hundredths of a second.
  const frames = el("div", "grp");
  for (const [glyph, direction, title] of [["◀◀", -1, "Back a frame (,)"],
                                           ["▶▶", 1, "On a frame (.)"]]) {
    const button = el("button", "", glyph);
    button.title = title;
    button.onclick = () => frameStep(direction);
    frames.appendChild(button);
  }

  // Rates earn their place here in a way they would not on ordinary footage: a
  // weekly is seven days in sixty seconds, and half speed is the only way to
  // watch it.
  const rates = el("div", "grp");
  for (const rate of RATES) {
    const button = el("button", "", rate + "×");
    button.dataset.rate = String(rate);
    button.onclick = () => { video.playbackRate = rate; syncRates(); };
    rates.appendChild(button);
  }

  // What time it is on screen. Nothing in the picture says so, and on a weekly
  // clip a second of video is nearly three hours of the world.
  const clock = el("span", "at");
  clock.id = "atclock";

  // The native controls were the only way to full screen, so removing them
  // without this would be a straight regression.
  const view = el("div", "grp");
  const full = el("button", "", "⛶");
  full.title = "Full screen";
  full.onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else $("screen").requestFullscreen?.();
  };
  view.appendChild(full);

  bar.append(transport, skips, frames, rates, clock, el("span", "spacer"), view);
  syncRates();
}

function drawNowPlaying() {
  const box = $("nowplaying");
  const e = state.selected;
  if (!e) { box.innerHTML = ""; return; }
  box.textContent = "";
  const tag = el("span", "tag", e.cadence);
  tag.style.background = cadenceColour(e.cadence);
  const link = el("a", "", "Download");
  link.href = e.url;
  link.download = "";
  box.append(tag, el("span", "", "Camera " + e.channel),
             el("span", "when", fmtFull(e.s) + "  \u2192  " + fmtFull(e.f)),
             el("span", "", fmtSize(e.size_bytes)), link);
}

function step(direction) {
  if (!state.selected) {
    const pool = visible().sort((a, b) => a.s - b.s);
    if (pool.length) select(pool[0]);
    return;
  }
  const next = neighbour(state.selected, direction);
  if (!next) return;
  select(next);
  // Follow it if it fell outside the window, rather than selecting something
  // the reader cannot see.
  if (next.s < state.start || next.f > state.end) {
    const span = state.end - state.start;
    const mid = (next.s + next.f) / 2;
    state.start = mid - span / 2; state.end = mid + span / 2;
    drawTimeline();
  }
}

const lanesEl = $("lanes");
const DRAG_SLOP = 3;
let drag = null;
lanesEl.addEventListener("pointerdown", ev => {
  drag = {x: ev.clientX, id: ev.pointerId, start: state.start, end: state.end, moved: false};
});
// Panning only begins past a few pixels of slop, and the pointer is captured at
// that same moment. Both matter for clicking a clip: a redraw on every stray
// pixel would rip the clip out of the DOM before mouseup, and a capture held
// from pointerdown would retarget the click onto the lane strip itself.
lanesEl.addEventListener("pointermove", ev => {
  if (!drag) return;
  const dx = ev.clientX - drag.x;
  if (!drag.moved) {
    if (Math.abs(dx) <= DRAG_SLOP) return;
    drag.moved = true;
    lanesEl.setPointerCapture(drag.id);
    lanesEl.classList.add("dragging");
  }
  const span = drag.end - drag.start;
  const shift = (dx / lanesEl.clientWidth) * span;
  state.start = drag.start - shift; state.end = drag.end - shift;
  drawTimeline();
});
const endDrag = () => { drag = null; lanesEl.classList.remove("dragging"); };
lanesEl.addEventListener("pointerup", endDrag);
lanesEl.addEventListener("pointercancel", endDrag);
lanesEl.addEventListener("wheel", ev => {
  ev.preventDefault();
  const rect = lanesEl.getBoundingClientRect();
  const at = Math.min(Math.max((ev.clientX - rect.left - 56) / (rect.width - 56), 0), 1);
  const span = state.end - state.start;
  const factor = ev.deltaY > 0 ? 1.25 : 0.8;
  const focus = state.start + span * at;
  state.start = focus - span * factor * at;
  state.end = focus + span * factor * (1 - at);
  drawTimeline();
}, {passive: false});

addEventListener("keydown", ev => {
  if (ev.target.tagName === "INPUT") return;
  if (ev.key === "ArrowLeft") { step(-1); ev.preventDefault(); }
  else if (ev.key === "ArrowRight") { step(1); ev.preventDefault(); }
  // Space is a button's own activation key, so leave it alone while one has
  // focus: clicking play and then pressing space would otherwise toggle twice.
  else if (ev.key === " " && ev.target.tagName !== "BUTTON") { togglePlay(); ev.preventDefault(); }
  else if (ev.key === ",") { frameStep(-1); ev.preventDefault(); }
  else if (ev.key === ".") { frameStep(1); ev.preventDefault(); }
});

// Keep the wall roughly live. The replacement is decoded off-screen and only
// swapped in once it has loaded, so a tile never flashes empty mid-refresh.
const THUMB_REFRESH = 30e3;
setInterval(() => {
  if (document.hidden) return;
  for (const card of document.querySelectorAll(".cam")) {
    const thumb = card.querySelector(".thumb");
    if (!thumb) continue;
    newThumbImage(card.dataset.channel, next => {
      const current = thumb.querySelector("img");
      thumb.classList.remove("blank");
      if (current) current.replaceWith(next);
      else thumb.appendChild(next);
    });
  }
}, THUMB_REFRESH);

if (HAS_RECOGNITION) $("librarylink").hidden = false;

drawControls();
drawTransport();
buildChannels();

// ?at= is how the library page hands a sighting back to the viewer: centre the
// timeline on that moment and start the covering clip there, rather than
// dropping the reader at the newest clip and making them hunt for it.
const deepLinkAt = Number(params.get("at"));
if (deepLinkAt) {
  state.start = deepLinkAt - 30 * MIN;
  state.end = deepLinkAt + 30 * MIN;
  const identity = Number(params.get("identity"));
  if (identity) state.focusIdentity = identity;
} else {
  setView(DAY);
}
drawTimeline();
drawNowPlaying();

const covers = e => e.s <= deepLinkAt && e.f >= deepLinkAt;
const target = deepLinkAt
  ? (visible().filter(covers).sort((a, b) => (a.f - a.s) - (b.f - b.s))[0]
     || forChannel().filter(covers).sort((a, b) => (a.f - a.s) - (b.f - b.s))[0])
  : visible().sort((a, b) => b.s - a.s)[0];
if (target) select(target, deepLinkAt || undefined);
</script>
</body>
</html>
"""


def render_index(
    catalogue: TimelapseCatalogue,
    channel_id: str | None,
    cadence: str | None,
    recognition: "RecognitionReader | None" = None,
    fps_by_cadence: dict[str, int] | None = None,
) -> bytes:
    """The viewer shell with the whole catalogue embedded.

    Everything is served in one request: the catalogue is small (retention caps
    it at a few thousand entries) and embedding it means no second round trip
    and no loading state. The page filters and lays out client-side.
    """
    entries = catalogue.entries(channel_id, cadence)

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
    page = PAGE_TEMPLATE.replace(
        "<script>\nconst ENTRIES",
        block("payload", [entry.as_dict() for entry in entries])
        + "\n"
        + block("channels-payload", channels)
        + "\n"
        + block("recognition-payload", recognition is not None)
        + "\n"
        # Lane order, colour lookup and filter chips all read this one array,
        # so the registry stays the single source of truth and a cadence added
        # there gets a lane without touching the viewer. Reversed: widest on top.
        + block("cadences-payload", list(reversed(CADENCES)))
        + "\n"
        # Frame stepping needs the rate the clip was rendered at, and that is a
        # per-cadence setting the browser has no way to find out: an MP4 carries
        # no frame rate the media element will admit to. Send it rather than
        # letting the player guess at 30 and land between frames.
        + block("fps-payload", fps_by_cadence or {})
        + "\n<script>\nconst ENTRIES",
    )
    return page.encode()


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
        fps_by_cadence: dict[str, int] | None = None,
        **kwargs,
    ):
        self.catalogue = catalogue
        self.thumbnails = thumbnails
        self.recognition = recognition
        self.fps_by_cadence = fps_by_cadence or {}
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
                    self.catalogue, query.get("channel"), query.get("cadence"),
                    recognition=self.recognition,
                    fps_by_cadence=self.fps_by_cadence,
                )
                self._send_bytes(body, "text/html; charset=utf-8")
            elif path == "/library":
                if self.recognition is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Recognition is not enabled")
                    return
                self._send_bytes(render_library(), "text/html; charset=utf-8")
            elif path == "/api/timelapses":
                entries = self.catalogue.entries(query.get("channel"), query.get("cadence"))
                body = json.dumps([entry.as_dict() for entry in entries], indent=2).encode()
                self._send_bytes(body, "application/json")
            elif path.startswith("/api/"):
                self._serve_recognition_api(path, query)
            elif path.startswith("/video/"):
                self._serve_video(path)
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
        fps_by_cadence={name: config.output_fps_for(name) for name in CADENCES},
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
