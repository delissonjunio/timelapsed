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

## `[nvr]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `url` | yes | — | Base URL of the NVR, e.g. `http://192.168.1.10`. ISAPI paths are appended; a trailing slash is stripped for you. |
| `username` | yes | — | NVR user. A read-only account is enough. |
| `password` | yes | — | Sent using HTTP Digest auth, never logged. |
| `channels` | yes | — | Comma-separated channel numbers, e.g. `1,2,3`. Whitespace is trimmed. Channel `1` maps to ISAPI channel `101`. |

One process is started per channel, so the channel count is also the process count and roughly the
memory multiplier.

## `[capture]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `interval_seconds` | yes | — | Seconds between snapshots, per channel. **This is your disk usage dial.** See [Storage Planning](Storage-Planning.md).

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
5. **Weekly videos** — the archive, taken only when nothing else is left

Each channel worker checks, but the deleting is serialised behind a lock file so six workers cannot
race each other into over-deleting. When the floor still cannot be met after exhausting everything,
it logs an error saying so — that means the configuration genuinely does not fit the disk. |
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
| `cadences` | no | `hourly,daily,weekly` | Which timelapses to produce. Any subset of `hourly`, `daily`, `weekly`. An unknown name is a startup error. |
| `timezone` | no | `UTC` | IANA zone name the cadences roll over on, e.g. `America/Sao_Paulo`. An unknown name is a startup error. |
| `max_concurrent_renders` | no | `1` | How many renders may run at once across **all** channels. |

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

## `[image_capture_library]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `root` | yes | — | Where images and videos are written. `~` is expanded. |
| `image_retention_days` | no | `8` | Delete stills older than this. `0` means keep forever. |
| `timelapse_retention_days` | no | — | Baseline video retention for every cadence. `0` means keep forever. |
| `timelapse_retention_days.<cadence>` | no | `hourly` 7, `daily` 90, `weekly` 0 | Per-cadence override. Wins over the baseline. |
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
5. **Weekly videos** — the archive, taken only when nothing else is left

Each channel worker checks, but the deleting is serialised behind a lock file so six workers cannot
race each other into over-deleting. When the floor still cannot be met after exhausting everything,
it logs an error saying so — that means the configuration genuinely does not fit the disk.

**`image_retention_days` must be strictly greater than your longest cadence window.** With `weekly`
enabled, that means at least 8. If it isn't, pruning deletes the stills before the weekly render
can read them and you get no weekly video, silently. Timelapsed checks this at startup and logs:

```
Configuration problem: image_retention_days (7) is not greater than the weekly cadence window
(7 days). Images will be pruned before the weekly render can use them. Set image_retention_days
to at least 8.
```

It is a warning, not a fatal error — the daemon still starts and the other cadences still work.

Videos are tiny compared to stills (a 60-second 1080p timelapse is a few MB), so `0` — keep them
forever — is the sensible default. Stills are the thing that will fill your disk.

## `[web]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `host` | no | `0.0.0.0` | Bind address for the viewer. |
| `port` | no | `8080` | Bind port. |

The viewer has **no authentication**. Bind it to `0.0.0.0` only when Tailscale is providing the
access control, or bind to `127.0.0.1` and put a proxy in front. Never port-forward it from your
router. See [Viewing Timelapses](Viewing-Timelapses.md).

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
