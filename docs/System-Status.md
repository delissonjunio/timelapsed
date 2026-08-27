# System Status

The viewer answers "what did the cameras see". The status page answers the other question: **is the
thing that records what they saw still working.**

It lives at **`/status`**, alongside the timeline and the people-and-plates page, and is linked from
both. Unlike `/library` it does not need recognition — a disk filling up and a camera going quiet
are the viewer's problems whether or not anything is analysing the frames.

```
/                 the timeline           what happened
/library          people and plates      who was here            (needs recognition)
/status           system status          is it still working
```

Everything on the page comes from one request to `/api/system`, so anything the page shows can be
scripted against.

## What it tells you

### The headline row

Six numbers, and if all six look right nothing below them needs reading.

| Tile | What it means | When it turns |
| --- | --- | --- |
| **Disk free** | Free bytes on the filesystem holding the library, with the used fraction | Amber over 90% used, red once free space is below `min_free_disk_gb` |
| **Library** | Total bytes the library holds, with the still and clip counts | — |
| **Cameras live** | How many configured channels wrote a frame recently | Amber if any camera is late, red if none are writing |
| **Analysis lag** | The worst per-camera lag, and the total frames still to analyse | Amber past six hours, red if any channel is losing ground |
| **Growth** | Bytes written in the last 24 hours, and where retention parks the library | Amber if the steady state does not fit the disk |
| **Renders due** | Closed periods inside retention with no video on disk | Amber past six |

### Checks

Everything wrong, worst first, in a sentence each. On a healthy server the list is empty, which is
itself worth being able to see at a glance. The configuration half of it is `validate_config` — the
same warnings the daemon prints at startup, which are easy to miss in a journal and impossible to
miss here.

### Storage

One bar for the whole library, split into stills, keyframes, videos, recognition crops and the
recognition index, then a table of the same per camera.

Keyframes are hardlinks to stills, so a keyframe whose still is still on disk costs no bytes of its
own — it is counted under stills, and its own figure reads 0 B until retention starts unlinking the
originals. Nothing is counted twice in either direction.

### Capture

Per camera: how long since the last frame, and the **yield** — frames on disk against frames the
interval should have produced, over the last hour and the last day. Yield is measured against how
long the camera has actually been running, so a channel added this morning is not reported as having
missed yesterday.

A camera is `live` while it is writing, `stale` once it has written nothing for six capture
intervals (at least two minutes), `silent` if it is configured but has never written anything, and
`retired` if it holds frames but is no longer in `channels` — nothing prunes a channel the daemon no
longer captures, so that disk stays yours forever until you delete it.

### Analysis

The part that answers "how far behind is recognition". Per camera:

* **Analysed through** — the watermark, which is where a restart would pick up.
* **Lag** — how far the watermark is behind the *newest still on disk*, not behind the clock.
  Analysis can never be nearer than one capture interval to now, so measuring against the clock
  would report a permanent lag that is not one.
* **Backlog** — the exact number of stills past the watermark, counted from the directory rather
  than divided out of the lag.
* **Rate** — seconds of footage analysed per second of wall clock, sampled while the page is open.
  `1.00×` is exactly keeping pace with the cameras; anything less than that while behind never
  catches up, because the frontier is also moving.
* **Catches up** — the lag divided by whatever the rate has above `1.00×`. Blank when it never will.

The rate needs about a minute of the page being open before it can be quoted; until then it reads
`measuring`. It is held in the viewer's memory and is a measurement rather than a record, so it
resets when the viewer restarts.

A channel is `current` when the lag is within three capture intervals, `behind` while it is closing
the gap, `losing` while the rate is below real time, and `stopped` when the watermark has not moved
at all — which usually means `timelapsed-analyzer` is not running rather than that it is slow.

Below the table: what the index is carrying. `detection` outnumbers `event` by roughly three orders
of magnitude and is what decides how big the file gets, so an over-generous
`detection_retention_days` shows up in that ratio.

### Renders

A camera-by-cadence matrix: how many clips each holds, how long ago the newest was written, and how
many closed periods inside retention have no video.

That count is derived from the videos on disk, **not** from the renderer's own queue — working out
the queue exactly would mean parsing every frame timestamp in the library, which is the one scan
this page exists to avoid. A period the renderer skipped for holding too few frames is therefore
reported as due, because on disk it is. Periods from before the camera captured anything are never
counted.

### Retention, host and configuration

How full each retention window is, what the host is doing (uptime, load, memory, and `systemctl`'s
view of the three units where systemd is present), and every setting in force including the cadence
table.

## The API

```bash
curl -s http://localhost:8080/api/system | jq .
```

One JSON object with `disk`, `storage`, `capture`, `renders`, `analysis`, `retention`, `growth`,
`host`, `services`, `config` and `checks`. `?refresh=1` bypasses the cache.

Useful one-liners:

```bash
# The worst analysis lag, in seconds.
curl -s localhost:8080/api/system | jq '.analysis.worst_lag_seconds'

# Anything wrong at all, as text.
curl -s localhost:8080/api/system | jq -r '.checks[] | "\(.level): \(.title)"'

# Free bytes against the configured floor.
curl -s localhost:8080/api/system | jq '.disk | {free_bytes, minimum_free_bytes, floor_met}'
```

Because `checks` is empty on a healthy server, that second command is a usable monitoring probe on
its own — see [Operations](Operations.md#monitoring).

## What it costs

Building the report scans every frame directory in the library. At a five second interval over eight
days of retention that is around 138,000 files per channel, so the scan avoids parsing timestamps
entirely: frame filenames are `%Y%m%d_%H%M%S_%Z`, which is fixed-width and zero-padded, so
lexicographic order *is* chronological order and "how many frames since 14:00" is a string
comparison. Only the first and last name in each directory is ever parsed into a date.

The report is then cached for twelve seconds, and the page polls every twenty, so an open page costs
one scan per refresh rather than one per request. The Refresh button is the only thing that skips
the cache. `collected_in_ms` in the payload — shown in the header — is how long the scan actually
took; a few tens of milliseconds is normal, and the tail is stat syscalls, so it scales with file
count rather than with bytes.
