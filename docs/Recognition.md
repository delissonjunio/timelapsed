# Recognition

Finds people and vehicles in the stills the capture daemon already wrote, groups
them into events, groups people by appearance, and reads number plates where
they are large enough to read. The viewer gains two activity lanes per camera
and a panel for naming people and searching plates.

It is **off by default**. Turn it on with `enabled = true` under `[analysis]`.

Before enabling it, read [Recognition Feasibility](Recognition-Feasibility.md).
It records what these cameras can and cannot support, measured on real footage,
and it is why this page does not offer face recognition.

## What it does and does not do

| | |
|---|---|
| **People and vehicles on the timeline** | Works well. Two density lanes per camera showing when something was there. |
| **Plates** | Works on cameras where plates land at 50 px or more. One channel here qualifies; others produce nothing usable. |
| **Grouping repeat sightings of a person** | Partial. By clothing and build, within a day. |
| **Face recognition** | **Not available.** Faces here are ~38 px against the ~80 px an embedding needs. Nothing in software fixes that; it needs a camera at face height. |

## Architecture

A fourth daemon alongside capture and the viewer:

```
timelapsed.service           6 capture workers  -> {channel}/image/*.jpg
timelapsed-analyzer.service  reads those stills -> index/index.sqlite3 + index/crops/
timelapsed-web.service       reads both         -> the viewer
```

It is a separate unit rather than part of the capture loop for three reasons.
The capture loop sleeps `interval - elapsed` and already warns when a cycle eats
80% of the interval, so inference inside it would spend the capture budget
directly. It gets its own `CPUQuota` and `MemoryMax`. And it can be stopped,
restarted or backfilled without interrupting capture.

Because it reads stills that already exist, it adds **no NVR load** and no extra
image storage.

### The pipeline

1. **Claim** — walk each channel's `image/` forward from a per-channel
   watermark. The analyzer holds back one capture interval from the newest file:
   `store_image` writes in place with no rename, so the newest still on disk may
   be half-written.
2. **Detect** — YOLOX-tiny over the whole frame, keeping people and vehicles at
   or above `score_threshold`.
3. **Associate** — match each box against the events already open on that
   channel by IoU, tolerating a 60-second gap. **This is the load-bearing step.**
   A car parked for eight hours is one event and ~2,900 detection rows; without
   it the index and the timeline both drown.
4. **Enrich** — embed the largest body crop of each person event; read plates on
   vehicle crops for configured channels.
5. **Settle on close** — when an event stops appearing, its identity is matched
   and its plate is voted. Both wait for the close deliberately: a person walking
   towards the camera gives a better crop every frame, and matching on each one
   would file one visit under several identities.

### Events, not detections

`event` is the durable record and what the timeline draws. `detection` holds the
per-frame boxes behind it — numerous, cheap, and pruned on a shorter retention.
Measured on this deployment: about **45 events a day** across six channels,
against roughly 3,200 detection rows.

## Plates

Plate reads are **voted across an event, never trusted per frame.** At the sizes
these cameras produce, single frames disagree — the same car read as `TZT4E17`,
`TZF4L17`, `TIF4E11` and `TZF4E17` across four consecutive frames. Voting runs
per character position, so it recovers a plate no single frame read correctly.

On a real sighting here that produced 40 reads, 37 agreed, at 0.97 confidence.

Three guards, all of which must pass:

1. OCR confidence at or above `plate_confidence` (default 0.7).
2. The model's own region head says Brazil.
3. The text matches a Brazilian layout — old `ABC1234` or Mercosul `ABC1D23`.

Together these reject the false positives cleanly: a neighbouring building on
one channel reads as garbage at 0.5–0.6 confidence and never gets stored.

## People

Grouping is by **body appearance, not face**. The UI says so, and so should you
when reading it: it answers "the same person, in the same clothes, today", and
nothing more. It does not survive a change of outfit, which is why matching is
capped at `reid_window_hours`.

### Matching happens twice, and the second time is what makes it work

Matching sightings **as they arrive** fragments badly. One person crossing a
yard bends over, turns their back, and is half-hidden by a post; none of those
match the frontal view directly. On a single day of real footage that produced
**156 groups for what was essentially two people**.

So there is a second pass. After each batch the analyzer **consolidates**:
any two groups sharing a similar enough pair of crops are linked, and the
transitive closure is taken. This works because fragments are not islands — a
back view does not match a frontal view, but both match the three-quarter views
in between, so the chain pulls the whole day together.

Measured on this deployment, 156 groups consolidate down to:

| `reid_merge_threshold` | Groups | Largest |
|---|---|---|
| 0.85 | 147 | 106 |
| 0.80 | 122 | 149 |
| **0.75** (default) | **72** | **202** |
| 0.70 | 47 | 271 |
| 0.60 | 20 | 292 |

**0.75 is the default because it is the last value that still tells the two
people actually on camera apart** — one in a red shirt with dark trousers, one
in a red shirt with blue jeans. At 0.70 those two become a single group. That
was checked by looking at the crops, not by picking a number off the table.

