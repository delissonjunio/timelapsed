"""The people-and-plates page.

Its own module, and its own page, because it answers a different question from
the timeline. The timeline asks "what happened in this window"; this asks "who
is in the library, and when did they appear". Those want different layouts, and
wedging the second into the viewer's sidebar made both worse.

The two pages meet at a link: a sighting here opens the viewer at
`/?channel=..&at=..`, which seeks the covering clip to that moment.

Standard library only, like web.py: one string, no build step, no framework.
"""

FAVICON = (
    "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjA"
    "gMCAzMiAzMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iNyIgZmlsbD0iIzEyMTUxZCIvPjxyZWN0IHdp"
    "ZHRoPSIzMiIgaGVpZ2h0PSIzMiIgcng9IjciIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzI0MmEzNyIvPjxyZWN0IHg9IjYiI"
    "Hk9IjgiIHdpZHRoPSIyMCIgaGVpZ2h0PSI0IiByeD0iMiIgZmlsbD0iIzM0ZDM5OSIvPjxyZWN0IHg9IjYiIHk9IjE0Ii"
    "B3aWR0aD0iMTMiIGhlaWdodD0iNCIgcng9IjIiIGZpbGw9IiNhNzhiZmEiLz48cmVjdCB4PSI2IiB5PSIyMCIgd2lkdGg"
    "9IjciIGhlaWdodD0iNCIgcng9IjIiIGZpbGw9IiM1YjlkZmYiLz48cmVjdCB4PSIyNyIgeT0iNiIgd2lkdGg9IjEiIGhl"
    "aWdodD0iMjAiIGZpbGw9IiNmZjZiNmIiLz48L3N2Zz4="
)

LIBRARY_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0b0d12">
<title>People &amp; plates &middot; Timelapsed</title>
<link rel="icon" href="__FAVICON__">
<style>
:root {
  color-scheme: dark;
  --bg:#0b0d12; --panel:#12151d; --panel-2:#171b25; --fg:#e8ebf1; --muted:#8b93a5;
  --line:#242a37; --accent:#5b9dff;
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
header .dot { width:7px; height:7px; border-radius:50%; background:var(--accent);
              box-shadow:0 0 8px var(--accent); }
header .spacer { flex:1; }
header .navlink { color:var(--muted); text-decoration:none; font-size:.75rem;
                  padding:.3rem .6rem; border-radius:6px; border:1px solid var(--line); }
header .navlink:hover { color:var(--fg); background:var(--panel-2); }
header .stat { color:var(--muted); font-size:.8rem; font-variant-numeric:tabular-nums; }

main { flex:1; overflow-y:auto; padding:1.1rem 1.25rem 2rem; }
.tabs { display:flex; gap:.35rem; margin-bottom:1.1rem; }
.tabs button { background:none; border:1px solid transparent; color:var(--muted); font:inherit;
               font-size:.8rem; cursor:pointer; padding:.35rem .8rem; border-radius:7px; }
.tabs button:hover { color:var(--fg); }
.tabs button[aria-pressed="true"] { background:var(--panel-2); border-color:var(--line);
                                    color:var(--fg); }

.grid { display:grid; gap:.75rem; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); }
.card { position:relative; background:var(--panel); border:1px solid var(--line);
        border-radius:10px; overflow:hidden; cursor:pointer; text-align:left;
        padding:0; color:inherit; font:inherit; display:block; }
