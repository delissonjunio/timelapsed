# Configuration

Timelapsed reads INI files from three locations, in this order:

1. `/etc/timelapsed.ini` — system-wide, used by the systemd units
2. `~/.timelapsed.ini` — per user
3. `./timelapsed.ini` — the working directory

**Later files override earlier ones key by key**, not file by file. You can keep the bulk of the
config in `/etc` and override just `logging_level` in your home directory. If none of the three
exist, startup fails with a `FileNotFoundError` naming all three paths.

The file contains your NVR password. `chmod 640` it, own it `root:timelapsed`, and never commit it.
`.gitignore` blocks `*.ini` for exactly this reason.

## `[nvr]` and `[nvr.<name>]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `url` | yes | — | Base URL of the NVR, e.g. `http://192.168.1.10`. API paths are appended; a trailing slash is stripped for you. |
| `username` | yes | — | NVR user. A read-only account is enough. |
| `password` | yes | — | Sent using HTTP Digest auth, never logged. |
| `channels` | yes | — | Comma-separated channel numbers, as the device counts them, e.g. `1,2,3`. Whitespace is trimmed. On an ISAPI device channel `1` maps to track `101`. |
| `type` | no | `hikvision` | Which API the device speaks: `hikvision` (ISAPI) or `dahua` (the CGI API Dahua-OEM devices such as Intelbras use). |

One process is started per channel, so the channel count is also the process count and roughly the
memory multiplier.

### More than one recorder

Further devices are `[nvr.<name>]` sections with the same keys. The unnamed `[nvr]` section is the
**default NVR** and its channels keep their bare numbers as ids everywhere — directories, index
rows, URLs, go2rtc stream names — so a single-NVR install keeps meaning exactly what it always
meant. A named section's channels become `<name>-<number>` (`garage-1`), which is what lets two
devices both have a channel 1 without anything colliding. That composite id **is** the channel id
everywhere downstream; no other layer carries an NVR dimension of its own.

The name appears in paths and URLs, so it is held to lowercase letters, digits, `-` and `_`.
Every channel-shaped setting elsewhere in the file (`[analysis] plate_channels` among them) takes
these global ids: `5` means the default NVR's channel 5, `garage-1` the garage's channel 1.

## `[capture]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `interval_seconds` | yes | — | Seconds between snapshots, per channel. **This is your disk usage dial.** See [Storage Planning](Storage-Planning.md). |
| `resolution.width` | yes | — | Requested snapshot width. |
| `resolution.height` | yes | — | Requested snapshot height. |

The NVR may ignore the requested resolution and return its native size; Timelapsed stores whatever
comes back.

## `[timelapse]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `duration_seconds` | yes | — | Target length of the rendered video. Surplus frames are sampled out, so this holds regardless of window size. |
| `output_fps` | no | `30` | Playback frame rate. `24`–`30` looks natural; higher just costs bitrate. |
| `min_frames` | no | `60` | Refuse to render fewer frames than this. At 30 fps, 60 frames is a 2-second video. |
| `cadences` | no | `hourly,daily,weekly` | Which timelapses to produce. Any subset of `hourly`, `daily`, `weekly`, `monthly`, `progress`. An unknown name is a startup error. |
| `timezone` | no | `UTC` | IANA zone name the cadences roll over on, e.g. `America/Sao_Paulo`. An unknown name is a startup error. |
| `output_fps.<cadence>` | no | `monthly` 6, `progress` 6 | Per-cadence playback rate. |
| `min_frames.<cadence>` | no | `monthly` 5, `progress` 10 | Per-cadence minimum. |
| `deflicker` | no | `true` | Even out auto-exposure between frames a day apart. Keyframe-sourced renders only. |
| `max_concurrent_renders` | no | `1` | How many renders may run at once across **all** channels. |

### The construction-progress cadences

`monthly` and `progress` are a different kind of video from the other three: one frame per day —
or per `[keyframe] every_minutes` step, when dense promotion is on — rather than one every few
seconds. `monthly` covers one calendar month; `progress` covers everything captured so far, in one
file, re-rendered on the 1st.

Both read the **keyframe track** rather than the stills — see `[keyframe]` below and
[Architecture](Architecture.md) — because a month of stills does not fit on the disk. Enabling them
is what starts the daily promotion; until then the keyframe directory is never created.

