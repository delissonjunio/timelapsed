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
    timelapse/
      hourly_20250601_110000_UTC-20250601_120000_UTC.mp4
      daily_20250531_120000_UTC-20250601_120000_UTC.mp4
      weekly_20250525_120000_UTC-20250601_120000_UTC.mp4
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

| Cadence | Fires when | Window |
| --- | --- | --- |
| `hourly` | `(date, hour)` differs from the last run | 1 hour |
| `daily` | `date` differs | 24 hours |
| `weekly` | ISO `(year, week)` differs — so, Monday | 7 days |

Using ISO week numbers rather than "7 days since last time" means the weekly video always covers a
Monday-to-Monday week, and the year boundary is handled correctly (29 Dec 2025 and 1 Jan 2026 are
both ISO week 1 of 2026, and correctly do *not* trigger a rollover).

On startup, every cadence is seeded with the current time. A restart therefore does not immediately
re-render everything; each cadence waits for its next genuine rollover.

## What gets rendered: missing windows, not just the last one

A rollover is the *trigger*; it is not the answer to "what should be rendered". That question is
answered by comparing what is on disk against the clock:

* Windows are **aligned to the clock** — the top of the hour, midnight UTC, Monday — so a period has
  one canonical name no matter what second the render actually fired on.
* A period is **already done** if a video of that cadence exists whose start falls inside it.
* A period is **renderable** if it holds at least `min_frames` stills.
* Everything else, back as far as retention still holds stills, is **missing** — and missing windows
  are submitted newest first, at most `MAX_WINDOWS_PER_RENDER` per pass.

The window that just closed is simply the newest missing one, so the common case is unchanged. What
this buys is that every other way a window can go missing now heals itself: a render killed by the
OOM killer, a `systemctl restart` landing mid-ffmpeg (renders are children of the unit, so they die
with it), or a render skipped because the previous one of that cadence was still going. Each worker
also runs one sweep at startup rather than waiting for the next rollover to notice.

## Rendering

`generate_timelapse` does four things:

1. **Select the window.** All stills with a timestamp in `[start, end]`, oldest first.
2. **Sample down to the target frame count.** `output_fps × duration_seconds` frames, picked at
   even intervals across the whole list. This is what makes the output length predictable: a
   60-second video at 30 fps is always 1,800 frames whether the window held 720 stills or 120,000.
   If fewer stills exist than the target, all of them are used and the video is simply shorter.
3. **Stage them** as `input-%015d.jpg`, which is the sequence pattern ffmpeg's image2 demuxer wants,
   in a scratch directory **inside the library** (`{root}/.render`) rather than `/tmp`. Same
   filesystem means `os.link` works, so staging 1,800 frames copies no bytes at all; `/tmp` is a
   different mount even when it is the same disk, and hardlinks do not cross mounts. Leftovers from
   a killed render are cleared at startup.
4. **Run ffmpeg**: `libx264`, `-preset veryfast`, `-crf 23`, `-pix_fmt yuv420p` for universal
   playback, and `-movflags +faststart` so the viewer can begin playing before the file finishes
   downloading.

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

**There is no authentication.** The viewer is designed to sit behind Tailscale, where the network
itself is the access control. Do not port-forward it. See [Viewing Timelapses](Viewing-Timelapses.md).
