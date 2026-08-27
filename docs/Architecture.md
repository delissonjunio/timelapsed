# Architecture

## Process model

```
main process  (timelapsed.timelapsed:run)
  │  reads config, validates it, warns about anything unworkable
  │  starts one worker per channel, staggered by up to 1.4s
  │
  ├── capture-1  ──┐
  ├── capture-2    │  each: snapshot ▸ store ▸ maybe render ▸ maybe prune ▸ sleep
  └── capture-3  ──┘
        │
        └── render-weekly-1   (short-lived, one per cadence per channel)
            render-daily-1
            render-hourly-1
            render-monthly-1
            render-progress-1

timelapsed-analyzer  (timelapsed.analyzer:run)      -- optional, off by default
  │  reads the stills the capture workers wrote, from a per-channel watermark
  └── detects people and vehicles, groups them into events, reads plates

timelapsed-web       (timelapsed.web:run)
  └── serves the viewer, the timelapse catalogue and the recognition index
```

Recognition runs in its own process rather than inside the capture loop. That loop sleeps
`interval - elapsed` and already warns when a cycle eats 80% of the interval, so inference inside
it would come straight out of the capture budget. Separate also means its own `CPUQuota` and
`MemoryMax`, and it can be stopped or backfilled without interrupting capture. See
[Recognition](Recognition.md).

**One OS process per channel, not a worker pool.** Capture workers never return, so a pool sized
below the channel count would start the first N channels and silently never start the rest. A
process per channel makes the count self-evident and lets each channel fail independently.

**Renders get their own process.** Rendering a week of footage takes ffmpeg tens of seconds to
minutes. Doing that inline would stall that channel's capture for the whole run. Each render is
forked off and the capture loop carries on. If a render is still running when the next one of the
same cadence comes due, the new one is **skipped, not queued** — on a small VM, falling one period
behind is much better than accumulating ffmpeg processes until the box dies.

**Shutdown is cooperative.** `SIGTERM` and `SIGINT` set a flag; the worker finishes its current
cycle, waits up to 30s for in-flight renders, then exits. systemd's default `TimeoutStopSec` of 90s
accommodates this.

## Storage layout

```
{image_capture_library_root}/
  1/                                          ← channel id, as written in config
    image/
      20250601_120000_UTC.jpg
      20250601_120010_UTC.jpg
    keyframe/                                 ← one per local day, hardlinked from image/
      20250531_150000_UTC.jpg
      20250601_150000_UTC.jpg
    timelapse/
      hourly_20250601_110000_UTC-20250601_120000_UTC.mp4
      daily_20250531_120000_UTC-20250601_120000_UTC.mp4
      weekly_20250525_120000_UTC-20250601_120000_UTC.mp4
      monthly_20250501_030000_UTC-20250601_030000_UTC.mp4
      progress_20250310_150000_UTC-20250601_030000_UTC.mp4
  2/
    ...
  index/                                      ← recognition only; absent unless enabled
    index.sqlite3
    crops/event/20250601/1234.jpg
    crops/plate/20250601/56.jpg
    models/
```

**The filename is the index.** Timestamps are fixed-width and always UTC, so sorting filenames
lexicographically sorts them chronologically. Range queries parse stems and filter; point lookups
use `bisect`. There is no database to corrupt, migrate, or back up separately, and you can answer
"what did the camera see at 3pm" with `ls`.

Everything is UTC on disk. Local-time filenames break ordering twice a year at the DST boundary.

The one exception to "no database" is `index/`, which recognition writes. It answers questions a
directory listing cannot -- "every time this person appeared on this channel" -- so it keeps a
real index. It sits outside the per-channel trees so the library's pruning never walks it, and it
carries its own retention: `reclaim` measures free space across the whole filesystem, so crops
left to grow unbounded would push it under the floor and make it delete stills instead. Delete
the whole directory and everything else still works.

## The capture cycle

Per channel, every `interval_seconds`:

1. `GET {url}/ISAPI/Streaming/channels/{channel}01/picture?videoResolutionWidth=W&videoResolutionHeight=H`
   with HTTP Digest auth, a `(5s connect, 20s read)` timeout, and up to 4 attempts with exponential
   backoff and full jitter.
2. Verify the response `Content-Type` is actually an image. NVRs answer some failures with `200 OK`
   and an XML error body; without this check that XML gets written to disk as a "frame" and poisons
   the next render.
3. Write to `{root}/{channel}/image/{timestamp}.jpg`.
4. For each enabled cadence, ask whether the clock has rolled over into a new period. If so, work
   out which of that cadence's windows are still missing a video and fork one render process for
   them.