Consolidation never discards a name: merging keeps the oldest group, and a name
on either side survives.

## Setup

```bash
# 1. Fetch the models (~155 MB). Safe to re-run.
sudo /opt/timelapsed/deploy/fetch-models.sh

# 2. Enable it
sudoedit /etc/timelapsed.ini      # [analysis] enabled = true

# 3. Start it
sudo systemctl enable --now timelapsed-analyzer
journalctl -fu timelapsed-analyzer
```

`deploy/install.sh` does all three automatically when `[analysis] enabled` is
already true in the config it finds.

The first run backfills every still on disk. At ~70 ms a frame that is roughly
an hour for a full 8-day library of six channels; it logs progress as it drains,
and capture is unaffected throughout.

### Configuration

Every key lives under `[analysis]`. See [Configuration](Configuration.md) for the
full table; the two that matter most:

| Key | Default | Meaning |
|---|---|---|
| `score_threshold` | `0.5` | Minimum detector confidence. **Do not lower this.** At 0.35 a neighbouring building scored as a vehicle on 70% of night frames and a pile of tools as a car indoors. At 0.5 every one of those disappeared. |
| `plate_channels` | *(empty)* | Channels to read plates on. Empty disables plate reading entirely. Only worth setting for cameras where plates actually land at 50 px or more. |

## Storage

Everything recognition writes lives under `{library root}/index/`:

```
/var/lib/timelapsed/index/
  index.sqlite3        the index (WAL; analyzer writes, viewer reads)
  crops/event/{day}/   one representative crop per event
  crops/plate/{day}/   the clearest plate crop per read
  models/              the four ONNX models, ~155 MB
```

This sits **outside** the per-channel `image/` and `timelapse/` trees on purpose,
so the library's own pruning never walks it.

> Crops carry their own retention, and they have to. `reclaim` measures free
> space across the whole filesystem, so crops left to grow unbounded would push
> it under the floor and make it delete **stills** — forever, and for a reason
> that would be very hard to spot.

Measured here: roughly **2.6 MB of crops a day**, because crops are per event
rather than per frame. The index itself is a few MB a month.

## Operations

The `sqlite3` command-line tool is not installed on a stock guest, and the
venv's Python always is, so these go through it. `query` is defined once:

```bash
query() {
  sudo -u timelapsed /opt/timelapsed/.venv/bin/python -c '
import sqlite3, sys
db = sqlite3.connect("file:/var/lib/timelapsed/index/index.sqlite3?mode=ro", uri=True)
for row in db.execute(sys.argv[1]):
    print(*row, sep="\t")
' "$1"
}

# Is it keeping up? Compare against the newest still on disk.
query 'SELECT channel, datetime(analysed_through, "unixepoch") FROM watermark ORDER BY channel'

# What has it found?
query 'SELECT channel, kind, COUNT(*) FROM event GROUP BY channel, kind'

# Plates, most recent first
query 'SELECT datetime(captured_at,"unixepoch"), channel, text, votes FROM plate ORDER BY captured_at DESC LIMIT 20'
```

```bash
# Disk
du -sh /var/lib/timelapsed/index
```

Or without touching the database at all, since the viewer already exposes it:

```bash
curl -s localhost:8080/api/identities
curl -s localhost:8080/api/plates
```

### Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Model not found at ... Run deploy/fetch-models.sh` | Recognition enabled before the models were fetched | Run `sudo /opt/timelapsed/deploy/fetch-models.sh` |
| Analyzer exits immediately, `Analysis is disabled` | `[analysis] enabled` is false | Set it true and restart |
| Detection counts look identical day and night | Threshold lowered below 0.5, picking up scenery | Restore `score_threshold = 0.5` |
| `attempt to write a readonly database` in the viewer | `timelapsed-web.service` lost its `ReadWritePaths=/var/lib/timelapsed/index` | WAL needs to write sidecars even to read; restore the line and `daemon-reload` |
| Watermarks falling behind | Backfill in progress, or the guest is short on CPU | Check `journalctl -u timelapsed-analyzer`; it logs ms/frame per pass |
| One person appears as many unnamed groups | Expected at `reid_threshold = 0.8` | Lower it toward 0.7 for more merging, accepting ~15% wrong merges |

### Turning it off

```bash
sudo systemctl disable --now timelapsed-analyzer
```

The index and crops stay on disk and the viewer keeps serving them. To reclaim
the space, remove `/var/lib/timelapsed/index` as well.

## Privacy

This builds a searchable index of people and vehicles at a private property, and
appearance vectors are personal data under the LGPD. Two things follow.

The viewer has **no authentication** — it relies entirely on Tailscale for
access control, and this feature adds the first write endpoint to it (naming a
person). Do not expose it beyond the tailnet.

Plate text and crops are identifying. They are covered by
`event_retention_days`; set it to something you are comfortable with rather than
leaving the default 365 by inertia.
