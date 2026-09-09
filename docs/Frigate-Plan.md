# Frigate Plan

Replace both recorders with [Frigate](https://frigate.video) running on a Linux box at each site,
and keep Timelapsed as the interface. Frigate takes over capture and recording — the part that
must never miss — and Timelapsed keeps what it is good at: the timelapse tracks, the timeline, the
viewer, the long-term archive.

Planned, not built. Nothing here has been deployed.

## Why

### The recorders fail in ways nothing here can route around

On **2026-09-08** the Intelbras stopped answering every authenticated CGI call at 05:54 UTC and
was still refusing them twelve hours later. All five configured channels went dark together, so
the home site's timelapse has a twelve-hour hole in it. Probed from CT 303 while it was wedged:

| probe | result |
| --- | --- |
| ICMP | 0% loss, 0.8 ms |
| `GET /` (no auth) | `200` in 6 ms |
| `snapshot.cgi?channel=1` (digest) | no response in 30 s |
| `magicBox.cgi?action=getSystemInfo` | no response in 20 s |
| `global.cgi?action=getCurrentTime` | no response in 20 s |
| `ffprobe` RTSP main ch1 | `hevc,960,1080` |
| `ffmpeg -c copy -t 5` RTSP main ch1 | five clean seconds at 1.0× |

**The video pipeline was never affected.** The wedge is in the CGI and digest-auth subsystem, and
that subsystem is the only thing Timelapsed talks to. Its own reboot endpoint is CGI, so the box
cannot even be restarted remotely — recovery is a power cycle. See
[Operations](Operations.md#the-intelbras-stops-answering-its-cgi-api).

Eleven days of error counts put today's episode well outside the normal: a background rate of one
or two snapshot timeouts every couple of hours, one 28-error blip on 09-03 that healed inside two
hours, and then this.

The likely trigger is the polling itself. Five channels at a ten-second interval, each opening a
fresh digest handshake, is about **43,000 authenticated CGI requests a day** against firmware that
is known to leak session slots. Frigate opens one RTSP session per camera and holds it open for
weeks. That is not a smaller version of the same load; it is a different subsystem under three
orders of magnitude less of it.

This is a reason to expect the specific bug to go away, not a promise. One healthy RTSP probe
during one wedge does not prove RTSP survives everything. Stage 1 exists to answer that.

### Everything crosses the WAN twice, and it does not have to

The zermatt site is behind the `uberaba` subnet router, so every byte the archiver replicates and
every still the capture loop pulls rides that uplink. Measured on CT 303 on 2026-09-08:

| what | per day |
| --- | --- |
| stills, seven zermatt channels at 10 s | 11.0 GB |
| segments replicated (median of the last six days) | 20 GB |
| segments replicated (worst of the last six days) | 97 GB |

So somewhere between 30 and 110 GB a day of a remote site's video is pulled across a domestic
uplink to be stored at a different site, where a nightly `rclone` then pushes most of it up a
second domestic uplink to Backblaze. A recorder at the site reduces the first leg to metadata and
whatever someone is actually watching, and lets the site upload its own footage directly.

### The disks are nearly full at the wrong site

```
/dev/mapper/pve-vm--303--disk--1  147G  131G  9.1G  94% /var/lib/timelapsed
/dev/mapper/hdd-vm--303--disk--2  1.6T  1.4T  111G  93% /var/lib/timelapsed/archive
```

The 1.6 TB pool at home is 93% full holding **zermatt's** footage. Moving the replica to the site
that generated it hands that entire pool to the home site's own recordings, which is most of what
Frigate needs there. The storage problem and the reliability problem have the same fix.

## What each site becomes

```
Home site — 192.168.50.0/24              Zermatt site — 192.168.18.0/24
  pve1                                     pve2  (new, standalone node)
    CT frigate-home                          CT frigate-zermatt
      records, detects, serves video            records, detects, serves video
    CT 303 timelapsed                        CT timelapsed
      capture, renders, viewer, portal          capture, renders, viewer
    MHDX 1308                                7 Hikvision IP cameras, direct
      RTSP encoder only (stage 1)            DS-7616NXI-K1 — retired (stage 3)
      retired with the analog cameras
                    │                                      │
                    └──────── Tailscale, metadata ─────────┘
                              and on-demand video
```

**pve2 is not clustered with pve1.** A two-node Proxmox cluster over a WAN link loses quorum every
time the link drops, and a node without quorum will not start or stop a guest without
`pvecm expected 1`. Two standalone nodes on the same tailnet, and nothing more.

**Frigate is never exposed to the browser.** It binds to its container's LAN address with
`auth.enabled: false`, and the site's own Timelapsed viewer and nginx are the only things that
talk to it. That keeps the existing posture — Tailscale is the front door, there is no second
login — and it means the browser sees one origin per site. The federation half of this is
[Multi-Site](Multi-Site.md).

## The device driver

`[nvr.<name>]` already carries a `type =` that selects which API a device speaks, and the channel
id it produces is the only NVR dimension anything downstream has. So Frigate arrives as a third
value of `type`, and nothing below the driver has to know.

```ini
[nvr.zermatt]
type = frigate
url = http://127.0.0.1:5000
cameras = 1=oficina-interno, 5=portao-social-superior, 6=geral-lote,
          7=fundos-oficina, 8=rua-oficina, 9=frente-oficina, 10=area-gourmet
channels = 1,5,6,7,8,9,10
```

`NVR_KINDS` at `timelapsed/schema.py:20` gains `"frigate"`, and `capture_agent_for()` at
`timelapsed/nvr_capture_agent.py:95` gains a branch. A new `FrigateCaptureAgent` implements the
same one-method interface `NVRCaptureAgent` and `DahuaCaptureAgent` already implement, so
`timelapsed.py:425` calls it without knowing the difference.

### `cameras` exists to protect the library

This is the load-bearing detail of the whole migration. The channel id **is** the directory name
(`{root}/{channel_id}/image/…`), the index key, the archive path, the viewer's URL and the
go2rtc stream name. Frigate identifies cameras by name, Timelapsed by number. If a Frigate camera
name became the channel id, every path would change and six months of stills, keyframes and
rendered videos would be stranded beside a new empty tree under a new name.

So `cameras` maps channel id to Frigate camera name, and `channels` keeps meaning exactly what it
means today. The ids `1`, `5`, `6`, `7`, `8`, `9`, `10` survive the recorder swap untouched, and
the monthly and progress videos keep growing across it. Where the key is absent the camera name
defaults to the channel id, which is what a site set up from scratch would want.

Frigate camera names are YAML keys and appear in its own paths, so they are held to the same
character class the `[nvr.<name>]` name already is.

### Stills come from go2rtc, not from Frigate's snapshot API

Frigate's `latest.jpg` serves a frame from the **detect** stream, which is 640×360 by design. That
is a downgrade on both sites. The full-resolution frame comes from the go2rtc instance Frigate
runs internally:

```
GET http://127.0.0.1:1984/api/frame.jpeg?src=<camera>
```

That reads the same producer Frigate already has open for recording, so it costs the camera
nothing and adds no stream. Two consequences to design around:

* **The frame is a keyframe, and go2rtc waits for the next one.** The zermatt cameras run GOP 60
  at 30 fps, so a request can block up to two seconds and the image can be up to two seconds older
  than the moment it was asked for. At a ten-second interval that is tolerable jitter, and it is
  well inside capture's `(5s connect, 20s read)` budget — but the stored filename is the request
  time, so the name is now up to two seconds off the truth. Keyframe promotion's tolerance is
  measured in minutes, so it does not care.
* **`Content-Type` checking stays.** `timelapsed/nvr_capture_agent.py` verifies the response is
  actually an image because NVRs answer failures with `200 OK` and an XML body. go2rtc answers a
  missing producer with a text error and the same status, so the check earns its keep unchanged.

On the home site this is a straight quality win: 960×1080 from the camera's own encode instead of
the 704×480 the MHDX's `SnapFormat` is stuck at, and no CGI call in the loop at all.

There is a second, better-timed source worth knowing about and not using yet.
`GET /api/camera/<name>/recordings/frame/<epoch>.jpg` pulls a frame out of the recording at an
exact timestamp, full resolution, no stream open. It is exact where `frame.jpeg` is approximate,
but it can only answer for moments the recorder actually kept — and under the event-only policy
below, that is not every ten seconds. It is the right tool for **repairing** a gap after the fact,
not for driving the capture loop.

## Recording policy: events, not continuous

Frigate's `record` block splits retention four ways, and the current schema is not the one older
guides show — `record.retain.days` was replaced by separate `continuous` and `motion` blocks.

```yaml
record:
  enabled: true
  continuous:
    days: 0          # keep no footage purely for existing
  motion:
    days: 7          # 10 s segments where motion was seen
  alerts:            # review.alerts.labels — person, car
    pre_capture: 5
    post_capture: 30
    retain:
      days: 30
      mode: motion
  detections:        # everything else detect.objects tracks
    pre_capture: 5
    post_capture: 15
    retain:
      days: 14
      mode: motion
```

`mode: all` keeps a whole event span, `motion` drops the still segments inside it,
`active_objects` keeps only frames where the object moved. Verify every key against the release
notes of the tag actually pinned; these keys moved once already and will again.

What it costs, at the measured bitrates:

| site | streams | continuous | event-only, detection untuned | 30 days, event-only |
| --- | --- | --- | --- | --- |
| zermatt | 7 × 2048 kbit/s | 155 GB/day | ≈ 46 GB/day (50 video-hours) | ≈ 1.4 TB |
| home | 8 × 1024 kbit/s | 88 GB/day | unmeasured; assume 20% duty | ≈ 0.5 TB |

The zermatt event-only figure is the [NVR Roadmap](NVR-Roadmap.md#it-records-on-events-not-continuously)'s
50 video-hours a day, and that number is inflated on purpose: `PostRecordTimeSeconds = 30` on
every channel, no region mask, sensitivity 60. Frigate's motion masks are the same lever and are
much easier to tune, so expect the real figure to land nearer a quarter of that once the street
and the trees are masked out. Size the disk for the untuned number anyway.

## The footage lane, without a replica

The lane exists to answer "which moments can I actually play". Today that takes a mirror of the
device's segment list (`nvr_segment`, `nvr_sweep`), a daemon that downloads every segment before
quota wrap deletes it, and five colours to say how far each stretch got. All of that is
scaffolding around one problem: **the footage was on a device that would delete it and would not
reliably hand it over.**

A recorder at the site with its own disk removes the problem rather than managing it. The lane's
question collapses to one call:

```
GET /api/camera/<name>/recordings?after=<epoch>&before=<epoch>
GET /api/camera/<name>/recordings/summary          # true/false per day, for the zoomed-out view
```

`footage_status_runs()` in `timelapsed/catalogue.py:200` keeps its job — merge rows into runs at
about a pixel's resolution of the requested window — but reads Frigate instead of SQLite, and
returns one status instead of five. The lane goes back to meaning "the recorder holds this", which
is what it looked like before the archiver existed.

Retired by this: `timelapsed/archiver.py` and its unit, `FootageClient` and both its
implementations in `timelapsed/nvr_footage.py` and `timelapsed/dahua.py`, the `nvr_segment` and
`nvr_sweep` tables, the segment-name-reuse keying from `a2f2e1d`, the five-attempt backoff and
`.abandoned.json` write-off, `.archiver-status.json`, and the archiver's New Relic app and
backlog gauges. The five status colours in `index.html:14` collapse to the one `--footage`
variable that predates them.

Kept: the index's other seven tables, and `AnalysisIndex`'s single-writer discipline — which gets
easier, because the analyzer stops being that writer's only competitor.

## Playback

Frigate serves any time range as HLS off the 10-second segments, no re-encode:

```
GET /vod/<camera>/start/<epoch>/end/<epoch>/index.m3u8
GET /api/camera/<camera>/start/<epoch>/end/<epoch>/clip.mp4     # muxed on demand
```

`clip.mp4` is a drop-in for what `/archive/<channel>/<day>/<file>.mp4` serves today: one file,
Range-able, plays in the existing `<video>` element with no new code. Start it there. It muxes on
request, so it is the wrong shape for scrubbing a long window.

`index.m3u8` is the right shape, and costs the viewer a dependency. Safari plays HLS natively;
everything else needs hls.js. The constraint to respect is in
[Architecture](Architecture.md#the-web-viewer): there is exactly one `<video>` element, built once
and kept, because rebuilding it loses the buffer. hls.js attaches to and detaches from an existing
element, so this works — but `select()` at `index.html:1067` becomes "detach any current hls.js
instance, then either set `src` or attach a new one", and the blob-prefetch handoff at
`index.html:987` has no meaning for a manifest and must be skipped for footage entries rather than
left to misfire.

Two reported limits worth designing around: VOD playback degrades badly on ranges spanning
thousands of segments, so cap the window a single manifest may cover and stitch in the viewer; and
a VOD URL for a just-closed event can 404 until the recorder has flushed, so the "jump to real
footage" button needs a retry rather than an error.

## Detection

Frigate detects people and vehicles as part of recording, and exposes them:

```
GET /api/events?after=&before=&cameras=&labels=
GET /api/review?after=&before=&severity=
GET /api/events/<id>/snapshot.jpg      GET /api/events/<id>/thumbnail.jpg
```

So the sightings on the timeline come from `/api/review`, and `yolox_tiny.onnx` stops running.
Do not keep both detectors on the same footage: two sets of person boxes on one timeline is not a
richer timeline, it is two things to reconcile.

What Frigate does not do is cross-camera re-identification, which is `identity`, `signature` and
`IdentityMatcher` — the part of recognition with no replacement. Keep it, and change what it eats:
today it reads 8,640 stills per channel per day looking for people; it should read Frigate's event
snapshots instead. Those are the frames Frigate already judged best, at full resolution, a few
hundred a day instead of sixty thousand. Better input, a fraction of the CPU, and the re-ID index
keeps working. Plate reading moves the same way, with the same caveat
[Recognition Feasibility](Recognition-Feasibility.md) already records: a better-chosen crop does
not add pixels that were never there.

## Long-term keeping and offsite

Frigate's retention is a number of days and a delete loop. It has no concept of "keep this
forever", and the archive does — the whole point of the 1.6 TB pool is that footage outlives the
recorder's window.

So the archive survives the archiver's retirement, fed differently:

```
POST /api/export/<camera>/start/<epoch>/end/<epoch>
```

An export is a permanent file Frigate will not reap. Driving that from a closed recognition event
is the "fetch a clip per event" design from
[NVR Roadmap stage 3](NVR-Roadmap.md#stage-3--fetch-clips--built-as-the-replica-2026-08-28) — which
was rejected then because clips were the *only* thing obtainable and a replica was strictly better.
With a local recorder holding a month of everything, clips are no longer a compromise: the month is
the safety net and the exports are the history.

Offsite then changes shape twice over. Each site uploads its own, so no footage crosses the WAN to
reach B2. And what it uploads is exports, not raw segments — which matters, because Frigate writes
8,640 files per camera per day and B2 bills listings as Class C transactions. `timelapsed/offsite.py`
keeps its full/tail pass logic and its bandwidth timetable; only the source tree and the account
change.

## Hardware

**pve2, at zermatt.** Seven 1080p H.265 streams recorded with `-c copy` and detected on 640×360
sub-streams at 5 fps, plus Timelapsed's capture and renders. Two shapes fit:

* An **N100/N150 mini PC**, 16 GB, with an M.2 for the system and either a second M.2 or a 2.5"
  bay for footage. Hardware HEVC decode and OpenVINO detection on the iGPU, so no Coral. Cheap,
  silent, ~10 W. Constrained to 2 TB unless the second slot takes a 4 TB M.2.
* A **used SFF desktop** (OptiPlex/ThinkCentre, i5-8500 or later) with a 3.5" bay, which is the
  cheaper route to 4 TB and gives QSV decode. More watts, more noise.

Take the second if a month of untuned event recording is the target, the first if detection gets
masked properly first. Proxmox on it either way — same `pct` workflow, same snapshot-before-you-
break-it habit, and the [rescue hatch](Proxmox-Deployment.md#a-rescue-hatch-vms-never-had) that
containers give. A UPS matters more at a site nobody is standing in, and out-of-band access
matters more still: pve2 wants the same NanoKVM treatment pve1 has.

**pve1, at home.** Already suitable: a Ryzen 5 5600G with a Vega iGPU, which does VAAPI HEVC
decode. **Detection runs on the CPU, not that iGPU** — OpenVINO's GPU plugin supports Intel
hardware only, so on an AMD part it falls back to the CPU device and a Radeon contributes nothing
to inference. That is cheap enough here to be a non-issue: measured 2026-09-09, OpenVINO on the
5600G's CPU runs ssdlite_mobilenet_v2 in **8–12 ms**, and five cameras detecting at 5 fps leave the
host 63% idle. The empty chipset M.2 slot takes a Coral or a Hailo if a future camera count needs
one. RAM is the constraint on that node, not cores — a Frigate container serving five cameras
measured **1.1 GB resident**, against about 10 GB free, so check `free -m` before starting it.
Storage is the 1.6 TB pool the zermatt replica currently occupies, which stage 3 hands back.

The Vega is not unusable for inference, only unusable *by OpenVINO*. Frigate supports AMD GPUs
through the ONNX detector in the `-rocm` image variant
(`ghcr.io/blakeblackshear/frigate:stable-rocm`, `detectors: {amd: {type: onnx}}`). Not taken here,
on purpose: the 5600G's iGPU is Cezanne **gfx90c**, which is not on ROCm's supported list and needs
`HSA_OVERRIDE_GFX_VERSION` to load at all, the image is several GB larger, and the thing it would
accelerate already costs 8–12 ms on a host that is 63% idle. It is the right lever only if the home
site grows well past five cameras, or wants a model bigger than `ssdlite_mobilenet_v2`.

This still shapes the pve2 buy: the N100 above gets OpenVINO **on its iGPU** with no override, no
alternate image and no unsupported chipset, which is worth more than raw throughput on a box that
has to run unattended at a site nobody visits.

### Why not a Raspberry Pi

It is the obvious cheap answer and it is the wrong one here, for a reason that has nothing to do
with the Pi being slow.

**Proxmox is x86-only.** The node is wanted as a Proxmox node — `pct`, a snapshot before every
change, the [rescue hatch](Proxmox-Deployment.md#a-rescue-hatch-vms-never-had), the same shape as
pve1 so one set of habits covers both sites. On ARM that becomes plain Debian with Docker and none
of [Proxmox Deployment](Proxmox-Deployment.md) applies any more. Pimox exists and is neither
official nor maintained enough to put a remote site on.

**Storage is the second problem, and the worse one in practice.** A month of untuned event
recording at zermatt is 1.4 TB written continuously, for years, at a site nobody is standing in.
A Pi has no SATA. USB enclosures are the known weak point under exactly that load — UAS resets and
power-delivery dropouts — and NVMe through the Pi 5's M.2 HAT costs more at 2 TB than the entire
rest of the build. Replacing unreliable hardware with a recorder on a USB disk reintroduces the
class of failure this plan exists to remove.

**The cost saving does not survive the parts list.** A Pi 5 with 8 GB, a PSU, a case, an M.2 HAT,
an NVMe and either an AI HAT or a Coral lands at or above an N100 mini PC — which Frigate's own
hardware page recommends as its current favourite, which needs no separate accelerator because
OpenVINO runs on the iGPU, which takes SATA, and which runs Proxmox.

The codec question matters less than it looks. **Recording is `-c copy`, so nothing is decoded to
write it**; decode exists only for the detect streams, and seven of those at 640×360 and 5 fps is
trivial on anything. For the record: the Pi 5 kept HEVC hardware decode and dropped the H.264
block, which is the lucky direction here since every zermatt main stream is H.265 — but Frigate
ships `preset-rpi-64-h264` and `preset-rpi-64-h265` for the Pi 3/4 only, and Pi 5 HEVC decode is a
community recipe rather than a supported preset.

A Pi is a fine Frigate host for one or two cameras with a small retention window on an NVMe. It is
not the shape of either of these sites.

## Stages

Each stage is useful on its own and leaves a working system.

### Stage 0 — make the DVR recoverable

A switched plug (Shelly, Tasmota, anything Home Assistant on VM 210 already speaks) and a NanoKVM
on the MHDX. Then a New Relic condition that actually fires on this failure. Details and the
reasoning about which of the two does what are in
[Operations](Operations.md#the-intelbras-stops-answering-its-cgi-api). Days, not weeks, and it
stops the next wedge costing twelve hours.

### Stage 1 — Frigate on pve1, against the MHDX's RTSP — **running (2026-09-09)**

No new hardware, no code, no camera changes. A container on pve1 recording the DVR's
`cam/realmonitor` streams beside everything that runs today.

This is the soak test the argument above needs: a week of recordings with no gaps says RTSP
survives what CGI does not, and the encoder-only plan holds. A week with gaps says the box is
finished and the analog cameras move to the front of the queue. Either answer is worth a week.

**Recording is continuous here, not event-only**, which is the opposite of what the finished system
wants. Under event-only a gap is ambiguous — no motion and no stream look identical — and the whole
point is to detect stream drops. In a continuous timeline a gap *is* a drop. Switch to the
event-only block above once the soak passes.

As deployed: **CT 306** on pve1, unprivileged, `nesting=1,keyctl=1`, 4 cores, 6 GB, a 250 GB
rootfs on `local-lvm`, `onboot=1`, `/dev/dri/renderD128` passed through with `dev0`. Docker from
the upstream repo, Frigate **0.17.2**. Channels 1–5; inputs 6–8 have no cameras and are left out
deliberately, because a permanently failing stream would drown the one signal the test exists to
read. Timelapsed stopped capturing from the device the same day — its `[nvr.intelbras]` section
was **renamed** to `[disabled-nvr.intelbras]` rather than deleted, so credentials and channel list
survive and re-enabling is a one-word edit. All intelbras history stays visible in the viewer,
because `TimelapseCatalogue.channels()` scans the library tree rather than the config; only the
live wall, which reads `config.channels`, loses those tiles.

A `frigate-soak` systemd unit samples `/api/stats` once a minute into `/var/log/frigate-soak.log`
— per-camera capture fps, segment count, free space, and deltas for stream drops and container
restarts. A flat file beats a dashboard because the artefact of interest is a gap, and a gap is
easiest to see in a column of numbers. It survived an unplanned power cut on the first afternoon
and logged it as one `API-DOWN` line between two healthy samples; every guest, the container, the
soak unit and Tailscale's serve config came back without intervention.

First measurements, five analog cameras: 10-second segments of **1.31 MB** each, `hevc 960×1080`,
remuxed not transcoded; **56 GB/day**, which is why `continuous.days` is 3 rather than 4 — four
days would land on 225 GB against 224 GB free. Ten simultaneous RTSP sessions (record plus detect
per camera) run without complaint, so the DVR has no session budget worth designing around.

### Stage 2 — `type = frigate`

The driver, `cameras`, stills from go2rtc, the lane from Frigate's recordings API, playback from
`clip.mp4`. Home site only, with the Intelbras channels moved over and zermatt still on ISAPI —
which is exactly what the multi-NVR support was built for, and the reason this can be done without
a flag day.

Biggest code change of the plan, and it lands before any of the traffic win, because stage 3 has
nothing to point at until it exists.

### Stage 3 — pve2, and the archiver's retirement

Frigate at zermatt against the seven cameras directly. Timelapsed beside it, capturing locally.
Then cut the zermatt channels over, stop `timelapsed-archiver`, and let the 1.6 TB pool at home
drain into the home site's own recordings.

This is where the WAN traffic goes away. Both sites are standalone and complete at the end of it;
neither knows about the other yet.

### Stage 4 — federation

The portal at pve1, remote lanes, media fetched by the browser straight from the site that holds
it. [Multi-Site](Multi-Site.md).

### Stage 5 — detection from Frigate

Sightings from `/api/review`, `yolox_tiny.onnx` retired, re-ID and plates repointed at event
snapshots.

### Stage 6 — retire the hardware

The DS-7616NXI-K1 unplugs as soon as stage 3 is trusted; its cameras were never behind it. The
MHDX needs its eight analog cameras replaced first — PoE-over-coax adapters reuse the existing
runs, which is the whole reason this is last rather than never.

## What does not change

* **The 10-second snapshot poller stays.** Frigate has no long-horizon timelapse; its export
  timelapse is a sped-up recording. The keyframe track, the monthly and progress renders and the
  eight-day still window are why Timelapsed exists and none of them come from a recorder.
* **Filename-as-index stays.** So does UTC on disk, and so do the channel ids — see `cameras`.
* **The render pipeline stays**, untouched: sampling, hardlink staging, the semaphore,
  missing-window sweeps.
* **The archive stays**, fed by exports instead of by wholesale replication.
* **No authentication in the viewer**, and none in Frigate either. Tailscale remains the access
  control, and the LGPD concerns in [Recognition](Recognition.md) get sharper, not softer, when a
  month of continuous-quality footage sits at each site.
* **`[nvr]` keeps its meaning.** A device that speaks ISAPI or Dahua CGI stays supported; this adds
  a third kind rather than replacing the first two.

## Open decisions

1. **Retention days per site**, which is the disk purchase. 30 days untuned at zermatt is 1.4 TB.
2. **Whether stage 1's soak passes**, which decides whether the MHDX survives as an encoder or the
   analog cameras get replaced sooner.
3. **Whether motion masks come before or after sizing the disk.** Tuning first makes a 2 TB mini PC
   sufficient; sizing first means never having to revisit it.
4. **Continuous or event-only at home.** 88 GB/day continuous against a pool that stage 3 frees
   entirely — it would fit. The zermatt NVR could never offer continuous playback and this would.
5. **Whether the analyzer keeps a detector at all**, or becomes purely re-ID and plates over
   Frigate's crops. The second is less code and better input; the first keeps the timeline working
   if Frigate is down.
6. **Frigate config as source of truth for cameras, or `timelapsed.ini`.** `go2rtc_config.py`
   renders go2rtc's YAML from the ini today. Frigate's config carries masks and zones that no ini
   should own, so the direction should probably reverse: Frigate owns cameras, and Timelapsed
   discovers them from `/api/config` with `cameras` only pinning the historical ids.

## Appendix: Frigate specifics

Verified against the docs on 2026-09-08. Frigate's config keys and API paths have both moved
between minor releases — pin an exact image tag and re-check these against that tag's own docs.

* **Ships only as a container image.** No wheel, no deb, no tarball. The image carries a patched
  ffmpeg, go2rtc, nginx, s6-overlay and the detector runtimes. An LXC needs `features: nesting=1`
  and Docker inside it; the community-scripts native install replays the Dockerfile onto a bare
  container and is explicitly unsupported upstream.
* **`record.retain.days` no longer exists.** It is `record.continuous.days` and
  `record.motion.days`, with `record.alerts` and `record.detections` carrying their own
  `pre_capture`, `post_capture` and `retain`.
* **Which labels count as an alert** is `review.alerts.labels`, not a record setting.
* **The default record preset strips audio.** Use `preset-record-generic-audio-copy` or
  [Captions](Captions-Plan.md) has nothing to transcribe. Every zermatt segment carries AAC mono
  16 kHz today and that must survive the swap.
* **`latest.jpg` is detect resolution.** Full-resolution stills come from the bundled go2rtc at
  `:1984/api/frame.jpeg?src=<camera>`, which waits for a keyframe.
* **VOD is nginx-served at `/vod/…`**, outside `/api/`. `master.m3u8` and `index.m3u8` are both
  accepted.
* **Recent versions enable authentication by default** and print a generated password on first
  start. Behind Tailscale, set `auth.enabled: false` deliberately rather than carrying a JWT in
  the capture loop.
* **`/api/camera/<name>/…`** is the current shape for per-camera endpoints; older guides show
  `/api/<name>/…`. Check before writing a client.
* **10-second segments** at `recordings/<YYYY-MM-DD>/<HH>/<camera>/<MM.SS>.mp4`, UTC. 8,640 files
  per camera per day is the number that makes raw-segment offsite a bad idea.

Found the hard way while bringing stage 1 up, all on 0.17.2:

* **The openvino detector needs an explicit `model` block.** Without one `model_path` is `None`,
  the detector process dies with `TypeError: stat: path should be string... not NoneType` on every
  boot, and it takes the API down with it. What you see is nginx serving **500s**, with nothing in
  the visible error mentioning the detector. The bundled model is
  `/openvino-model/ssdlite_mobilenet_v2.xml` with `labelmap_path`
  `/openvino-model/coco_91cl_bkgr.txt`, `width`/`height` 300, `input_tensor: nhwc`,
  `input_pixel_format: bgr`.
* **`docker compose restart` does not re-read `env_file`.** Compose reads it when the container is
  *created*, so changing credentials needs `docker compose up -d --force-recreate`. A restart
  silently keeps the old ones, which reads as "the new password does not work".
* **Percent-encode the RTSP password.** ffmpeg parses the URL before the device sees it, so a `#`
  becomes a fragment and fails with `Port missing in uri` — a message that says nothing about
  passwords. This device's password ends in `#`.
* **A 401 storm is not evidence about the device.** During stage 1 bring-up every stream returned
  `401 Unauthorized` while a single `ffprobe` from another container succeeded minutes apart. It
  was neither the credentials, the `-user_agent` Frigate adds, the `{VAR}` substitution, nor a
  session limit — all four were tested and eliminated. A new DVR account fixed it. Test one stream
  by hand before theorising about the recorder.
* **Frigate has its own auth, on by default.** `auth.enabled: false` is a deliberate choice here,
  matching the viewer and go2rtc: Tailscale is the front door, and there is no second login.
