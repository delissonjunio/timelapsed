# Live

The viewer's `/live` page shows every camera at once, as real video, about a
second behind the world. It is the one page that does not read the library at
all: the tiles come straight off the NVR's RTSP main streams.

## How it works

Timelapsed itself never touches RTSP — the capture loop stays snapshots-only.
The streaming is done by [go2rtc](https://github.com/AlexxIT/go2rtc), a small
single-binary relay that runs next to the viewer:

```
NVR ──RTSP (H.265)──▶ go2rtc ──remux, no transcode──▶ WebRTC or MSE ──▶ browser
                        ▲
        one pull per camera, opened on demand,
        shared by every viewer, closed when the last one leaves
```

The video is **remuxed, not transcoded**: the H.265 bitstream the camera
encoded is repackaged for the browser byte-for-byte, so six simultaneous
1080p30 streams cost the guest a few percent of one core and the picture is
exactly what the NVR records. The page tries WebRTC first (sub-second latency)
and falls back to MSE; go2rtc's own `video-stream.js` element does the
negotiating.

The browser-side catch of skipping the transcode: the codec reaching the
browser is whatever the cameras encode, and here that is HEVC. Safari plays it
everywhere, Chrome and Edge play it wherever the machine has hardware HEVC
decode (every Apple device does), Firefox mostly does not. If a tile stays
black in Firefox, that is why.

## Setup

```bash
sudo bash deploy/go2rtc-setup.sh          # on an existing install
# or, from scratch:
sudo bash deploy/install.sh --with-go2rtc
```

The script installs the go2rtc binary, renders `/etc/go2rtc.yaml` (via
`timelapsed.go2rtc_config`) from every `[nvr]`/`[nvr.<name>]` section of
`/etc/timelapsed.ini` — same credentials, same channel lists, each device's
own RTSP dialect (ISAPI's `/Streaming/Channels/N01`, Dahua's
`/cam/realmonitor?channel=N`) — installs a hardened systemd unit, and starts
it. Stream names are `ch` plus the global channel id: `ch1` for the default
NVR, `chgarage-1` for a named one. Once the unit exists,
`deploy/update.sh` re-renders everything on every upgrade, so a channel added
to `timelapsed.ini` reaches the live wall on the next update.

nginx is required, not optional: go2rtc's API listens on localhost only, and
the `/go2rtc/` location in `deploy/nginx-timelapsed.conf` is what hands the
page its player script and the signalling websocket on the viewer's own
origin. `--with-go2rtc` therefore implies `--with-nginx`. As everywhere else
in the viewer there is no authentication — Tailscale is the front door.

Ports, all on the guest: `1984` go2rtc API (localhost only, proxied under
`/go2rtc/`), `8554` a local RTSP restream for debugging (localhost only),
`8555` WebRTC media (bound wide — the browser connects to it directly after
the proxied signalling).

## Bandwidth

A tile only costs bandwidth while someone is watching it: go2rtc opens the
camera pull when the first viewer arrives and closes it when the last leaves.
While the wall is open, expect a few Mbps per camera — the NVR's main-stream
bitrate, unchanged — between the NVR and the guest, and again between the
guest and each browser. Where the NVR sits behind a subnet router on another
site, that first leg rides the site's uplink; if the wall ever stutters on
remote viewing, the sub-streams (`ch<N>` → RTSP channel N02, a few hundred
kbps each) are the lever to pull.

## When a tile will not play

* **Stuck on "connecting", every camera** — go2rtc is down or nginx is not
  proxying it. `systemctl status go2rtc`, then `curl -s
  localhost:1984/api/streams` on the guest.
* **One camera dead** — check the pull itself, through the local restream:
  `ffprobe rtsp://127.0.0.1:8554/ch6`. If that fails, go2rtc cannot reach the
  NVR for that channel; `journalctl -u go2rtc` says why.
* **Black tile in Firefox** — HEVC, see above. Use Safari or Chrome.
* **Plays for exactly a minute, then dies** — a proxy in front is timing the
  websocket out. The shipped nginx location sets `proxy_read_timeout` long on
  `/go2rtc/` for exactly this reason; check whatever else is in the path.
