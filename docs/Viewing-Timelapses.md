# Viewing Timelapses

## The built-in viewer

Timelapsed ships a small web viewer with no dependencies beyond the Python standard library. It
runs as its own service:

```bash
sudo systemctl enable --now timelapsed-web
```

It serves:

| Path | What it does |
| --- | --- |
| `/` | An NVR-style timeline: one lane per cadence, a camera wall down the side, and a player |
| `/?channel=1` | Filter to one channel |
| `/?cadence=weekly` | Filter to one cadence |
| `/?channel=1&cadence=daily` | Both |
| `/?channel=5&at=1787773521000` | Open on a moment: centres the timeline there and seeks the covering clip to it. This is what the library page links to. |
| `/api/timelapses` | The same list as JSON |
| `/video/{channel}/{filename}` | The video file, with HTTP Range support |
| `/thumb/{channel}.jpg` | The latest still for a camera, downscaled for the sidebar |
| `/library` | People and plates, when recognition is enabled |
| `/crop/{event\|plate}/{id}.jpg` | A recognition crop |
| `/healthz` | Returns `ok`, for monitoring |

Lanes are drawn widest-first, so with everything enabled the order top to bottom is `progress`,
`monthly`, `weekly`, `daily`, `hourly`. Each has a colour swatch that doubles as a filter chip. The
lane list comes from the cadence registry rather than being written into the page, so a cadence added
in `schema.py` gets a lane without the viewer changing.

Two things about the `progress` lane specifically: its clip spans the entire project, so it fills the
width at every zoom level, and the arrow keys — which step within a cadence — have nothing to step
to. Click it in the lane to play it. The range presets go up to `1y` and `All`, which is what you
want for a build that has been running for months.

The page is mobile-first and works in Safari on iOS, which is fussier than most browsers: it needs
`Accept-Ranges`, correct `Content-Type`, and `playsinline`. All three are handled.

## The player

There is one `<video>` on the page and it is built once, at load. Selecting a clip swaps its `.src`;
nothing is torn down. That matters because the timeline hands the player *moments*, not files: a
click on a sighting inside the clip already on screen has to move the picture without restarting it.

A timelapse is a linear compression of its window, so wall-clock time and position in the video are
the same coordinate at two scales. Everything the player and the timeline agree about goes through
that one conversion, in both directions:

| You do this | It means |
| --- | --- |
| Click a clip | Play from the moment you clicked, not from the clip's first frame |
| Click a sighting | Play from that sighting, in the shortest clip that covers it |
| Drag the selected clip | Scrub it. Dragging anywhere else still pans the timeline |
| Double-tap the left or right of the picture | Jump ten seconds back or forward, same as the `↺ 10` / `10 ↻` buttons |
| Watch it play | A playhead tracks across every lane, and sightings light up as it passes through them |

The transport bar under the picture states **what time is on screen**. Nothing in the picture says
so, and on a weekly clip a second of video is nearly three hours of the world. It also carries
play/pause, ten-second skips, frame stepping, 0.5×–4× (half speed is the only way to watch a
weekly), and full screen. Space plays and pauses, `,` and `.` step a frame, the arrow keys move a
clip at a time.

The ten-second skip is ten seconds **of the video**, not of the world — on a weekly render that is
most of a day. The reason to reach for it is "I missed something, back it up", and that distance is
measured in what you were just watching; the clock in the bar says what it came to in world time.

Double-tapping the outer third of the picture does the same jump, left for back and right for
forward, the way a phone video player does. A single tap in the middle plays and pauses. That means
a tap on the picture has to wait out the double-tap window before it commits to a toggle, so it
feels a beat slower than the button — the button has no such delay, and is always the instant way to
pause.

Frame stepping needs the rate the clip was rendered at, and an MP4 carries no frame rate a media
element will report — so the server sends it, per cadence, in the page.

