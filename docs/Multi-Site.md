# Multi-Site

Two sites, each with its own recorder and its own Timelapsed, and one page that shows both. The
recorder half of this is the [Frigate Plan](Frigate-Plan.md); this is the viewer half.

Planned, not built.

## The rule that decides the design

Once footage lives at the site that recorded it, the only question left is which bytes cross the
WAN. There is exactly one answer worth having:

> **Metadata is fetched by the portal. Media is fetched by the browser, from the site that holds
> it.**

Proxying video through the portal would send every frame across the WAN twice — once from zermatt
to pve1, once from pve1 to whoever is watching — which is the traffic the whole plan exists to
remove. Fetching metadata in the browser instead would mean CORS on every JSON endpoint, two
origins in one page, and a broken page when one site is down.

So the portal renders the page and answers every `/api/*` call, merging its own data with what it
pulls from its siblings. Lane rows, clip catalogues, sightings, status: all small, all
server-side, all on one origin. The URLs inside those payloads are **absolute** and point at the
site that owns the bytes.

## Topology

```
browser (on the tailnet)
   │
   │  page + all /api/*            ┌──── https://timelapsed.tail14e39f.ts.net   (pve1, portal)
   ├───────────────────────────────┘        merges its own data with pve2's
   │
   │  /vod/, /clip.mp4, /video/, /archive/, go2rtc WebRTC
   ├───────────────────────────────────────▶ pve1's nginx  ──▶ frigate-home
   └───────────────────────────────────────▶ pve2's nginx  ──▶ frigate-zermatt
                                             https://timelapsed-zermatt.tail14e39f.ts.net
```

Every node runs the same code. The portal is not a different program; it is the same
`timelapsed-web` with a `[sites]` section filled in. pve2's is empty, so pve2 serves a complete
single-site viewer for anyone at that site — which is also what makes it debuggable when the link
is down.

## Configuration

```ini
[sites]
# Only on the portal. Name = base URL, reachable from this host and from a browser
# on the tailnet. The local site is implicit and always present.
zermatt = https://timelapsed-zermatt.tail14e39f.ts.net
```

Names follow the `[nvr.<name>]` character class — lowercase, digits, `-`, `_` — because they reach
URLs and DOM attributes.

Use Tailscale Serve names rather than raw `100.x` addresses. The address is what a browser has to
resolve and what an nginx CORS allowlist has to match, and a stable hostname with a real
certificate is worth more than one fewer moving part. It also keeps the portal's own origin
`https://`, which avoids mixed-content blocking when it hands the page a remote media URL.

## Channel ids across sites

The channel id is the one namespace everything shares, and it is currently flat: the unnamed
`[nvr]`'s channels keep bare numbers, a named section's become `<name>-<number>`. That was enough
for two recorders on one host.

Two hosts can collide, and today's split happens not to. Zermatt's channels are the bare ids
`1,5,6,7,8,9,10` and they must stay bare, because they are directory names holding months of
stills. Home's are `intelbras-1..5`. So the migration needs no renaming — but a third site with a
bare channel `1` would land on top of zermatt's.

So the portal namespaces remote channels **for its own payloads only**: `zermatt/6`, never on
disk, never at the site itself. A site's own ids are untouched, which is what keeps
`{root}/6/image/` meaning what it has always meant. The viewer's `forChannel()` filter at
`index.html:339` already treats the id as an opaque string, and `state.channel` is compared, not
parsed, so a `/` in it is a display and URL-encoding concern rather than a logic one.

## What crosses the WAN, and what does not

| what | how | volume |
| --- | --- | --- |
| lane rows, sightings, clip catalogue, status | portal fetches remote `/api/*` | KB per page view |
| a timelapse clip | browser to the owning site | the clip, once, cached |
| archived footage, scrubbing | browser to the owning site's Frigate | only what is watched |
| live wall tiles | browser to the owning site's go2rtc | only while watched |
| recordings | never | zero |

