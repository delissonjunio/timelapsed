"""The live wall: every camera at once, as close to now as remuxing allows.

Its own module and its own page for the same reason the library is: it answers
a different question. The timeline asks "what happened"; this asks "what is
happening". The tiles are real video, not polled stills -- go2rtc pulls each
camera's RTSP main stream on demand and remuxes it (no transcode) to whatever
the browser will take, WebRTC or MSE. The NVR encodes HEVC, which every Apple
device here decodes in hardware; Firefox is the one likely casualty.

The page itself stays in the house style: standard library only, one string,
no build step. The player element is go2rtc's own `video-stream.js`, served by
go2rtc itself and reverse-proxied to the same origin as this page under
GO2RTC_PATH -- see the matching location in deploy/nginx-timelapsed.conf,
installed by deploy/go2rtc-setup.sh. tests/test_nginx_config.py holds the two
sides of that path together.

Streams are named `ch{channel}` in /etc/go2rtc.yaml, and go2rtc-setup.sh
derives them from the same [nvr] channels line this page's channel list comes
from, so the two lists cannot drift apart on an installed guest.
"""

import json

GO2RTC_PATH = "/go2rtc/"

FAVICON = (
    "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjA"
    "gMCAzMiAzMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iNyIgZmlsbD0iIzEyMTUxZCIvPjxyZWN0IHdp"
    "ZHRoPSIzMiIgaGVpZ2h0PSIzMiIgcng9IjciIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzI0MmEzNyIvPjxyZWN0IHg9IjYiI"
    "Hk9IjgiIHdpZHRoPSIyMCIgaGVpZ2h0PSI0IiByeD0iMiIgZmlsbD0iIzM0ZDM5OSIvPjxyZWN0IHg9IjYiIHk9IjE0Ii"
    "B3aWR0aD0iMTMiIGhlaWdodD0iNCIgcng9IjIiIGZpbGw9IiNhNzhiZmEiLz48cmVjdCB4PSI2IiB5PSIyMCIgd2lkdGg"
    "9IjciIGhlaWdodD0iNCIgcng9IjIiIGZpbGw9IiM1YjlkZmYiLz48cmVjdCB4PSIyNyIgeT0iNiIgd2lkdGg9IjEiIGhl"
    "aWdodD0iMjAiIGZpbGw9IiNmZjZiNmIiLz48L3N2Zz4="
)

LIVE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0b0d12">
<title>Live &middot; Timelapsed</title>
<link rel="icon" href="__FAVICON__">
<style>
:root {
  color-scheme: dark;
  --bg:#0b0d12; --panel:#12151d; --panel-2:#171b25; --fg:#e8ebf1; --muted:#8b93a5;
  --line:#242a37; --accent:#5b9dff; --live:#ff6b6b;
}
* { box-sizing:border-box; }
html, body { height:100%; }
body {
  margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  display:flex; flex-direction:column;
}
header {
  display:flex; align-items:center; gap:.75rem; flex:none;
  padding:.7rem 1rem; border-bottom:1px solid var(--line); background:var(--panel);
}
header h1 { font-size:1rem; margin:0; letter-spacing:.14em; text-transform:uppercase;
            color:var(--muted); font-weight:700; }
header .dot { width:7px; height:7px; border-radius:50%; background:var(--live);
              box-shadow:0 0 8px var(--live); animation:livepulse 2s ease-in-out infinite; }
@keyframes livepulse { 50% { opacity:.35; } }
header .spacer { flex:1; }
header .navlink { color:var(--muted); text-decoration:none; font-size:.75rem;
                  padding:.3rem .6rem; border-radius:6px; border:1px solid var(--line); }
header .navlink:hover { color:var(--fg); background:var(--panel-2); }
header .stat { color:var(--muted); font-size:.8rem; }

main { flex:1; overflow-y:auto; padding:.75rem; }
#grid { display:grid; gap:.75rem; grid-template-columns:repeat(auto-fit, minmax(min(420px, 100%), 1fr)); }
.tile { position:relative; aspect-ratio:16/9; background:#05070b;
        border:1px solid var(--line); border-radius:10px; overflow:hidden; }
/* The element is go2rtc's; the <video> inside it is plain DOM, not shadow,
   so both can be sized from here. */
.tile video-stream { display:block; width:100%; height:100%; }
.tile video { width:100%; height:100%; object-fit:cover; display:block; background:#05070b; }
.tile .label { position:absolute; left:8px; bottom:6px; z-index:2;
               padding:2px 7px; border-radius:6px; background:rgba(5,7,11,.66);
               font-size:.72rem; letter-spacing:.05em; }
/* Sits under the video, so it reads "connecting" until first frame paints and
   "no signal" if the stream never arrives -- same vocabulary as the wall. */
.tile .blank { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
               font-size:.65rem; letter-spacing:.1em; text-transform:uppercase; color:#3d4557; }
.tile:fullscreen { border:none; border-radius:0; }
.tile:fullscreen video { object-fit:contain; }
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <h1>Live</h1>
  <a class="navlink" href="/">Timeline</a>
  <a class="navlink" id="librarylink" href="/library" hidden>People &amp; plates</a>
  <span class="spacer"></span>
  <span class="stat">double-click a tile for fullscreen</span>
</header>

<main><div id="grid"></div></main>

<noscript><p style="padding:1rem">Live video needs JavaScript.</p></noscript>

<script type="module">
import "__GO2RTC_PATH__video-stream.js";

const channels = JSON.parse(document.getElementById("channels-payload").textContent);
const HAS_RECOGNITION = JSON.parse(document.getElementById("recognition-payload").textContent);
if (HAS_RECOGNITION) document.getElementById("librarylink").hidden = false;

const grid = document.getElementById("grid");
for (const id of channels) {
  const tile = document.createElement("div");
  tile.className = "tile";

  const blank = document.createElement("span");
  blank.className = "blank";
  blank.textContent = "connecting";

  const player = document.createElement("video-stream");
  // webrtc first for the sub-second path where the browser takes HEVC that
  // way (Safari); mse is the fallback that works everywhere else Apple.
  player.mode = "webrtc,mse";
  player.background = false;
  player.src = new URL("__GO2RTC_PATH__api/ws?src=ch" + encodeURIComponent(id), location.href);

  const label = document.createElement("span");
  label.className = "label";
  label.textContent = "Camera " + id;

  tile.append(blank, player, label);
  grid.appendChild(tile);
  // go2rtc builds the inner <video> on connect, unmuted; it only falls back to
  // muted when the browser blocks the autoplay. A browser that *allows* unmuted
  // autoplay (Safari per-site, Chrome with enough engagement) would therefore
  // start every camera's audio at once. Mute up front -- the native controls on
  // each tile unmute one on demand.
  player.video.muted = true;

  tile.addEventListener("dblclick", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else tile.requestFullscreen?.();
  });
}
</script>
</body>
</html>
"""


def render_live(channels: list[str], recognition_enabled: bool = False) -> bytes:
    """The live wall with the channel list embedded, one request like the rest."""

    def block(element_id: str, value: object) -> str:
        payload = json.dumps(value, separators=(",", ":")).replace("</", "<\\/")
        return f'<script type="application/json" id="{element_id}">{payload}</script>'

    page = LIVE_TEMPLATE.replace("__FAVICON__", FAVICON)
    page = page.replace("__GO2RTC_PATH__", GO2RTC_PATH)
    page = page.replace(
        '<script type="module">',
        block("channels-payload", list(channels))
        + "\n"
        + block("recognition-payload", recognition_enabled)
        + '\n<script type="module">',
    )
    return page.encode()