.card:hover { border-color:var(--accent); }
.card img { width:100%; aspect-ratio:3/4; object-fit:cover; display:block; background:#000; }
.card .body { padding:.5rem .6rem .6rem; }
.card .who { font-size:.85rem; overflow-wrap:anywhere; }
.card .sub { font-size:.68rem; color:var(--muted); font-variant-numeric:tabular-nums;
             margin-top:.15rem; }
.card .noimg { aspect-ratio:3/4; display:flex; align-items:center; justify-content:center;
               background:var(--panel-2); color:var(--muted); font-size:.7rem; }
.card .rename { position:absolute; top:.4rem; right:.4rem; background:rgba(11,13,18,.82);
                border:1px solid var(--line); color:var(--fg); cursor:pointer; font-size:.65rem;
                padding:.2rem .45rem; border-radius:5px; opacity:0; transition:opacity .12s; }
.card:hover .rename, .card:focus-within .rename { opacity:1; }

/* Sightings: each is the still from that moment, and the whole card is the link
   into the video. The timestamp is the point, so it is never truncated. */
.sightings { display:grid; gap:.75rem; grid-template-columns:repeat(auto-fill, minmax(210px, 1fr)); }
.sighting { background:var(--panel); border:1px solid var(--line); border-radius:10px;
            overflow:hidden; cursor:pointer; display:block; color:inherit;
            font:inherit; text-align:left; text-decoration:none; }
.sighting:hover { border-color:var(--accent); }
.sighting img { width:100%; aspect-ratio:16/9; object-fit:cover; display:block; background:#000; }
.sighting .body { padding:.5rem .6rem .6rem; display:flex; align-items:baseline; gap:.5rem; }
.sighting .when { font-size:.8rem; font-variant-numeric:tabular-nums; }
.sighting .cam { font-size:.68rem; color:var(--muted); margin-left:auto; }

.detailbar { display:flex; align-items:center; gap:.7rem; margin-bottom:1rem; flex-wrap:wrap; }
.detailbar h2 { margin:0; font-size:1.05rem; }
.detailbar .count { color:var(--muted); font-size:.8rem; }
.btn { background:var(--panel-2); border:1px solid var(--line); color:var(--fg); font:inherit;
       font-size:.75rem; cursor:pointer; padding:.32rem .7rem; border-radius:6px; }
.btn:hover { border-color:var(--accent); }

.platerow { display:flex; align-items:center; gap:.8rem; padding:.5rem .6rem; width:100%;
            background:var(--panel); border:1px solid var(--line); border-radius:9px;
            cursor:pointer; margin-bottom:.5rem; color:inherit; font:inherit;
            text-align:left; text-decoration:none; }
.platerow:hover { border-color:var(--accent); }
.platerow img { width:86px; height:42px; object-fit:cover; border-radius:4px;
                background:#000; flex:none; }
.platerow .txt { font-family:ui-monospace,Menlo,monospace; font-size:1rem; letter-spacing:.07em; }
.platerow .meta { color:var(--muted); font-size:.72rem; margin-left:auto;
                  font-variant-numeric:tabular-nums; text-align:right; }

.empty { color:var(--muted); font-size:.85rem; padding:2rem 0; line-height:1.6; max-width:46rem; }
.note { color:var(--muted); font-size:.72rem; margin:1.2rem 0 0; line-height:1.5; max-width:46rem; }
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <h1>People &amp; plates</h1>
  <a class="navlink" href="/">&larr; Timeline</a>
  <span class="spacer"></span>
  <span class="stat" id="stat"></span>
</header>

<main>
  <div class="tabs">
    <button id="tab-people" aria-pressed="true">People</button>
    <button id="tab-plates" aria-pressed="false">Plates</button>
  </div>
  <div id="content"></div>
</main>

<noscript><p style="padding:1rem">This page needs JavaScript. The raw data is at
  <a href="/api/identities">/api/identities</a> and <a href="/api/plates">/api/plates</a>.</p></noscript>

<script>
const params = new URLSearchParams(location.search);
const state = {
  tab: params.get("tab") === "plates" ? "plates" : "people",
  identity: Number(params.get("identity")) || null,
  plate: params.get("plate") || null,
  identities: [],
  plates: [],
  sightings: [],
  loading: true,
};

const $ = id => document.getElementById(id);
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const pad = n => String(n).padStart(2, "0");
function fmt(ms) {
  const d = new Date(ms);
  return DAYS[d.getDay()] + " " + d.getDate() + " " + MONTHS[d.getMonth()]
    + ", " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
const clock = ms => { const d = new Date(ms); return pad(d.getHours()) + ":" + pad(d.getMinutes()); };

// A plate row covers a stay, not a moment: a car parked in the driveway is read
// again for as long as it sits there. Show the span when there is one.
function span(read) {
  const from = Date.parse(read.seen_at), to = Date.parse(read.last_seen_at || read.seen_at);
  return to - from >= 60000 ? fmt(from) + "\\u2013" + clock(to) : fmt(from);
}

// The point of this page: hand a moment back to the viewer, which seeks the
// covering clip to it rather than starting from the top of the newest one.
const viewerUrl = (channel, atMs, identity) =>
  "/?channel=" + encodeURIComponent(channel) + "&at=" + Math.round(atMs)
  + (identity ? "&identity=" + identity : "");

// The view is fully described by the query string, so a sighting list survives
// a reload and can be linked to.
function syncUrl() {
  const next = new URLSearchParams();
  if (state.tab === "plates") next.set("tab", "plates");
  if (state.identity) next.set("identity", String(state.identity));
  if (state.plate) next.set("plate", state.plate);
  const query = next.toString();
  history.replaceState(null, "", query ? "?" + query : location.pathname);
}

async function load() {
  try {
    const [identities, plates] = await Promise.all([
      fetch("/api/identities?kind=person").then(r => r.json()),
      fetch("/api/plates").then(r => r.json()),
    ]);
    state.identities = identities;
    state.plates = plates;
    if (state.identity) await loadSightings(state.identity);
  } catch (err) {
    console.warn("library fetch failed", err);
  }
  state.loading = false;
  draw();
}

async function loadSightings(identityId) {
  state.sightings = await fetch("/api/events?identity=" + identityId + "&limit=500")
    .then(r => r.json());
}

async function openIdentity(identityId) {
  state.identity = identityId;
  state.sightings = [];
  syncUrl();
  draw();
  await loadSightings(identityId);
  draw();
}

function draw() {
  $("tab-people").setAttribute("aria-pressed", String(state.tab === "people"));
  $("tab-plates").setAttribute("aria-pressed", String(state.tab === "plates"));
  const content = $("content");
  content.textContent = "";

  if (state.loading) {
    content.appendChild(el("p", "empty", "Loading\\u2026"));
    return;
  }
  if (state.tab === "plates") return state.plate ? drawPlateReads(content) : drawPlates(content);
  if (state.identity) return drawSightings(content);
  return drawPeople(content);
}

function drawPeople(content) {
  $("stat").textContent = state.identities.length
    + (state.identities.length === 1 ? " person" : " people");

  if (!state.identities.length) {
    content.appendChild(el("p", "empty",
      "Nobody has been grouped yet. People appear here once the analyzer has seen "
      + "someone large enough in frame to tell one sighting from another."));
    return;
  }

  const grid = el("div", "grid");
  for (const identity of state.identities) {
    const card = el("div", "card");
    if (identity.thumb) {
      const img = document.createElement("img");
      img.alt = ""; img.loading = "lazy"; img.src = identity.thumb;
      img.onerror = () => img.replaceWith(el("div", "noimg", "no crop"));
      card.appendChild(img);
    } else {
      card.appendChild(el("div", "noimg", "no crop"));
    }
    const body = el("div", "body");
    body.append(
      el("div", "who", identity.name || "Unnamed #" + identity.id),
      el("div", "sub", identity.sightings
        + (identity.sightings === 1 ? " sighting" : " sightings")
        + " \\u00b7 " + fmt(Date.parse(identity.last_seen))),
    );
    card.appendChild(body);

    const rename = el("button", "rename", identity.name ? "rename" : "name");
    rename.type = "button";
    rename.onclick = ev => { ev.stopPropagation(); renameIdentity(identity); };
    card.appendChild(rename);

    card.title = "Show every time this person appeared";
    card.onclick = () => openIdentity(identity.id);
    grid.appendChild(card);
  }
  content.appendChild(grid);
  content.appendChild(el("p", "note",
    "Grouped by clothing and build rather than by face \\u2014 faces on these cameras "
    + "are too small to identify. A group covers one day and one outfit."));
}

function drawSightings(content) {
  const identity = state.identities.find(i => i.id === state.identity);
  const name = identity ? (identity.name || "Unnamed #" + identity.id) : "#" + state.identity;

  const bar = el("div", "detailbar");
  const back = el("button", "btn", "\\u2190 All people");
  back.onclick = () => { state.identity = null; state.sightings = []; syncUrl(); draw(); };
  bar.append(back, el("h2", "", name),
             el("span", "count", state.sightings.length
               + (state.sightings.length === 1 ? " sighting" : " sightings")));
  if (identity) {
    const rename = el("button", "btn", identity.name ? "Rename" : "Give a name");
    rename.onclick = () => renameIdentity(identity);
    bar.appendChild(rename);
  }
  content.appendChild(bar);
  $("stat").textContent = name;

  if (!state.sightings.length) {
    content.appendChild(el("p", "empty", "Loading sightings\\u2026"));
    return;
  }

  const grid = el("div", "sightings");
  for (const sighting of state.sightings) {
    const at = Date.parse(sighting.starts);
    const card = document.createElement("a");
    card.className = "sighting";
    card.href = viewerUrl(sighting.channel, at, state.identity);
    card.title = "Open the video at this moment";
    if (sighting.thumb) {
      const img = document.createElement("img");
      img.alt = ""; img.loading = "lazy"; img.src = sighting.thumb;
      img.onerror = () => img.remove();
      card.appendChild(img);
    }
    const body = el("div", "body");
    body.append(el("span", "when", fmt(at)), el("span", "cam", "Camera " + sighting.channel));
    card.appendChild(body);
    grid.appendChild(card);
  }
  content.appendChild(grid);
}

// A read is one sighting; a plate is a car. The same car comes and goes all day,
// so listing raw reads buries the handful of plates that actually went past
// under a wall of rows repeating one of them. Group first, list on demand.
function platesByText() {
  const groups = new Map();
  for (const read of state.plates) {  // newest first, as the API returns them
    let group = groups.get(read.text);
    if (!group) {
      group = {text: read.text, reads: [], cameras: []};
      groups.set(read.text, group);
    }
    group.reads.push(read);
    if (!group.cameras.includes(read.channel)) group.cameras.push(read.channel);
  }
  return [...groups.values()];
}

const cameraLabel = channels =>
  (channels.length === 1 ? "Camera " : "Cameras ") + channels.join(", ");

// The row is an <a> when it goes to the video and a <button> when it opens a
// list, because those are different things and the keyboard should know it.
function plateRow(node, crop, text, meta) {
  node.className = "platerow";
  if (crop) {
    const img = document.createElement("img");
    img.alt = ""; img.loading = "lazy"; img.src = crop;
    img.onerror = () => img.remove();
    node.appendChild(img);
  }
  node.append(el("span", "txt", text), el("span", "meta", meta));
  return node;
}

function drawPlates(content) {
  const groups = platesByText();
  $("stat").textContent = groups.length + (groups.length === 1 ? " plate" : " plates");

  if (!groups.length) {
    content.appendChild(el("p", "empty",
      "No plates read yet. Plates are only read on the cameras listed in plate_channels, "
      + "and only where they land large enough in frame to resolve."));
    return;
  }

  for (const group of groups) {
    const latest = group.reads[0];
    const at = Date.parse(latest.seen_at);
    const crop = (group.reads.find(read => read.crop) || {}).crop;
    const meta = group.reads.length
      + (group.reads.length === 1 ? " sighting \\u00b7 " : " sightings \\u00b7 last ")
      + span(latest) + " \\u00b7 " + cameraLabel(group.cameras);

    if (group.reads.length === 1) {
      const row = plateRow(document.createElement("a"), crop, group.text, meta);
      row.href = viewerUrl(latest.channel, at);
      row.title = "Open the video at this moment";
      content.appendChild(row);
    } else {
      const row = plateRow(document.createElement("button"), crop, group.text, meta);
      row.type = "button";
      row.title = "Show every time this plate was read";
      row.onclick = () => { state.plate = group.text; syncUrl(); draw(); };
      content.appendChild(row);
    }
  }
  content.appendChild(el("p", "note",
    "One row per plate; open one to see each time it was seen. A sighting pools "
    + "every read of a plate that stayed in the same part of the frame, because "
    + "single frames disagree at this resolution and a car that sits still gives "
    + "plenty of them."));
}

function drawPlateReads(content) {
  const reads = state.plates.filter(read => read.text === state.plate);

  const bar = el("div", "detailbar");
  const back = el("button", "btn", "\\u2190 All plates");
  back.onclick = () => { state.plate = null; syncUrl(); draw(); };
  bar.append(back, el("h2", "", state.plate),
             el("span", "count", reads.length
               + (reads.length === 1 ? " sighting" : " sightings")));
  content.appendChild(bar);
  $("stat").textContent = state.plate;

  if (!reads.length) {
    content.appendChild(el("p", "empty", "No reads of this plate are in the index."));
    return;
  }

  for (const read of reads) {
    const at = Date.parse(read.seen_at);
    const row = plateRow(document.createElement("a"), read.crop, read.text,
      span(read) + " \\u00b7 Camera " + read.channel
      + " \\u00b7 " + read.votes + " of " + (read.reads || read.votes) + " reads agreed");
    row.href = viewerUrl(read.channel, at);
    row.title = "Open the video at this moment";
    content.appendChild(row);
  }
}

async function renameIdentity(identity) {
  const name = prompt("Name for this group", identity.name || "");
  if (name === null) return;
  try {
    const response = await fetch("/api/identities/" + identity.id, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: name.trim() || null}),
    });
    if (!response.ok) throw new Error(response.statusText);
    identity.name = name.trim() || null;
    draw();
  } catch (err) {
    console.warn("rename failed", err);
  }
}

for (const [id, tab] of [["tab-people", "people"], ["tab-plates", "plates"]]) {
  $(id).onclick = () => {
    state.tab = tab;
    if (tab === "plates") state.identity = null;
    else state.plate = null;
    syncUrl();
    draw();
  };
}

draw();
load();
</script>
</body>
</html>
""".replace("__FAVICON__", FAVICON)


def render_library() -> bytes:
    """The page is static; every list it shows is fetched from the API.

    Unlike the timeline, nothing is server-rendered into it: the library is
    unbounded and paged through by the user, so there is no small catalogue
    worth embedding the way the viewer embeds its clips.
    """
    return LIBRARY_TEMPLATE.encode()
