# Second NVR: the Intelbras

**Status: probed and viable, not built.** A second NVR — an Intelbras at **192.168.50.170** on the
home LAN, from a different site than `nvr-zermatt` — was probed in depth on **2026-08-28**. Every
operation Timelapsed needs was verified working against the live device: stills, segment search,
segment download, remux to MP4, and RTSP. But it speaks **Dahua's CGI API, not ISAPI**, so
supporting it means a second protocol driver on top of multi-NVR plumbing the codebase does not
have yet. Both are mapped below.

All probing was done from the Mac (Wi-Fi, `192.168.50.181`). CT 303 reaches the device directly —
see [reachability](#reachability-from-the-guest) — so the wired figures can and should be
re-measured from there. Figures were measured on 2026-08-28; re-measure before trusting them.

## The device

| | |
| --- | --- |
| Model | Intelbras **MHDX 1308** (hybrid DVR, Dahua OEM) |
| Firmware | 4.002.00IB000.0.T, build 2024-04-17 |
| Serial | V2ZM4600729XP |
| Address | 192.168.50.170, wired; MAC `54:6c:ac:29:d2:85` |
| Cameras | **8 analog, channels 1–8** all answering; slots 9–10 are unused IP slots and answer 401 |
| Main stream | H.265 **960×1080** ("1080N", anamorphic) 30 fps, CBR 1024 kbit/s |
| Extra stream | H.264 352×240 |
| Snapshot stream | MJPG **704×480** (`SnapFormat`, a device setting — see below) |
| Recording | **Motion events only** — clips of roughly 15–40 s tagged `VideoMotion`. No continuous track. |
| Auth | HTTP Digest everywhere. Credentials are not in this repo or on any machine; ask Delisson. Failed attempts lock the account for minutes (Dahua behaviour), so never guess. |

The web UI at `http://192.168.50.170/` is Dahua's ExtJS interface with Intelbras branding.
`/ISAPI/*` paths answer 404 — nothing Hikvision-shaped is on this box.

## Reachability from the guest

Verified 2026-08-29 from CT 303: ARP resolves (`54:6c:ac:29:d2:85` learned as `REACHABLE`), ping
is 0% loss, and TCP port 80 answers. Nothing blocks driver work from running where it will
actually live.

One caveat stands: every figure in this document was measured over the Mac's Wi-Fi path, including
the download throughput number, which should be re-measured from the container before sizing
anything on it.

## The API, mapped and verified

Dahua CGI, HTTP Digest on every endpoint. Everything below was run against the live device and
worked.

### Stills

```
GET /cgi-bin/snapshot.cgi?channel=N          # N is 1-based; 1–8 answer, 9–10 → 401
```

Returns a JPEG immediately. **But at 704×480**, not recording resolution: `snapshot.cgi` serves
the device's *snapshot encode* (`Encode[ch].SnapFormat[0]`: MJPG 704×480 on every channel), which
is a setting, not a ceiling. Options before capture quality is judged:

* Raise `SnapFormat` resolution in the device UI (or via `configManager.cgi setConfig`) if the
  firmware allows 960×1080 there — unverified.
* Or pull stills from the RTSP main stream (`ffmpeg -i rtsp://… -frames:v 1`) at full recording
  resolution, at the cost of a stream setup per frame instead of one HTTP GET.

For reference, zermatt's ISAPI snapshots are 1920×1080 at ~231 KB; these are ~7–40 KB.

### Channel enumeration

```
GET /cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle   # lists all slots, live or not
GET /cgi-bin/configManager.cgi?action=getConfig&name=Encode         # per-channel stream settings
```

`ChannelTitle` lists ten slots regardless of what is connected ("Canal1"…"Canal10"); the honest
liveness test is `snapshot.cgi` answering 200 vs 401. There is no equivalent of ISAPI's
`/ISAPI/Streaming/channels` id list.

### Segment search

Session-object flow, four calls:

```
GET /cgi-bin/mediaFileFind.cgi?action=factory.create            # → result=<finder id>
GET /cgi-bin/mediaFileFind.cgi?action=findFile&object=<id>
      &condition.Channel=1                                      # 1-BASED here
      &condition.StartTime=2026-08-28%2000:00:00                # device-local time
      &condition.EndTime=2026-08-28%2023:59:59
GET /cgi-bin/mediaFileFind.cgi?action=findNextFile&object=<id>&count=32   # → found=N + items[…]
GET /cgi-bin/mediaFileFind.cgi?action=close&object=<id>
GET /cgi-bin/mediaFileFind.cgi?action=destroy&object=<id>
```

Each item carries `FilePath` (a real path on the device's disk, e.g.
`/mnt/dvr/2026-08-28/0/dav/00/0/1/98673/00.14.57-00.15.31[M][0@0][0].dav`), `StartTime`/`EndTime`,
`Events[0]=VideoMotion`, `VideoStream=Main`, and `Length`.

### Segment download

```
GET /cgi-bin/RPC_Loadfile{FilePath}     # FilePath verbatim from search, with [ ] @ URL-encoded
```

Streams the raw `.dav` file. Measured ~18 Mbit/s over the Mac's Wi-Fi path — treat that as a
floor and re-measure from the container, which reaches the device directly.

The container is **DHAV** (magic bytes `DHAV`), Dahua's proprietary wrapper. **ffmpeg has a native
`dhav` demuxer** and a plain `-c copy` remux to MP4 worked first try on a real downloaded segment,
duration intact. Same pipeline shape as the zermatt archiver, different demuxer doing the work.

### RTSP (live wall)

```
rtsp://user:pass@192.168.50.170:554/cam/realmonitor?channel=N&subtype=0   # main; subtype=1 extra
```

Probes fine over TCP: HEVC 960×1080. HEVC means the go2rtc/WebRTC live wall would be
Chrome-only for this device too, exactly like zermatt — no H.264 fallback on the main stream
(the extra stream *is* H.264, at postage-stamp resolution).

## Traps

Every one of these was hit live; the driver must handle them.

1. **Channel indexing is off by one between ask and answer.** `mediaFileFind` takes
   `condition.Channel` **1-based** (0 answers `Error / Bad Request!`), but the result items report
   `Channel` **0-based** — query channel 1, get `items[N].Channel=0` back. `snapshot.cgi` and RTSP
   are 1-based throughout.
2. **The clock is device-local stamped as GMT.** The device runs local time (−03) and its HTTP
   `Date:` header presents that as `GMT`. `global.cgi?action=getCurrentTime` returns local time
   with no offset. Same disease as zermatt's search API (see the NVR Roadmap appendix); translate
   at the edge, store UTC.
3. **`Length` lies upward.** It is cluster-aligned allocation, not file size — a segment listing
   `Length=7077888` downloaded 4,988,928 bytes. Fine for progress display, wrong for verification;
   verify by remux success, not byte count.
4. **Snapshot resolution is a config, not a capability.** 704×480 today because `SnapFormat` says
   so, while the same camera records 960×1080. Do not size capture-quality decisions off the
   default.
5. **Account lockout.** Dahua firmware locks the account after a handful of failed Digest
   attempts. A driver retrying on 401 must distinguish "challenge" from "wrong password" and never
   loop.

## What motion-only recording means for the pipeline

zermatt records continuously plus events; this device records **only motion events**. Consequences:

* A **footage lane** for it is honest by construction — the segments *are* the events — but sparse.
  Gaps mean "nothing moved", not "recording was down", and the UI copy should not imply otherwise.
* The **archiver's** replica for this device is small: dozens of short clips per day per channel,
  not 40 GB/day of continuous video. The existing oldest-first sequential replication model fits
  as-is.
* **Timelapse capture is unaffected** — stills come from `snapshot.cgi` live, not from recordings.
* On-device retention depth is unknown; measure it once a driver can sweep the whole history
  (mind whether this firmware has a search-session result cap like zermatt's 4,000-match silent
  truncation — unverified here).

## What the codebase needs

Two separable pieces of work, and this device forces both.

### 1. A protocol driver

Everything NVR-facing speaks ISAPI today: `nvr_capture_agent.py` (stills),
`nvr_footage.py` (search + download), and the archiver and analyzer that instantiate them. The
Dahua equivalents are fully mapped above; the clean shape is an interface the existing ISAPI
client already satisfies (snapshot / enumerate channels / search segments / download segment) with
a Dahua implementation beside it, chosen per NVR by config.

### 2. Multi-NVR plumbing

Mapped 2026-08-28 against the code; the single-NVR assumption is baked in end to end, with the
**channel number as the sole namespace key everywhere**:

* **Config**: one `[nvr]` section — one URL, one credential pair, one channel list
  (`config.py`, `schema.py`).
* **Storage**: `{root}/{channel}/image|keyframe|timelapse/` and the archive's
  `{root}/{channel}/{day}/` — no NVR dimension (`image_capture_library.py`, `archiver.py`).
* **Recognition index**: `watermark(channel PRIMARY KEY)`, `nvr_segment UNIQUE(channel,
  started_at)`, `nvr_sweep(channel PRIMARY KEY)` (`analysis/index.py`).
* **Web routes**: `/thumb/{channel}`, `/video/{channel}/…`, `/archive/{channel}/…` (`web.py`).
* **go2rtc**: stream names are `ch{channel}` (`live_page.py`, `deploy/go2rtc-setup.sh`).
* **Process model**: one shared `NVRCaptureAgent` for all capture workers; analyzer and archiver
  each build one `NVRFootageClient`.

Both NVRs have a channel 1, so every one of those collides the moment a second device exists.
The work is threading an NVR identity through config (`[nvr.zermatt]`-style sections), storage
paths, the index schema (composite keys, with a migration), routes (old URLs aliasing to the
default NVR so bookmarks survive), go2rtc stream names, and per-(nvr, channel) worker spawning.
Mostly mechanical parameter-threading, plus two real migrations (directory tree and sqlite), and
one real risk: any query that filters on channel alone silently mixes devices.

**The cheap alternative — a second instance** (own config, roots, ports, templated systemd units)
— needs almost none of that and stood as the fallback for a *different-site* NVR. This device is
on the same LAN and would presumably want the one UI, which is the whole reason the archive
exists; that argues for doing the plumbing properly.

### Resources

* The 1.4 TB archive volume was sized against zermatt's ~40 GB/day alone; this device adds little
  (motion clips only), so it fits, but recount after measuring its real daily volume.
* CT 303 runs 4 cores with a 6 GB memory cap. The cap is host-side config and cheap to raise —
  container memory is a cap, not a reservation — but eight more capture processes plus analyzer
  load for eight more channels is real; measure before enabling recognition on the new channels.

## Next steps, in order

1. Decide the still-quality route: raise `SnapFormat` vs frames-from-RTSP.
2. Driver interface + Dahua implementation, tested from the container.
3. Multi-NVR config/storage/index/route threading, with the migrations.
4. Measure this device's real retention depth and daily archive volume — including re-measuring
   download throughput wired — and recount the archive volume's headroom.