**They do not inherit `output_fps` and `min_frames`.** The baselines are tuned for a window holding
thousands of stills, and are wrong for one frame a day in both directions: at 30 fps a 31-frame
month plays in one second, and a 60-frame minimum would refuse to render a month that can never hold
more than 31. `install.sh` copies the shipped template verbatim, so those baselines *are* written
into `/etc/timelapsed.ini`, and inheriting them would silently break every monthly render. They fall
back to their own defaults instead. An explicit `output_fps.monthly` still wins.

At 6 fps a month is about five seconds and a year is about a minute. Past `duration_seconds ×
output_fps` frames — 360 by default, so roughly a year — the progress video starts evenly sampling
days out and holds at a fixed length rather than growing forever. If you would rather keep every day
at any length, raise `duration_seconds`.

### Which midnight a daily closes at

`timezone` decides the wall clock a period is measured on. On the default `UTC` a daily covers
midnight to midnight UTC; set `America/Sao_Paulo` and it covers midnight to midnight in Sao Paulo,
which is 03:00 UTC to 03:00 UTC. Either way it is a full, contiguous 24 hours — the window slides,
it does not shrink, so consecutive dailies still meet exactly. `hourly` is unaffected outside the
half-hour zones, and `weekly` still turns over on Monday, just a local one.

Stored filenames stay UTC regardless. The library stamps them with `%Z` and reads them back as UTC,
and `parse_timelapse_filename` splits the window on `-`, so a zone abbreviated `-03` would not
survive the round trip. Only the choice of period moves; the timestamps written to disk do not.

Rollovers are detected, not scheduled: the daemon notices the clock has entered a new period on its
next capture cycle, so a render lands within one `interval_seconds` of the boundary rather than
exactly on it.

**Changing this setting re-renders recent history.** Windows are offered by what is missing, and a
day rendered on the old boundaries does not fill a window on the new ones, so the daemon will work
back through what it can still see — bounded by `image_retention_days`, the cadence's own retention
and a 30-day ceiling, at four windows per pass. The videos made on the old alignment stay until
retention expires them, so the viewer shows both for a while. Nothing is lost and no window is
skipped; it just costs some ffmpeg time. Set it once, before the archive matters.

`duration_seconds × output_fps` is the target frame count. The renderer picks that many stills,
evenly spaced across the whole window. If the window holds fewer, all are used and the video comes
out shorter — it is never padded or slowed down.

`max_concurrent_renders` is a RAM budget written as a process count. Every channel rolls over on the
same tick, so without it six cameras start six ffmpegs at once, each peaking around 250 MB at 1080p.
Budget ~300 MB per concurrent render on top of the daemon itself and leave the guest room to
breathe; `1` is right for anything with 2 GB. Renders that cannot start are not dropped — they wait
their turn, and anything missed is picked up as a missing window later.

## `[keyframe]`

The daily construction-progress frame. Only read when a keyframe-sourced cadence (`monthly`,
`progress`) is enabled; a plain hourly/daily/weekly deployment never pays for any of this.

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `at` | no | `12:00` | Local wall-clock time to take the daily frame at, as 24-hour `HH:MM`, on the `[timelapse] timezone`. An unparseable value is a startup error. Ignored when `every_minutes` is set. |
| `tolerance_minutes` | no | `30` | How far from the scheduled instant a stored still may be and still count as its frame. |
| `every_minutes` | no | unset | Dense promotion: a keyframe every this many minutes across `between`, instead of the single daily `at`. Must be set together with `between`. |
| `between` | no | unset | The local wall-clock window dense promotion covers, as `HH:MM-HH:MM`, both ends included. May not cross midnight. |

### Dense promotion: making the monthly a real video

One frame a day makes the monthly a flipbook: a month plays in a few seconds however low the frame
rate is pushed. Setting `every_minutes` and `between` promotes a keyframe on every step across the
window each day — `every_minutes = 15` with `between = 06:00-18:00` turns a month into about a
minute of playback at `output_fps.monthly = 24`, and the `progress` video grows by a couple of
seconds a day instead of a couple of seconds a month.

The window is what keeps the sun in the frame: promoting around the clock on infrared cameras puts
a grey night between every daylight second of playback. When going dense, also drop
`tolerance_minutes` to at most half of `every_minutes` (Timelapsed warns otherwise — around capture
gaps a wider tolerance can promote the same still for two neighbouring instants) and raise
`output_fps.monthly` / `output_fps.progress` to the rate you actually want.

