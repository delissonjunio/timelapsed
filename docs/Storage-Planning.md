# Storage Planning

Picking `interval_seconds` is the single most consequential decision you will make. It does **not**
control how long your videos are — `duration_seconds` does that, and surplus frames get sampled out.
What the interval actually controls is:

* how much disk you burn, linearly
* how smooth the **hourly** video is

## The tension

A 60-second video at 30 fps needs **1,800 frames**. How long it takes to collect 1,800 frames
depends on the cadence window:

| Cadence | Window | Interval needed to fill 60s of video |
| --- | --- | --- |
| Hourly | 3,600s | **2 seconds** |
| Daily | 86,400s | 48 seconds |
| Weekly | 604,800s | 336 seconds (5.6 minutes) |

So daily and weekly videos are full-length at almost any sane interval. **Only the hourly video is
starved**, and getting it to a full 60 seconds costs you a 2-second interval — which is where the
disk usage becomes painful.

## The numbers

Assuming ~300 KB per 1080p JPEG (measure yours; 200–400 KB is the usual range):

| Interval | Images/day/channel | Disk/day/channel | 3 channels, 8 days | Hourly video length |
| --- | --- | --- | --- | --- |
| 2s | 43,200 | 12.4 GB | **297 GB** | 60s (full) |
| 5s | 17,280 | 4.9 GB | **119 GB** | 24s |
| **10s** | 8,640 | 2.5 GB | **59 GB** | 12s |
| 15s | 5,760 | 1.6 GB | **40 GB** | 8s |
| 30s | 2,880 | 0.8 GB | **20 GB** | 4s |
| 60s | 1,440 | 0.4 GB | **10 GB** | 2s |

Daily and weekly videos are the full 60 seconds at every row in this table.

**Recommended starting point: 10 seconds.** A 12-second hourly clip is genuinely watchable, daily
and weekly are full length, and 59 GB is a comfortable disk for three cameras.

Measure your actual average with:

```bash
find /var/lib/timelapsed -name '*.jpg' -printf '%s\n' | awk '{t+=$1; n++} END {print t/n/1024 " KB avg over " n " files"}'
```

## Sizing the disk

```
steady state ≈ channels × (86400 ÷ interval) × avg_image_size × image_retention_days
```

Then add headroom for:

* **Rendered videos** — small, but they accumulate forever at the default `timelapse_retention_days = 0`.
  A 60-second 1080p timelapse at CRF 23 is roughly 5–15 MB. Three channels producing hourly, daily
  and weekly videos is about 28 videos a day, so **on the order of 100 GB per year**. Set
  `timelapse_retention_days = 365` if that matters.
* **Render scratch space** — renders hardlink rather than copy where the filesystem allows it, so
  staging is usually free. Across a filesystem boundary it falls back to real copies, needing up to
  `target_frames × avg_image_size` (~500 MB) temporarily. `PrivateTmp=true` in the unit puts this
  under `/tmp` on the root filesystem, so leave a couple of GB free there.
* **Filesystem overhead** — millions of small files. ext4's default inode ratio handles this, but
  check `df -i` as well as `df -h`; running out of inodes looks exactly like a full disk.

For three cameras at a 10-second interval with 8-day retention, a **100 GB** disk is comfortable.
At the 5-second interval, budget **200 GB**.

## Retention and cadences interact

`image_retention_days` **must be greater than your longest cadence window**:

| Cadences enabled | Minimum `image_retention_days` |
| --- | --- |
| `hourly` | 1 |
| `hourly,daily` | 2 |
| `hourly,daily,weekly` | **8** |

Set it lower and pruning deletes the stills before the render reads them. Timelapsed warns at
startup, but the render still silently produces nothing. This is the most common misconfiguration.

## Keeping years of footage without keeping years of stills

The stills are the expensive part; the videos are not. The intended pattern is:

* `image_retention_days = 8` — just enough to feed a weekly render
* `timelapse_retention_days = 0` — keep every video forever

You end up with a permanent hourly, daily and weekly record at a fraction of a percent of the raw
storage. If you want the raw stills archived too, sync them off the box before pruning catches
them — see [Operations](Operations).

## Watching it in practice

```bash
du -sh /var/lib/timelapsed/*/image        # stills per channel
du -sh /var/lib/timelapsed/*/timelapse    # videos per channel
df -h /var/lib/timelapsed
df -i /var/lib/timelapsed                 # inodes: check this too
```

Steady state is reached after `image_retention_days` days. Growth that continues past that point
means pruning is not running — check the logs for permission errors on delete.
