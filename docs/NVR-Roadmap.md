# NVR Roadmap

**Status: stages 1–3 built; stage 4 is next.** The decision landed 2026-08-28: build the
**full segment replica** (see [Archiving everything, revisited](#archiving-everything-revisited)),
for one UI, an offsite copy, and longer retention than the device keeps. The storage for it exists
— the guest has a dedicated **1.4 TB ext4 volume mounted at `/var/lib/timelapsed/archive`**, owned
by the `timelapsed` user (thin volume `scsi1` from the `hdd-thin` LVM pool on the Proxmox host,
growable). Stage 1 landed 2026-08-28: `timelapsed/nvr_footage.py` sweeps `ContentMgmt/search`
into an `nvr_segment` table from inside the analyzer daemon, verified against the live device.
Stage 2 landed the same day: a footage lane on the timeline, served by `/api/footage`. So did
stage 3 in its replica form: the `timelapsed-archiver` daemon replicates every recorded segment
into the archive volume. A build session should continue at [Stages](#stages) with stage 4 —
playing the replica from the viewer.

This page records what the NVR can actually do, measured against the live device, and what it
would take to use it. It exists so the decision does not have to be re-derived later.

Device figures were measured from VM 302 against `nvr-zermatt` (192.168.18.89) on **2026-08-27**;
the bulk-replication figures were added the same way on **2026-08-28**.
Re-measure before trusting them; scenes, firmware and detection settings drift.

## Where this starts

Timelapsed already answers *what was there*. [Recognition](Recognition.md) finds people and
vehicles in the stills the capture daemon writes, groups them into events, reads plates, and keeps
it all in `index/index.sqlite3`. The viewer draws activity lanes and has pages for identities and
plates.

What it cannot answer is **what actually happened**. Recognition sees a person on ch5 at 14:32
because a still landed in that 10-second slot. The moment itself — the walk up to the gate, the
plate as the car turns — exists only on the NVR, at 30 fps, and Timelapsed never touches it.

**The gap this roadmap closes is video.** Everything else is already built.

## Why not simply capture stills faster

Because stills are the wrong representation for motion, by a factor measured on this hardware.
From a real downloaded segment (ch6, 50.0s, 1,501 frames, H.265 1080p):

| | bytes per frame |
| --- | --- |
| Frame inside the NVR's own recording | **9.6 KB** |
| Same frame as an ISAPI JPEG snapshot | **231 KB** |

**24× more expensive per frame**, against the NVR's own 2048 kbit/s encode rather than some
idealised codec. JPEG has no inter-frame compression and these scenes are near-static, which is
exactly where video codecs win by an order of magnitude.

Three further reasons a sub-2-second interval is wasted effort:

1. **The renderer discards the frames.** `image_processor.py` samples every window down to
   `output_fps × duration_seconds` = 1,800 frames, and [Storage Planning](Storage-Planning.md)
   shows a 2-second interval already fills a full 60-second hourly clip.
2. **The transport is per-frame.** `nvr_capture_agent.py` calls `requests.get()` with a fresh
   `HTTPDigestAuth` each time — no `Session` — so every frame costs a new TCP connection plus a
   full digest challenge, two round trips. Free at 10s; at 1s across six channels it is 12
   requests/second, each forcing a decode, scale and JPEG encode on NVR CPU.
3. **It still would not be motion.** Polling tops out near 1 fps. The gate walk needs 30.

**Keep `interval_seconds = 10`. Never go below 2.** Faster capture is not the route to video.

## What this NVR actually is

A DS-7616NXI-K1, firmware V4.76.015, one 953 GB SATA disk in `quota` work mode, `freeSpace 0` and
wrapping. Quota mode means **per-channel** retention, so a quiet camera's footage outlives a busy
one's.

### Channels

| ch | name | camera | model | NVR recording |
| --- | --- | --- | --- | --- |
| 1 | OFICINA INTERNO | 192.168.18.2 | DS-2CD1023G2-LIU | motion detection **off** |
| 5 | PORTAO SOCIAL SUPERIOR | 192.168.18.6 | DS-2CD1327G2H-LIU | on |
| 6 | GERAL LOTE | 192.168.18.9 | DS-2CD1327G2H-LIU | on |
| 7 | FUNDOS OFICINA | 192.168.18.10 | DS-2CD1327G2H-LIU | on |
| 8 | RUA OFICINA | 192.168.18.11 | DS-2CD1327G2H-LIU | on |
| 9 | FRENTE OFICINA | 192.168.18.12 | DS-2CD1327G2H-LIU | on |

Channels 2 (`Camera 01`, .18.5) and 3 (`LATERAL FRENTE`, .18.3) are configured but `netUnreachable`.

Channel 1 is the only interior camera and the only non-ColorVu sensor. Its motion detection being
off is most likely deliberate — an indoor workshop camera triggers continuously while anyone works.
It streams fine and Timelapsed captures its stills normally, averaging 115 KB against ~290 KB
outdoors.

### It records on events, not continuously

`ActionRecordingMode = AllEvent`; every segment is tagged `recordType.meta.hikvision.com/allEvent`.
Duty cycle over 48 hours:

| ch | segments | covered | duty | mean segment |
| --- | --- | --- | --- | --- |
| 1 | 1 | 0.0h | 0.0% | 46s |
| 5 | 702 | 29.6h | 61.7% | 152s |
| 6 | 794 | 31.2h | 65.0% | 141s |
| 7 | 223 | 4.9h | 10.2% | 79s |
| 8 | 823 | 24.6h | 51.2% | 108s |
| 9 | 437 | 9.8h | 20.4% | 81s |

**≈ 50 video-hours per day.** Continuous scrub playback cannot be delivered by pulling from this
device — that footage does not exist. Event playback can, which is what matters here.

Those duty figures are high because detection is configured loosely: `PostRecordTimeSeconds = 30`
on every channel (so re-triggers chain segments into the observed 141–152s blobs), no `gridMap`
region mask, and `sensitivityLevel = 60`. Worth tuning on its own merits — it would also roughly
quadruple the NVR's own retention window — but the plan below does not depend on it.

### Streams

All six channels H.265. Main `{ch}01` is 1920×1080 at a 2048 kbit/s VBR cap, GOP 60, 30 fps.
A sub-stream `{ch}02` exists at 640×360 / 512 kbit/s, but **only `{ch}01` tracks are recorded**, so
any archive is main-stream only.

## Transport: download, never RTSP

RTSP playback (`/Streaming/tracks/{ch}01/?starttime=…&endtime=…`) resolves and describes correctly
but reports `duration=-103656199`. Hikvision never advertises an end, so `ffmpeg -c copy` **hung for
300 seconds and produced nothing, with no error.** Do not build on it.

`POST /ISAPI/ContentMgmt/download` is strictly better. Measured over five consecutive segments:

```
seg0   50s video   14.8 MB  0.87s  136.5 Mbit/s   57.5x realtime
seg1   65s video   18.1 MB  0.89s  162.0 Mbit/s   72.7x realtime
seg2   65s video   19.4 MB  0.93s  166.8 Mbit/s   70.0x realtime
seg3   97s video   22.3 MB  1.07s  166.8 Mbit/s   90.6x realtime
seg4   49s video   11.2 MB  0.88s  101.5 Mbit/s   55.7x realtime
TOTAL 326s video   85.8 MB  4.6s   147.8 Mbit/s     70x realtime
```

**147.8 Mbit/s, 70× realtime**, across the Tailscale link to the remote site. Bandwidth is not the
constraint; an hour of footage lands in about 51 seconds. It also keeps credentials inside the HTTP
digest exchange rather than on an `ffmpeg` command line where `ps` would expose them.

## The idea: let recognition choose the footage

Archiving everything the NVR records is 948 MB per video-hour × 50 video-hours/day ≈ **47 GB/day**.
Against the ~95 GB free once the still library reaches its ~96 GB steady state, that is two days.
Not an archive.

But Timelapsed already knows which moments matter. Recognition produces about **45 events a day**
across six channels ([Recognition Feasibility](Recognition-Feasibility.md)), and an event is a
person or a vehicle above a 0.5 score — not foliage, not headlights, not a neighbouring building.

Fetching a bounded clip per recognition event instead of archiving every motion segment:

| clip length | GB/day | 30 days | 90 days | 365 days |
| --- | --- | --- | --- | --- |
| 60s (20s pre / 40s post) | 0.71 | 21 GB | 64 GB | 260 GB |
| 90s (30s pre / 60s post) | 1.07 | 32 GB | 96 GB | 390 GB |

**60-second clips at 90-day retention is 64 GB** — it fits the disk that exists, and it is roughly
a **50× reduction** against archiving all motion footage, aimed at the events that were worth
keeping in the first place.

Clip length is bounded deliberately rather than following event duration. A parked car is one
recognition event lasting hours; the interesting part is the arrival.

### The two event sources are complementary

Recognition runs on 10-second stills, so something crossing frame in under ten seconds may land on
one still or none. The NVR saw it at 30 fps regardless. So:

* **NVR extents** answer *is there footage for this moment* — needed before any fetch.
* **Recognition events** answer *was this moment worth keeping*.

On channels where recognition sees almost nothing (ch7 and ch9 recorded essentially no activity in
25 hours) NVR motion events are the only signal available, and are worth falling back to.

## Archiving everything, revisited

The rejection of a full archive above is a disk argument, not a transport one — ~47 GB/day against
a guest disk that cannot hold a week of it. With buying a disk on the table, the bulk path was
measured properly on **2026-08-28**: same live device, sequential `ContentMgmt/download` driven
from VM 302.

### Replication throughput holds at scale

The 70× figure above came from five consecutive segments. A full wall-clock hour of ch5
(2026-08-27 12:00–13:00Z, 16 segments) downloaded back to back:

```
16 segments   757.5 MB   47.5s   127.6 Mbit/s sustained
```

No ramp-down over minutes and no per-request penalty worth designing around. Every fetched segment
probed clean (`hevc`, sane durations) and remuxed to MP4 with `-c copy` as before.

**Parallel downloads buy nothing.** Two simultaneous fetches totalled 125.8 Mbit/s — the same
ceiling. The ~128 Mbit/s is the path, not the request, so a replica should fetch **sequentially**:
simpler, and exactly as fast.

At this rate a day of footage lands in ~42 minutes of transfer, and everything the NVR currently
holds (~950 GB) in roughly 17 hours.

### What a full day weighs

Census of 2026-08-27 (UTC), search paging only, sizes from the `size=` field the device reports:

| ch | segments | bytes |
| --- | --- | --- |
| 1 | 0 | 0 |
| 5 | 429 | 11.4 GB |
| 6 | 328 | 13.2 GB |
| 7 | 98 | 2.0 GB |
| 8 | 422 | 9.8 GB |
| 9 | 263 | 3.4 GB |
| **total** | **1,540** | **39.7 GB** |

Consistent with the 48-hour duty estimate above. ch1 contributed nothing — its motion detection is
off, so there was nothing to record and nothing to pull. **A replica inherits every blind spot of
the NVR's own recording**; coverage for ch1 means changing the NVR's recording config, not anything
on this side.

### How deep the device holds

Quota mode is per-channel, so retention is wildly uneven. Earliest recorded segment per channel,
from stage 1's first full sweep on 2026-08-28 (the 2026-08-27 measurements understated ch1 and
ch6 — they were taken before the 4,000-result search cap in the appendix was understood):

| ch | earliest segment | depth |
| --- | --- | --- |
| 1 | 2026-02-04 | 205 days |
| 5 | 2026-08-17 | 11 days |
| 6 | 2026-06-17 | 72 days |
| 7 | 2026-07-12 | 47 days |
| 8 | 2026-08-14 | 14 days |
| 9 | 2026-07-13 | 46 days |

Two consequences:

* **The edge is slow.** The tightest channel (ch5) keeps ~11 days, so a replica that syncs once a
  day is never close to losing footage; an hourly poll is generous.
* **The backfill window is now.** ch5's history already starts at 2026-08-17; the deep history on
  the other channels can be pulled once and is then safe locally whenever the quota wraps it on
  the device.

### Sizing against a bought disk

At the measured 39.7 GB/day:

| retention | disk |
| --- | --- |
| 30 days | 1.2 TB |
| 90 days | 3.6 TB |
| 1 year | 14.5 TB |

A 4 TB disk is ~100 days, 8 TB ~200. Tuning the NVR's detection (the region masks and sensitivity
noted above) would cut all of these roughly 4×, at the price of genuinely not recording some
motion. The guest's root disk holds under five days of this, which is why the replica got its own
volume: 1.4 TB at `/var/lib/timelapsed/archive` ≈ **35 days of full replica** at current NVR
settings. One caution inherited from the hardware: the backing pool spans two used laptop disks
with no redundancy, chosen deliberately because the trailing ~11 days are refetchable from the
device — the archive is a second copy, not the only copy, until footage ages off the NVR.

The WAN cost is the same 39.7 GB/day, ~42 minutes of link time daily. The measured throughput says
the Tailscale path runs direct, not relayed; the link needs nothing.

### What this changes in the stages

Structurally, nothing — stages 1 and 2 are identical either way. If the disk is bought:

* **Stage 3 fetches every segment** that appears in `nvr_segment` instead of a clip per recognition
  event — sequentially, same bounds, atomicity and semaphore; different trigger and selection.
* **Stage 4 plays the replica.** A recognition event links to a timestamp in footage that is
  already local, so the separate clip track and its refetchable-vs-irreplaceable retention tiers
  collapse into one archive with one retention number.
* The event-clip design above remains the fallback if the disk purchase does not happen — and the
  right shape on any future device that records continuously, where fetch-everything stops being
  affordable.

## Stages

Each is independently useful and shippable.

### Stage 1 — Index what footage exists — **built (2026-08-28)**

Poll `ContentMgmt/search` per channel on a slow timer into a new `nvr_segment` table in the existing
`index/index.sqlite3` — same database recognition already uses, same WAL discipline, analyzer-writes
/ viewer-reads. Handle the two device quirks: UUID `searchID`, 64 results per page.

Keep it a **rebuildable cache**: the NVR is authoritative for what it holds, so a `rebuild()` that
re-queries and repopulates is enough. Nothing here needs backing up.

Cost: near zero, no stored bytes. Yields an exact map of what footage exists, per channel, per
second.

As built: `timelapsed/nvr_footage.py` holds the search client and the `SegmentIndexer`; the
analyzer daemon runs a sweep every 15 minutes (it is the index's one writer). A channel's first
sweep covers the device's whole history; later ones re-ask only the last hour past the
`nvr_sweep` watermark, and the upsert extends a segment that was still being written when the
previous sweep saw it. Building it surfaced two more device quirks, recorded in the
[appendix](#appendix-isapi-notes): the search API speaks device-local time stamped `Z`, and it
silently truncates any search at 4,000 results, so a sweep resumes capped sessions.

### Stage 2 — Show it — **built (2026-08-28)**

Add a footage lane to the timeline beside the existing activity lanes, drawn from `nvr_segment`, so
it is visible which moments can be pulled. Serve it from a new endpoint — **not** `/api/events`,
which already means recognition events. `/api/footage` is free.

Extend the lane-colour whitelist rather than interpolating names into CSS; the existing XSS posture
must survive.

As built: `/api/footage?channel&start&end` merges segments server-side into runs at about a
pixel's resolution of the requested window (`AnalysisIndex.segment_runs`), so the payload tracks
the zoom level rather than the recording's duty cycle — a busy channel is hundreds of segments a
day, far more than the lane has pixels. The lane sits between the cadence and activity lanes,
draws only when the mirror has data, and is deliberately not clickable until stage 3 gives a run
something to do. The lane colour is a fixed CSS variable; nothing from the device reaches CSS.
The viewer tolerates an index from before schema v3 by answering an empty list.

### Stage 3 — Fetch clips — **built as the replica (2026-08-28)**

A `clip_fetcher` module: `POST /ISAPI/ContentMgmt/download` with the exact `playbackURI` from
`nvr_segment`, then `ffmpeg -i clip.ps -c copy -movflags +faststart clip.mp4`.

* **Never RTSP playback.** It hangs.
* Bound every fetch with a deadline and a subprocess kill regardless.
* Reuse the render-semaphore pattern so fetches cannot swamp the guest.
* Stage to scratch and move into place atomically, as renders already do, so a killed fetch never
  leaves a truncated file that looks valid.
* Store under `{root}/{channel}/clip/`, and record it in a `local_clip` table.

Trigger on recognition event close — the point at which the identity is settled and the plate voted
— so one fetch covers a settled event rather than firing per detection.

As built, per the replica decision: `timelapsed/archiver.py`, a fourth daemon
(`timelapsed-archiver`), fetches **every** segment the mirror lists rather than a clip per event —
sequentially (parallelism was measured to buy nothing) and oldest-first, because quota wrap
deletes oldest. There is no `local_clip` table: the archive is indexed by its filenames —
`{archive_root}/{channel}/{YYYYMMDD}/{start}_{end}_{device-name}.mp4` — so the recognition index
keeps its single writer and a crash can only ever lose a scratch file. A segment is fetched only
once its end is 30 minutes settled (an open segment's end keeps walking), and never fetched at
all if retention would delete it tomorrow. Configuration is the `[archive]` section; on this
deployment it points at the 1.4 TB volume. Failures are skipped until restart rather than
retried per pass, so a segment the device has expired cannot starve the queue.

### Stage 4 — Play them

A clip becomes the highest-value thing a recognition event can link to: from a named person or a
plate read, jump to the footage. The viewer already implements HTTP Range and nginx now serves
`/video/*.mp4` off disk, so playback needs little new machinery.

Extend `reclaim()` with clip tiers. Clips still inside the NVR's retention window are the most
disposable — refetchable. Clips of footage the NVR has since overwritten are the **least**
disposable, because nothing can regenerate them.

## What does not change

* **The 10-second snapshot poller stays.** It is the timelapse source and the input recognition
  reads. The argument above is against *sub-2-second* snapshots, not against snapshots.
* **Filename-as-index for stills stays.** Correct at 8,640 files/channel/day.
* **The render pipeline stays** — sampling, hardlink staging, the render semaphore, missing-window
  sweeps.
* **Recognition stays where it is.** It runs on stills, costs the NVR nothing, and already works;
  nothing here proposes moving inference onto video.
* **VM 302 does not become a recorder.** The NVR does not record continuously, and duplicating six
  live streams buys nothing the download API does not give at 70× realtime.
* **No face recognition.** Ruled out by measurement, not preference — see
  [Recognition Feasibility](Recognition-Feasibility.md). Pulling video does not change it; the
  faces are 38 px in the video too.
* **No authentication in the viewer.** Tailscale remains the access control — and clips of people
  raise the same LGPD concerns [Recognition](Recognition.md) already documents, more sharply.

## Open decisions

1. **Clip length and retention** — the table above; 60s at 90 days fits the current disk.
2. **Which channels.** ch5, ch6 and ch8 are 88% of NVR footage volume; ch7 and ch9 see almost no
   recognised activity.
3. **Whether to tune NVR detection.** Region masks and smart events would quadruple the device's own
   retention window, but mean genuinely not recording some things — a security-coverage call.
4. **Whether event playback is enough**, given continuous playback is not available from this device.
5. **Replica or clips — decided: replica** (2026-08-28), and the disk is installed. That makes
   decisions 1 and 4 mostly moot: retention becomes one number, and playback covers everything the
   NVR saw. Decisions 2 and 3 stay open — all channels are cheap enough to replicate, and NVR
   detection tuning remains worthwhile for stretching both the device's window and the archive's.

## Appendix: ISAPI notes

Hard-won specifics for whoever implements this.

* **`searchID` must be a real UUID.** Anything else returns HTTP 400, `statusCode 6`.
* **`maxResults` caps at 64 per page.** Page with `searchResultPostion` — note the device's spelling.
* **A search session truncates at 4,000 matches, and lies about it.** The page holding the
  4,000th match answers `OK` exactly as a genuinely final page does, mid-history, with no other
  signal (measured 2026-08-28 on ch1). Any code paging one `searchID` to completion silently
  loses everything after match 4,000. Resume with a **fresh** searchID whose window starts at the
  last returned segment's `endTime`; results arrive in ascending time order (also measured), so
  the seam costs one duplicated straddling segment. Treat a session that returned exactly 4,000
  as truncated — a real 4,000-segment result costs one extra near-empty session.
* **Downloads are per-recorded-segment.** An arbitrary time range without the `name=` and `size=`
  fields from a search result is rejected. The search index is the unit of fetch, so a clip window
  has to be mapped onto the segments covering it.
* **Downloads arrive as MPEG-PS**, not MP4 — and not bare: the stream opens with Hikvision's
  proprietary `IMKH` pseudo-header, with the standard `00 00 01 BA` pack header 32 bytes in.
  ffmpeg skips the wrapper; anything validating the first bytes has to accept both openings.
  Remux with `-c copy`; 0.17s for a 50s clip.
* **Keyframes land one per 1.6s** and extract at 32× realtime with `-skip_frame nokey`, so stills
  can be derived from fetched clips almost free.
* **`alertStream` works** — `multipart/mixed`, ~55 B/s of heartbeats — but is largely redundant
  here: it reports motion, while recognition already reports people, vehicles and plates. If used,
  it needs reconnect-with-backoff and a **read watchdog**; it blocks silently between events, and a
  75-second listen ran 149 seconds because the bound was only checked on chunk arrival.
* **`-rw_timeout` is not valid for the RTSP demuxer** on the guest's ffmpeg build.
* **The search API speaks device-local time stamped `Z`, in both directions.** Asking
  `ContentMgmt/search` for a window in true UTC answers `NO MATCHES`; the same wall-clock hour
  written as local-with-Z matches, and results come back stamped local-with-Z too (measured
  2026-08-28). `/ISAPI/System/time` reports the offset honestly
  (`<localTime>…-03:00</localTime>`) — read it once and translate every request and every
  result through it. The clock itself **is** NTP-synced and correct; only the stamps lie.
* Useful read-only endpoints: `/ISAPI/System/deviceInfo`, `/ISAPI/System/time`,
  `/ISAPI/ContentMgmt/Storage`, `/ISAPI/Streaming/channels`,
  `/ISAPI/ContentMgmt/InputProxy/channels` and `.../channels/status`,
  `/ISAPI/ContentMgmt/record/tracks/{ch}01`, `/ISAPI/Event/triggers`,
  `/ISAPI/System/Video/inputs/channels/{ch}/motionDetection`, `/ISAPI/Smart/capabilities`.