That last row is the whole point. A day nobody watches zermatt costs a few hundred kilobytes of
lane rows instead of tens of gigabytes of segments.

## CORS, and exactly where it is needed

Because metadata is server-side, no JSON endpoint needs CORS. Media mostly does not either: a
`<video src="https://other-site/…/clip.mp4">` is a plain media load, and go2rtc's WebRTC
negotiation is its own websocket to its own origin.

HLS is the exception. hls.js fetches the manifest and every segment with `fetch()`, so those are
cross-origin XHRs and the owning site must allow the portal's origin:

```nginx
# On each non-portal site, on /vod/ and the Frigate API locations only.
add_header Access-Control-Allow-Origin "https://timelapsed.tail14e39f.ts.net" always;
add_header Vary Origin always;
```

An allowlist of sibling origins, not `*` — there is no authentication anywhere in this system, so
the only thing standing between the footage and the internet is that nothing routes to it. A
wildcard header does not open a route, but it removes the one line of documentation that says who
is supposed to be reading this.

## Failure modes are the feature

A remote site will be unreachable, regularly. The uplink drops, pve2 reboots, Tailscale
reconnects. None of that may produce a 500 or an empty page.

* **Short timeouts, and a cache that outlives them.** Two seconds connect, five read, and the last
  good payload per remote endpoint kept and served stale with its age attached. A page that draws
  yesterday's lane marked stale is useful; a spinner is not.
* **Partial payloads are normal.** `/api/footage` merges what it has and reports which sites
  answered. The lane draws a distinct "not reachable" fill for a site that did not — visibly
  different from "the recorder held nothing here", which is the existing empty stretch and means
  something completely different.
* **The portal never blocks on a sibling.** Remote fetches happen concurrently with local work and
  in parallel with each other, and the slowest sibling bounds the response, not the sum.
* **Media failures are the browser's to report.** A tile or a scrub that cannot reach pve2 fails in
  the element, so the viewer needs the same "why is nothing playing" message the footage lane
  already produces for an unarchived stretch.

## Clocks

Every payload is UTC and every lane coordinate is wall-clock time, so two sites merge only as well
as their clocks agree. `chrony` on both nodes, and the status page should show each site's offset
from the portal's clock — a silently drifting remote site produces sightings that land next to the
wrong footage, which looks like a bug in the lane rather than a bug in the clock.

Frigate stores recordings in UTC paths and takes timestamps as epoch seconds, so nothing in the
media layer has an opinion about timezones. The `[timelapse] timezone` that anchors keyframes to
local noon stays per-site, because the sites are in different places and noon is a fact about the
sun, not about the portal.

## Status

`/status` becomes per-site with the portal's own first. The headline row is the worst of all sites,
because "everything is fine except the site you cannot see" is not fine. A site that has not
answered for longer than its cache TTL is itself a check that fails.

Each site keeps reporting to New Relic under its own hostname; the four APM app names stay as they
are, so the existing dashboard keeps working and the site becomes a facet rather than a new app.
The per-channel capture staleness condition in
[Operations](Operations.md#monitoring) is what catches a single site going quiet, and it needs to
be faceted by channel to do it — the current "no capture cycles for 10 minutes" condition counts
across all channels, which is exactly why the 2026-09-08 Intelbras wedge never fired an alert.

## What this does not do

* **No cluster, no shared storage, no replication between sites.** Each site owns its footage and
  its own offsite copy. The portal is a reader.
* **No cross-site identity matching.** The re-ID index is per-site, and it stays that way; the
  cameras at the two sites see different people in different countries.
* **No authentication.** Unchanged, and unchanged on purpose. Tailscale is the front door at both
  sites, and neither site's nginx is reachable from anywhere else.
* **No writes to a remote site.** The one write endpoint is identity rename, and it renames an
  identity in the local index. Renaming a remote site's identity means going to that site's own
  page.