5. Once an hour, prune stills and videos past their retention.
6. Sleep for whatever remains of the interval. If the cycle itself ate more than 80% of the
   interval, log a warning — that's the signal your interval is too aggressive for the hardware.

A failure anywhere in steps 1–5 is logged and swallowed. A single unreachable camera, a full disk
during one write, or one broken render must not take down the daemon.

## Cadence rollover

Cadences are not timers; they are **clock rollovers**. Each worker remembers when it last ran a
cadence and compares calendar fields:

| Cadence | Fires when | Window | Reads |
| --- | --- | --- | --- |
| `hourly` | `(date, hour)` differs from the last run | 1 hour | stills |
| `daily` | `date` differs | 24 hours | stills |
| `weekly` | ISO `(year, week)` differs — so, Monday | 7 days | stills |
| `monthly` | `(year, month)` differs — so, the 1st | one calendar month | keyframes |
| `progress` | `(year, month)` differs | everything so far | keyframes |

`window` on the last two is **nominal** — 31 days, the longest a month can be. It is read only to
order the cadences and to bound backfill; the arithmetic goes through `Cadence.previous_start` and
`Cadence.end_of`, which know that February is 28 days (29 in a leap year). `timedelta(days=31)` as
actual arithmetic would drift off the calendar within two months and skip February entirely.

Using ISO week numbers rather than "7 days since last time" means the weekly video always covers a
Monday-to-Monday week, and the year boundary is handled correctly (29 Dec 2025 and 1 Jan 2026 are
both ISO week 1 of 2026, and correctly do *not* trigger a rollover).

On startup, every cadence is seeded with the current time. A restart therefore does not immediately
re-render everything; each cadence waits for its next genuine rollover.

## What gets rendered: missing windows, not just the last one

A rollover is the *trigger*; it is not the answer to "what should be rendered". That question is
answered by comparing what is on disk against the clock:

* Windows are **aligned to the clock** — the top of the hour, midnight, Monday, the 1st — so a
  period has one canonical name no matter what second the render actually fired on.
* A period is **already done** if a video of that cadence exists whose start falls inside it.
* A period is **renderable** if it holds at least that cadence's `min_frames`.
* Everything else, back as far as the cadence's own source track still holds frames, is **missing** —
  and missing windows are submitted newest first, at most `MAX_WINDOWS_PER_RENDER` per pass.

The window that just closed is simply the newest missing one, so the common case is unchanged. What
this buys is that every other way a window can go missing now heals itself: a render killed by the
OOM killer, a `systemctl restart` landing mid-ffmpeg (renders are children of the unit, so they die
with it), or a render skipped because the previous one of that cadence was still going. Each worker
also runs one sweep at startup rather than waiting for the next rollover to notice.

### The exception: an anchored render

`progress` breaks the second rule, and has to. Its window starts at the oldest keyframe there is and
that start never moves, so "does a stored video start inside this period" is true forever after the
first render and the video would never be refreshed again.

So an anchored cadence is judged on its **end** instead: the render is outstanding while nothing
already stored reaches as far forward as the period that just closed. Each new render therefore
covers everything the previous one did and more, which is also why age-based retention is wrong for
it — pruning on a start that is day one of the project deletes the current video. `prune_superseded`
drops the previous file after each successful render instead, so there is exactly one, and it is
always current.

## The keyframe track

The month-long renders cannot read the stills. `image_retention_days` is 8, and raising it to 32 to
feed a monthly video would mean ~380 GB of stills for six channels on a 200 GB disk.

So one still per channel per local day is **promoted**: hardlinked from `image/` into `keyframe/`
before retention can reach it. Because it is a hardlink on the same filesystem, promotion costs one
inode and no bytes, and when retention unlinks the still eight days later the keyframe is simply the
other name the inode still has. Six channels at 231 KB a day is about **500 MB a year**.

Three details carry the design:

* **Local noon, not a fixed interval.** A construction timelapse lives or dies on a constant sun
  angle, so the frame is anchored to a wall-clock time (`[keyframe] at`, on the `[timelapse]`
  timezone) and the nearest stored still within `tolerance_minutes` is the one taken. No still close
  enough — the camera was down over noon — and that day is simply absent.
* **Named for the instant it was promoted for**, not for the still's real capture time. That makes
  promotion idempotent by filename and puts frames exactly 24 hours apart, so the video reads as one
  frame a day rather than as jitter. The cost is that the name can be up to `tolerance_minutes` off
  the true capture time.