When an hourly clip ends the player rolls into the next hour rather than looping, and it warms the
next clip's header before the seam so the handover does not stall. A clip with nothing after it
loops, as before.

**A caveat on the time mapping.** It is linear because frames are sampled at even intervals across
the window — but they are sampled across the stills that *exist*, so a gap in capture compresses
unevenly and the mapping drifts across it. The playhead makes that visible rather than introducing
it; the seek has always worked this way. Fixing it properly needs a per-clip frame-time sidecar
written at render.

Clips use `preload="metadata"`, not `auto`. They are dense — 1800 frames compressed into 60 seconds
runs to tens of megabytes — and `auto` starts pulling from byte zero the moment a clip is selected.
That download is wasted whenever the viewer is opened on a specific moment (`?at=`), because the
seek cannot be applied until the header arrives and playback then restarts elsewhere. Fetching
metadata alone gets the header quickly — it is at the front, thanks to `-movflags +faststart` — so
the seek lands first and buffering begins at the moment actually being watched.

Autoplay is normally allowed for a muted video, but not on every browser and not after every
navigation. When it is refused the player says so and offers a tap, rather than leaving a picture
sitting paused — which is indistinguishable from a click that did nothing.

## Putting nginx in front

Optional, and worth it if you watch the same footage more than once or the guest is tight on RAM:

```bash
sudo bash deploy/install.sh --with-nginx
```

nginx takes port `8080` — the one Tailscale Serve and the firewall rules already point at — and the
viewer moves to `127.0.0.1:8081`. Requests split there:

| Path | Served by | Why |
| --- | --- | --- |
| `/video/{channel}/{file}.mp4` | nginx, straight off the disk | Bytes, and nothing else |
| everything else | the Python viewer, proxied | The page, the JSON APIs, ffmpeg-scaled thumbnails and crops |

Nothing about the URLs changes, so bookmarks, Tailscale Serve and `/healthz` all keep working. The
flag is sticky: once `/etc/nginx/sites-available/timelapsed` exists, `install.sh` and `update.sh`
keep it configured and re-render it from `deploy/nginx-timelapsed.conf` on each upgrade.

### What it actually buys

In the order the difference is noticeable:

* **Conditional requests.** This is the big one. The Python viewer sends no `ETag`, no
  `Last-Modified` and no `Cache-Control`, so re-opening yesterday's daily downloads all ~140 MB of
  it again. nginx answers the second visit with a `304`, and a render is immutable once written so
  the cache is never wrong.
* **Page cache.** Python reads a video into userspace 256 KB at a time, pulling the whole file
  through the guest's page cache and evicting the stills the next ffmpeg wants. The nginx location
  uses `directio` above 16 MB, so a 140 MB render is read without disturbing the cache at all. On a
  2 GB guest running renders at `max_concurrent_renders = 1`, that is the difference that matters.
* **Threads.** `ThreadingHTTPServer` spawns one OS thread per connection. A browser scrubbing a
  timeline opens several while every camera tile is polling a thumbnail; nginx serves the lot from
  one worker with no per-connection memory.
* **gzip** on the page and the JSON APIs, which the viewer does not do.

**What it does not buy is a faster single stream.** Over Tailscale the WireGuard tunnel is the
ceiling long before Python's copy loop is, and `sendfile` cannot make a tunnel wider. If your
complaint is that one video takes a while to start over a slow link, this will not fix it — shorten
`duration_seconds` or drop `resolution` instead.

### Checking it works

```bash
curl -sI localhost:8080/healthz                            # still ok, via the proxy
curl -sI localhost:8080/video/1/weekly_….mp4 | grep -i etag  # nginx sets one, Python does not
sudo tail -f /var/log/nginx/timelapsed.access.log
```

An `ETag` on a video response means nginx served it. If videos come back without one, nginx fell
back to the viewer — almost always because it cannot read the library:

```bash
sudo -u www-data test -r /var/lib/timelapsed/1/timelapse/*.mp4 || sudo chmod -R g+rX /var/lib/timelapsed
```

`nginx-setup.sh` checks this at install time and warns, but it will not chmod a library with a
hundred thousand stills in it on your behalf.

### Going back

```bash
sudo rm /etc/nginx/sites-enabled/timelapsed /etc/nginx/sites-available/timelapsed
sudo systemctl reload nginx
sudoedit /etc/timelapsed.ini      # [web] back to host = 0.0.0.0, port = 8080
sudo systemctl restart timelapsed-web
```

Removing the file in `sites-available` is what makes it non-sticky; leave it and the next
`install.sh` will put it back.

## Publishing it with Tailscale Serve

This is the recommended setup. It gives you a real HTTPS certificate, a stable hostname, and no
port forwarding:

```bash
sudo tailscale serve --bg 8080
```

The viewer is then at `https://timelapsed.<your-tailnet>.ts.net` from any device signed into your
tailnet. Add it to your phone's home screen and it behaves like an app.

```bash
sudo tailscale serve status   # show what is published
sudo tailscale serve --https=443 off   # stop publishing
```

### Do not use Funnel

`tailscale funnel` publishes to the public internet. **The viewer has no authentication** — it is
built on the assumption that the network is the access control. Funnel would make your camera
footage available to anyone who guesses or is given the URL.

If you genuinely need public access, put an authenticating reverse proxy in front of it
(Caddy with `basic_auth`, or an OAuth proxy) rather than exposing the viewer directly.

## Why not YouTube

A reasonable question, since it is free and plays anywhere. It does not work for this:

* **API quota.** The YouTube Data API allocates 10,000 units a day by default and an upload costs
  1,600 units — **six uploads a day**. Three channels producing hourly, daily and weekly videos is
  about 75 uploads a day, roughly twelve times over the limit. Quota increases require a written
  audit that is rarely granted for automated surveillance uploads.
* **It is not a storage service,** and bulk automated uploads of repetitive footage is precisely
  the pattern that draws strikes. "Unlisted" is not private; anyone with the link can watch.
* **Re-encoding and processing delay.** Your h264 is transcoded again, and there is a wait before
  each video is playable.

Cloud object storage (Cloudflare R2's free tier, Backblaze B2) is a reasonable *backup* target, but
it does not solve "watch it easily" — you still need something to browse and play it. Treat that as
a separate problem from viewing.

## Jellyfin, if you want apps

If you want native iOS / Apple TV / Android apps, resume-where-you-left-off, and thumbnails, run
Jellyfin on the same guest pointed at the timelapse directories. It costs about 1 GB of RAM.

```bash
curl -fsSL https://repo.jellyfin.org/install-debuntu.sh | sudo bash
```

Then in the setup wizard add a library:

* **Content type:** Home videos and photos
* **Folder:** `/var/lib/timelapsed`

Give Jellyfin read access to the library:

```bash
sudo usermod -aG timelapsed jellyfin
sudo chmod -R g+rX /var/lib/timelapsed
sudo systemctl restart jellyfin
```

Publish it over Tailscale the same way:

```bash
sudo tailscale serve --bg 8096
```

Jellyfin has real user accounts, so it is a better fit if you want to share footage with someone
who should not have access to your whole tailnet.

Note that Jellyfin will try to organise the videos as media, and the filenames
(`weekly_20250601_120000_UTC-20250608_120000_UTC.mp4`) are not what its metadata scrapers expect.
Disable metadata downloading for that library to keep it from inventing movie matches.

## Direct file access

Nothing about the layout is proprietary. The videos are ordinary MP4 files:

```bash
scp delisson@timelapsed:/var/lib/timelapsed/1/timelapse/weekly_*.mp4 .
```

Or mount the directory over SSHFS, or export it read-only over Samba if you want it to show up in
Finder. The web viewer is a convenience, not a gatekeeper.
