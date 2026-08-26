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
  --hourly:#5b9dff; --daily:#a78bfa; --weekly:#34d399;
}
* { box-sizing:border-box; }
html, body { height:100%; }
body {
  margin:0; background:var(--bg); color:var(--fg); overflow:hidden;
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  display:grid; grid-template-rows:auto minmax(0,1fr) auto; grid-template-columns:190px minmax(0,1fr);
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
  display:flex; align-items:center; gap:.5rem; width:100%; text-align:left; cursor:pointer;
  background:none; border:1px solid transparent; border-radius:8px; padding:.5rem .6rem;
  color:var(--fg); font:inherit; font-size:.85rem;
}
.cam:hover { background:var(--panel-2); }
.cam[aria-pressed="true"] { background:var(--panel-2); border-color:var(--accent); }
.cam .led { width:6px; height:6px; border-radius:50%; background:var(--muted); flex:none; }
.cam[aria-pressed="true"] .led { background:var(--accent); box-shadow:0 0 6px var(--accent); }
.cam .n { flex:1; }
.cam .c { color:var(--muted); font-size:.75rem; font-variant-numeric:tabular-nums; }

#stage { display:flex; flex-direction:column; min-height:0; padding:1rem; gap:.75rem; }
#screen { flex:1; min-height:0; display:flex; align-items:center; justify-content:center; background:#000;
          border:1px solid var(--line); border-radius:10px; overflow:hidden; position:relative; }
/* A <video> is 300x150 until metadata loads, and max-width alone never grows it
     back up; object-fit keeps the aspect ratio while it fills the screen. */
#screen video { width:100%; height:100%; object-fit:contain; display:block; background:#000; }
#placeholder { color:var(--muted); font-size:.85rem; text-align:center; padding:2rem; }
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
.clip.sel { opacity:1; outline:1.5px solid var(--fg); outline-offset:1px; z-index:3; }
#nowline { position:absolute; top:0; bottom:18px; width:1px; background:#ff6b6b; pointer-events:none; z-index:4; }
#axis { position:relative; height:16px; margin-left:56px; }
#axis span { position:absolute; top:0; font-size:.63rem; color:var(--muted); transform:translateX(-50%);
             white-space:nowrap; font-variant-numeric:tabular-nums; }
#axis span::before { content:""; position:absolute; left:50%; top:-4px; height:3px; width:1px; background:var(--line); }
#empty { color:var(--muted); font-size:.8rem; padding:1rem 0 0 56px; }

@media (max-width:760px) {
  body { grid-template-columns:1fr; grid-template-rows:auto auto minmax(0,1fr) auto; }
  header { grid-column:1; }
  #channels { grid-column:1; border-right:none; border-bottom:1px solid var(--line);
              display:flex; gap:.4rem; overflow-x:auto; padding:.5rem; }
  #channels h2 { display:none; }
  .cam { width:auto; flex:none; }
  #stage { grid-column:1; }
  #timeline { grid-column:1; }
}
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <h1>Timelapsed</h1>
  <span class="spacer"></span>
  <span class="stat" id="stat"></span>
</header>

<aside id="channels"><h2>Cameras</h2></aside>

<main id="stage">
  <div id="screen"><div id="placeholder">Select a clip on the timeline below.</div></div>
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
const CADENCES = ["weekly", "daily", "hourly"];
const MIN = 60e3, HOUR = 60 * MIN, DAY = 24 * HOUR;
const RANGES = [["6h", 6 * HOUR], ["24h", DAY], ["7d", 7 * DAY], ["30d", 30 * DAY], ["All", 0]];
const TICKS = [5 * MIN, 15 * MIN, 30 * MIN, HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY, 90 * DAY];

const channels = [...new Set(ENTRIES.map(e => e.channel))].sort((a, b) => a.localeCompare(b, undefined, {numeric: true}));
const params = new URLSearchParams(location.search);