Keyframes promoted before the change stay: earlier days keep their single noon frame and play as a
brief flipbook prefix in the next render, then the dense days take over. Storage stays small — a
1080p still is a few hundred KB, so even 15-minute promotion is a few GB per channel per year.

### Why a time of day and not an interval

A months-long timelapse lives or dies on a **constant sun angle**. Take the frame at "every 24
hours" and it drifts; take it at local noon and every frame has the same shadows, so what moves in
the video is the building rather than the light.

Noon is the default for two reasons: the sun is highest, so shadows are shortest and seasonal drift
is smallest; and noon exists unambiguously in every timezone, which midnight does not — some zones
spring forward at 00:00 and that local time simply does not occur on one day of the year.

Pick a different hour if the site faces east or west and gets better light off-noon. Whatever you
pick, it needs to be an hour the cameras are actually recording.

### What happens when there is no frame

If no stored still falls within `tolerance_minutes` of the time — the camera was down over noon, the
network was out, the NVR was rebooting — **that day is simply absent** from the video. There is
nothing to retry: the stills that could have filled it are the ones being pruned.

`tolerance_minutes` must be longer than `interval_seconds`, or most days will have no still close
enough. Timelapsed warns at startup if it isn't.

### Changing `at` later does not move existing keyframes

Keyframes are named for the instant they were promoted *for*, so raising or lowering `at` does not
rename what is already on disk. Days still inside `image_retention_days` will pick up a **second**
keyframe at the new time, so those days appear twice in the next render. Set it once, before the
archive matters.

## `[image_capture_library]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `root` | yes | — | Where images and videos are written. `~` is expanded. |
| `image_retention_days` | no | `8` | Delete stills older than this. `0` means keep forever. |
| `keyframe_retention_days` | no | `0` | Delete promoted keyframes older than this. `0` — keep forever — is the right answer; see below. |
| `timelapse_retention_days` | no | — | Baseline video retention for every cadence. `0` means keep forever. |
| `timelapse_retention_days.<cadence>` | no | `hourly` 7, `daily` 90, `weekly` 0, `monthly` 0, `progress` 0 | Per-cadence override. Wins over the baseline. |
| `min_free_disk_gb` | no | `5` | Hard floor on free space. Below it, files are deleted past retention until it is met. `0` disables. |

Timelapse footage compresses badly — consecutive frames are minutes apart, so a full 1,800-frame
60-second video runs around 140 MB. That makes *daily* the expensive cadence and makes a single
retention for all three wrong: keep everything and the disk fills, expire everything and the
archive goes with it. Expire hourly after a week, bound daily, keep weekly forever. See
[Storage Planning](Storage-Planning.md).

### The free-space floor

Retention bounds how **old** files get, not how many **bytes** they occupy, so it cannot on its own
promise the disk stays writable: add two cameras, point them at a more detailed scene, or let a
video archive grow for a year and the steady state moves without any setting changing.

`min_free_disk_gb` is the backstop. Every capture cycle checks free space — a `statvfs`, so it costs
nothing — and when it falls below the floor the daemon deletes past retention until the floor is met
again. It sacrifices in order of what cannot be recovered:

1. **Stills past every render window** — free to drop; every render that could have used them has run
2. **Hourly videos** — the most disposable history
3. **Stills an upcoming render needs** — degrades a future video rather than destroying a finished one
4. **Daily videos**
5. **Progress videos** — re-renderable from keyframes, and superseded on the next rollover anyway
6. **Monthly videos** — also re-renderable from keyframes
7. **Weekly videos** — taken only when nothing else is left: its stills were pruned months ago, so
   once it is gone no amount of CPU brings it back

**Keyframes are never taken.** They are the one thing in the library that cannot be rebuilt — a
pruned still cannot be re-promoted — and at ~500 MB a year against a steady state near 100 GB,
sacrificing them buys under half a percent of the disk while destroying the beginning of the record,
which is the part worth having.

Each channel worker checks, but the deleting is serialised behind a lock file so six workers cannot
race each other into over-deleting. When the floor still cannot be met after exhausting everything,
it logs an error saying so — that means the configuration genuinely does not fit the disk.

One subtlety worth knowing if you read the code: a still that has been promoted is a hardlink, so
unlinking it frees **nothing** — the inode survives under the keyframe's name. `reclaim` counts
`st_nlink` and credits itself zero bytes for those, or it would stop short of a floor it had just
claimed to reach.