* **Missing-work-driven, like the renders.** `pending_keyframes` compares the local days in the
  retention window against what is on disk, so a promotion lost to a crash or a restart heals on the
  next pass. It runs hourly and once at startup, before the prune — a still must never be pruned in
  the same pass it was due to be promoted in.

The bound is honest and short: promotion can only reach back as far as the stills survive. **History
before the daemon started running this way is not recoverable from the library**, because the stills
that would have filled it are already gone.

Keyframes are the one artefact here that no amount of CPU can rebuild — a monthly video can always
be re-rendered, a pruned still cannot be re-promoted — so they are the only thing the free-space
reclaim will never touch. At half a percent of the disk, sacrificing them could not save it anyway.

## Rendering

`generate_timelapse` does four things:

1. **Select the window.** All frames with a timestamp in `[start, end]`, oldest first, from the
   cadence's own track — the stills, or the keyframes.
2. **Sample down to the target frame count.** `output_fps × duration_seconds` frames, picked at
   even intervals across the whole list. This is what makes the output length predictable: a
   60-second video at 30 fps is always 1,800 frames whether the window held 720 stills or 120,000.
   If fewer stills exist than the target, all of them are used and the video is simply shorter.
3. **Stage them** as `input-%015d.<ext>`, which is the sequence pattern ffmpeg's image2 demuxer wants
   — one extension for all of them, taken from the first frame, because a channel answering PNG
   would otherwise stage names the pattern cannot match —
   in a scratch directory **inside the library** (`{root}/.render`) rather than `/tmp`. Same
   filesystem means `os.link` works, so staging 1,800 frames copies no bytes at all; `/tmp` is a
   different mount even when it is the same disk, and hardlinks do not cross mounts. Leftovers from
   a killed render are cleared at startup.
4. **Run ffmpeg**: `libx264`, `-preset veryfast`, `-crf 23`, `-pix_fmt yuv420p` for universal
   playback, and `-movflags +faststart` so the viewer can begin playing before the file finishes
   downloading. Keyframe-sourced renders also get `-vf deflicker`, and are padded up to 24 fps on
   the way out because a 6 fps container makes some players stutter.

Below `min_frames` the render is skipped with a warning rather than producing a video that flashes
past in a third of a second.

**Renders are serialised across the whole daemon.** Every channel rolls over on the same tick, and
each 1080p ffmpeg peaks around 250 MB, so six of them at once is 1.5 GB — more than a 2 GB guest
has. A `multiprocessing.BoundedSemaphore(max_concurrent_renders)`, held around each individual
ffmpeg run rather than around a whole batch, is what keeps that to one at a time while still letting
a channel with a backlog take its turn. The unit's `MemoryMax` is the backstop underneath it.

## The web viewer

A separate, optional process (`timelapsed.web`) built entirely on the standard library. It scans
the timelapse directories on each request — the file count is small and a stale listing is more
annoying than a directory scan is expensive — and serves an index page with inline `<video>`
elements, plus a small JSON API at `/api/timelapses`.

It implements **HTTP Range requests**, which is not optional: Safari refuses to play video at all
from a server that doesn't advertise `Accept-Ranges`, and without them scrubbing doesn't work.

Video paths are resolved and then checked to still be inside the library root, so `..` traversal
returns 404.

### Optionally, nginx takes the bytes

`install.sh --with-nginx` moves the viewer to `127.0.0.1:8081` and gives nginx the public port.
nginx then serves `/video/{channel}/{file}.mp4` straight off the disk and proxies everything else —
the page, the JSON APIs, the ffmpeg-scaled thumbnails, the detection crops — back to Python.

The split is drawn at "is this file already on disk". Renders are; nothing else is.

The win is not throughput: over Tailscale the WireGuard tunnel is the ceiling long before Python's
256 KB copy loop is. It is that the viewer sends no validators, so every revisit re-downloads a
140 MB file that has not changed and never will, and that reading those 140 MB through userspace
evicts the stills the next ffmpeg wants out of a 2 GB guest's page cache. nginx answers the revisit
with a `304` and reads the file with `directio`, touching neither.

Two paths mean two chances to drift. The location regex in `deploy/nginx-timelapsed.conf` has to
agree with the URLs `TimelapseEntry.as_dict` builds and with the traversal rules
`TimelapseCatalogue.resolve_video` enforces, and nothing at runtime would notice if it stopped —
a mismatch reads as "videos are slow again". `tests/test_nginx_config.py` parses the regex out of
the site file and checks it against both.

**There is no authentication.** The viewer is designed to sit behind Tailscale, where the network
itself is the access control. Do not port-forward it. See [Viewing Timelapses](Viewing-Timelapses.md).