const state = {
  channel: params.get("channel") && channels.includes(params.get("channel")) ? params.get("channel") : channels[0] || null,
  show: Object.fromEntries(CADENCES.map(c => [c, true])),
  utc: true,
  start: 0, end: 0,
  selected: null,
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

function drawChannels() {
  const box = $("channels");
  box.querySelectorAll(".cam").forEach(n => n.remove());
  for (const id of channels) {
    const n = ENTRIES.filter(e => e.channel === id).length;
    const b = document.createElement("button");
    b.className = "cam";
    b.setAttribute("aria-pressed", String(id === state.channel));
    // textContent throughout: channel ids and cadence names come from filenames
    // on disk, so they are never interpolated into markup.
    b.append(el("span", "led"), el("span", "n", "Camera " + id), el("span", "c", String(n)));
    b.onclick = () => { state.channel = id; state.selected = null; setView(state.end - state.start || DAY); drawAll(); };
    box.appendChild(b);
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

function drawTimeline() {
  clampView();
  const span = state.end - state.start;
  const pct = ms => ((ms - state.start) / span) * 100;
  const lanes = $("lanes");
  lanes.innerHTML = "";

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
      clip.onclick = ev => { ev.stopPropagation(); select(e); };
      track.appendChild(clip);
    }
    lane.appendChild(track);
    lanes.appendChild(lane);
  }

  const now = Date.now();
  if (now >= state.start && now <= state.end) {
    const line = document.createElement("div");
    line.id = "nowline";
    // The tracks start 56px in, past the lane labels, so the line has to too.
    line.style.left = "calc(56px + (100% - 56px) * " + (pct(now) / 100).toFixed(6) + ")";
    lanes.appendChild(line);
  }

  drawAxis(span, pct);
  $("viewrange").textContent = fmtFull(state.start) + "  →  " + fmtFull(state.end);
  document.querySelectorAll("#ranges button").forEach((b, i) => {
    const target = RANGES[i][1];
    b.setAttribute("aria-pressed", String(target !== 0 && Math.abs(span - target) / target < 0.15));
  });
  $("stat").textContent = entries.length + " clip" + (entries.length === 1 ? "" : "s") + " · "
    + fmtSize(entries.reduce((t, e) => t + e.size_bytes, 0));
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

function select(entry) {
  state.selected = entry;
  const screen = $("screen");
  screen.innerHTML = "";
  const v = document.createElement("video");
  // Timelapses have no audio track, and muted is what lets autoplay through.
  v.controls = true; v.autoplay = true; v.loop = true; v.muted = true;
  v.playsInline = true; v.preload = "auto";
  v.src = entry.url;
  screen.appendChild(v);
  drawNowPlaying();
  drawTimeline();
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
  const pool = visible().filter(e => !state.selected || e.cadence === state.selected.cadence)
                        .sort((a, b) => a.s - b.s);
  if (!pool.length) return;
  const at = state.selected ? pool.indexOf(state.selected) : -1;
  const next = pool[Math.min(Math.max(at + direction, 0), pool.length - 1)];
  if (next) {
    select(next);
    if (next.s < state.start || next.f > state.end) {
      const span = state.end - state.start;
      const mid = (next.s + next.f) / 2;
      state.start = mid - span / 2; state.end = mid + span / 2;
      drawTimeline();
    }
  }
}

const lanesEl = $("lanes");
let drag = null;
lanesEl.addEventListener("pointerdown", ev => {
  drag = {x: ev.clientX, start: state.start, end: state.end, moved: false};
  lanesEl.setPointerCapture(ev.pointerId);
  lanesEl.classList.add("dragging");
});
lanesEl.addEventListener("pointermove", ev => {
  if (!drag) return;
  const dx = ev.clientX - drag.x;
  if (Math.abs(dx) > 2) drag.moved = true;
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
});

function drawAll() { drawChannels(); drawTimeline(); drawNowPlaying(); }

drawControls();
setView(DAY);
drawAll();
const latest = visible().sort((a, b) => b.s - a.s)[0];
if (latest) select(latest);
</script>
</body>
</html>
"""


def render_index(catalogue: TimelapseCatalogue, channel_id: str | None, cadence: str | None) -> bytes:
    """The viewer shell with the whole catalogue embedded.

    Everything is served in one request: the catalogue is small (retention caps
    it at a few thousand entries) and embedding it means no second round trip
    and no loading state. The page filters and lays out client-side.
    """
    entries = catalogue.entries(channel_id, cadence)
    payload = json.dumps([entry.as_dict() for entry in entries], separators=(",", ":"))
    # </script> inside a script block would close it early; nothing else in JSON can escape.
    payload = payload.replace("</", "<\\/")

    page = PAGE_TEMPLATE.replace(
        '<script>\nconst ENTRIES',
        f'<script type="application/json" id="payload">{payload}</script>\n<script>\nconst ENTRIES',
    )
    return page.encode()


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