**`image_retention_days` must be strictly greater than your longest *still-sourced* cadence
window.** With `weekly` enabled, that means at least 8. `monthly` and `progress` do not read the
still track, so they do not constrain it — which is the entire reason the keyframe track exists.

If it isn't, pruning deletes the stills before the weekly render can read them and you get no weekly
video, silently. Timelapsed checks this at startup and logs:

```
Configuration problem: image_retention_days (7) is not greater than the weekly cadence window
(7 days). Images will be pruned before the weekly render can use them. Set image_retention_days
to at least 8.
```

It is a warning, not a fatal error — the daemon still starts and the other cadences still work.

Videos are tiny compared to stills (a 60-second 1080p timelapse is a few MB), so `0` — keep them
forever — is the sensible default. Stills are the thing that will fill your disk.

### Leave `timelapse_retention_days.progress` at 0

The progress video's start is day one of the project and never moves, so an age-based retention
deletes the **current** video as soon as that start ages out, and keeps nothing. Each render
supersedes the last instead — there is always exactly one file, and it is always current. Timelapsed
warns at startup if you configure a retention for it anyway.

### `keyframe_retention_days` should stay at 0 too

Six channels at 231 KB a day is about **500 MB a year**, and for the first eight days of its life a
keyframe costs literally nothing — it is a second name on a still that already exists. Against that,
they are the only unrecoverable thing in the library. Anything shorter than 32 days also stops a
monthly render working at all, which Timelapsed warns about at startup.

## `[web]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `host` | no | `0.0.0.0` | Bind address for the viewer. |
| `port` | no | `8080` | Bind port. |

The viewer has **no authentication**. Bind it to `0.0.0.0` only when Tailscale is providing the
access control, or bind to `127.0.0.1` and put a proxy in front. Never port-forward it from your
router. See [Viewing Timelapses](Viewing-Timelapses.md).

If you ran `install.sh --with-nginx`, these two are managed for you. nginx takes over whichever port
was configured here — `8080` unless you changed it — and the setup script rewrites this section to
`127.0.0.1` one port up. Change either with `LISTEN_PORT=… UPSTREAM_PORT=… sudo deploy/nginx-setup.sh`
rather than editing here: the next upgrade re-renders the nginx side, and the two would disagree.

## `[analysis]`

Recognition: people, vehicles and plates found in the stills capture already wrote. Off by
default. See [Recognition](Recognition.md) for what it does, and
[Recognition Feasibility](Recognition-Feasibility.md) for what these cameras can actually support.

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | no | `false` | Whether to run recognition at all. |
| `root` | no | `{library root}/index` | Where the index, crops and models live. |
| `model_root` | no | `{root}/models` | Where the ONNX models live. |
| `score_threshold` | no | `0.5` | Minimum detector confidence. |
| `threads` | no | `2` | ONNX Runtime threads per model. |
| `batch_size` | no | `200` | Frames per channel per pass. |
| `detection_retention_days` | no | `30` | How long to keep per-frame detection rows. `0` keeps them forever. |
| `event_retention_days` | no | `365` | How long to keep events, crops and plates. `0` keeps them forever. |
| `reid_enabled` | no | `true` | Group repeat sightings of a person by appearance. |
| `reid_threshold` | no | `0.8` | Similarity required for an arriving sighting to join an existing group. |
| `reid_merge_threshold` | no | `0.75` | Similarity required to fold two groups together afterwards. |
| `reid_window_hours` | no | `12` | How far back to look for a match. |
| `plate_channels` | no | *(empty)* | Channels to read plates on. Empty disables plate reading. |
| `plate_confidence` | no | `0.7` | Minimum OCR confidence per read. |
| `ignore_vehicles_on` | no | *(empty)* | Channels where vehicle detections are discarded outright. For indoor cameras. |

### Do not lower `score_threshold`

This is the setting most likely to be "tuned" into uselessness. The usual detector default is
0.25, and at 0.35 on this footage the results were nonsense: a neighbouring building seen over a
wall scored as a vehicle on **70% of night frames**, and a pile of tools on a workshop floor
scored as a car on **57%** of frames from an indoor camera. Both vanished completely at 0.5 —
no masking, no exclusion zones, no static-background subtraction.

Lowering it does not buy you extra detail. It buys phantom events that never end.

### `plate_channels` is a whitelist for a reason

