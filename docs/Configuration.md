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
| `interval_seconds` | yes | — | Seconds between snapshots, per channel. **This is your disk usage dial.** See [Storage Planning](Storage-Planning). |
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

`duration_seconds × output_fps` is the target frame count. The renderer picks that many stills,
evenly spaced across the whole window. If the window holds fewer, all are used and the video comes
out shorter — it is never padded or slowed down.

## `[image_capture_library]`

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `root` | yes | — | Where images and videos are written. `~` is expanded. |
| `image_retention_days` | no | `8` | Delete stills older than this. `0` means keep forever. |
| `timelapse_retention_days` | no | `0` | Delete videos older than this. `0` means keep forever. |

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
router. See [Viewing Timelapses](Viewing-Timelapses).

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

[image_capture_library]
root = /var/lib/timelapsed
image_retention_days = 8
timelapse_retention_days = 0

[web]
host = 0.0.0.0
port = 8080

[general]
logging_level = INFO
```

That produces, per channel: a 12-second hourly video, a 60-second daily, a 60-second weekly, and
roughly 59 GB of stills on disk at steady state.