Plate OCR needs roughly 50 px of plate width. Measured across six cameras here, exactly one
channel clears it: 52–65 px, reading cleanly. The others sit near 40 px and return garbage.
Enabling every channel does not find more plates, it just spends CPU producing reads that the
confidence and format guards then throw away.

### `ignore_vehicles_on` exists for indoor cameras

On an indoor camera, nothing that appears can be a vehicle -- but static clutter can look like
one to the detector. Measured here on a workshop channel: a pile of crates and metal tubes
scored 0.50-0.58, straddling `score_threshold` frame to frame, and every flicker across the
line opened another one-frame vehicle event -- hundreds a day, drowning the channel's timeline.
Raising `score_threshold` would have cost real people and vehicles on the outdoor cameras, so
the kind is dropped per channel instead. Person detection on the listed channels is unaffected.

### `reid_threshold` trades recall for precision

Grouping is by body appearance, **not** by face — faces on these cameras are ~38 px, well under
what any embedding needs. Measured on 423 real body crops:

| Threshold | Same-person pairs matched | Different people wrongly merged |
| --- | --- | --- |
| `0.7` | 50% | 14.6% |
| `0.8` | 27% | 1.2% |

`reid_threshold` governs matching as sightings arrive, and is kept strict because a wrong group
created online is awkward to undo. On its own it fragments one person into many: bending over,
turning away and being half-occluded all fail to match a frontal view, and a single day produced
**156 groups for two people**.

`reid_merge_threshold` is the pass that repairs that, linking groups that share a similar enough
pair of crops and taking the transitive closure. On this deployment's footage:

| `reid_merge_threshold` | Groups | Largest |
| --- | --- | --- |
| 0.85 | 147 | 106 |
| 0.80 | 122 | 149 |
| `0.75` | 72 | 202 |
| 0.70 | 47 | 271 |

0.75 is the default because it is the last value that still separates the two people actually on
camera — red shirt with dark trousers, red shirt with blue jeans. At 0.70 they become one group.
Lower it if you would rather have fewer, broader groups; raise it if two people are being merged.

## `[archive]`

The full-segment replica of what the NVR itself records, kept by the `timelapsed-archiver`
daemon. Background and measurements are in [NVR-Roadmap](NVR-Roadmap.md).

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `root` | no | empty | Where the replica lives. **Empty disables the archiver entirely.** |
| `retention_days` | no | `0` | Days of footage to keep; `0` keeps everything until the floor bites. |
| `min_free_disk_gb` | no | `50` | Free space on the archive volume below which the oldest whole days are dropped. |

The archiver downloads every recorded segment the footage mirror lists — sequentially, oldest
first, because the NVR wraps its quota by deleting oldest — remuxes each to MP4 and files it
under `{root}/{channel}/{YYYYMMDD}/{start}_{end}_{device-name}.mp4`. There is no database of
what has been archived: the filenames are the index, exactly as they are for stills. It
requires `[analysis]` to be enabled, because the analyzer daemon maintains the footage mirror
it reads; segments older than `retention_days` are never fetched in the first place, so a deep
device history does not turn into a fetch/delete loop.

Point `root` at a volume sized for the job before enabling it — this deployment's NVR records
roughly 40 GB/day. The floor is generous for the same reason: a day of footage has to be able
to arrive between reclaim passes without punching through it.

## `[general]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `logging_level` | no | `INFO` | Standard Python level name. |

At `INFO` you get startup, renders, pruning, and failures. At `DEBUG` you also get a line per
captured frame, which at a 10-second interval across 3 channels is around 26,000 lines a day —
useful for a few minutes of diagnosis, not for a running service.

## A worked example

Three cameras, footage kept just long enough to feed a weekly video, viewer behind Tailscale:

```ini
[nvr]
url = http://192.168.1.10
username = timelapse
password = ...
channels = 1,2,3

[capture]
interval_seconds = 10
resolution.width = 1920
resolution.height = 1080

[timelapse]
duration_seconds = 60
output_fps = 30
min_frames = 60
cadences = hourly,daily,weekly
timezone = UTC
max_concurrent_renders = 1

[image_capture_library]
root = /var/lib/timelapsed
image_retention_days = 8
timelapse_retention_days.hourly = 7
timelapse_retention_days.daily = 90
timelapse_retention_days.weekly = 0

[web]
host = 0.0.0.0
port = 8080

[general]
logging_level = INFO
```

That produces, per channel: a 12-second hourly video, a 60-second daily, a 60-second weekly, and
roughly 59 GB of stills on disk at steady state.
