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

Measured on the DS-7616NXI-K1 at `192.168.18.89`, 1080p main streams average **231 KB** per JPEG
across its six live channels (150–300 KB depending on scene detail). Measure your own before
committing to a disk size — see the command below.

Per channel, at 231 KB:

| Interval | Images/day | Disk/day | 8-day retention | Hourly video length |
| --- | --- | --- | --- | --- |
| 2s | 43,200 | 9.5 GB | 76 GB | 60s (full) |
| 5s | 17,280 | 3.8 GB | 30 GB | 24s |
| **10s** | 8,640 | 1.9 GB | **15 GB** | 12s |
| 15s | 5,760 | 1.3 GB | 10 GB | 8s |
| 30s | 2,880 | 0.6 GB | 5 GB | 4s |
| 60s | 1,440 | 0.3 GB | 2.5 GB | 2s |

Multiply the retention column by your channel count. The deployed guest runs **six** channels at
10 seconds, so its steady-state still library is **~96 GB** — which is why its disk is 200 GB and
not the 100 GB that three channels would have needed.

Daily and weekly videos are the full 60 seconds at every row in this table.

**Recommended starting point: 10 seconds.** A 12-second hourly clip is genuinely watchable and
daily and weekly are full length. Beyond about six cameras the stills start to dominate the guest,
and 15 seconds buys back a third of the disk for a barely noticeable loss in the hourly clip.

Measure your actual average with:

```bash
find /var/lib/timelapsed -name '*.jpg' -printf '%s\n' | awk '{t+=$1; n++} END {print t/n/1024 " KB avg over " n " files"}'
```

## Sizing the disk

```
steady state ≈ channels × (86400 ÷ interval) × avg_image_size × image_retention_days
```

Then add headroom for:

* **Rendered videos** — bigger than intuition suggests. Timelapse footage is close to worst case
  for inter-frame compression: consecutive frames are seconds or minutes apart, so almost nothing
  is shared between them. Measured at CRF 23 on this NVR's 1080p stills, encoded output runs about
  **80 KB per frame**, so a full 1,800-frame 60-second video is on the order of **140 MB**, not the
  5–15 MB a normal 60-second clip would be.

  That makes *daily* the expensive cadence, not hourly: six channels producing one 1,800-frame
  daily video each is ~840 MB a day, or **300 GB a year** if kept forever. Hourly clips are shorter
  (a 10-second interval only yields 360 frames an hour) and cost ~2 GB a day across six channels.

  Hence the per-cadence retention: expire hourly after a week, cap daily at a quarter, keep weekly
  forever. Weekly is the archive and costs ~44 GB a year at six channels.

  These are estimates from a short sample. Check the real figures after the first full day:

  ```bash
  sudo du -sh /var/lib/timelapsed/*/timelapse
  ```
* **Render scratch space** — renders stage into `{root}/.render`, which is on the same filesystem as
  the stills, so staging is hardlinks and costs no bytes. What does land there is the video being
  encoded, so budget one render's output (~150 MB) plus a little. The free-space floor
  (`min_free_disk_gb`) already covers it.
* **Filesystem overhead** — millions of small files. ext4's default inode ratio handles this, but
  check `df -i` as well as `df -h`; running out of inodes looks exactly like a full disk.

For three cameras at a 10-second interval with 8-day retention, a **100 GB** disk is comfortable.
For six, budget **200 GB**. LVM-thin disks only consume what is written and resize online, so start
at the figure your math gives and grow it once you have a week of real data:

```bash
sudo -n qm disk resize 302 scsi0 300G          # on the Proxmox host
sudo growpart /dev/sda 1 && sudo resize2fs /dev/sda1   # in the guest
```

## The floor beneath all of this

Every figure above is an estimate, and estimates drift: a camera gets added, a scene gets busier,
an archive grows for a year. `min_free_disk_gb` (default **5**) is what makes that survivable —
below it, the daemon deletes past retention until the floor is met, sacrificing stills no render
still needs first and the weekly archive last. See
[Configuration](Configuration.md) for the full ladder.

Treat the floor as a backstop, not a plan. If it fires regularly, retention is too generous for the
disk: shorten it or grow the disk.

## Retention and cadences interact

`image_retention_days` **must be greater than your longest still-sourced cadence window**:

| Cadences enabled | Minimum `image_retention_days` |
| --- | --- |
| `hourly` | 1 |
| `hourly,daily` | 2 |
| `hourly,daily,weekly` | **8** |
| plus `monthly,progress` | still **8** — they do not read the stills |

Set it lower and pruning deletes the stills before the render reads them. Timelapsed warns at
startup, but the render still silently produces nothing. This is the most common misconfiguration.

The last row is the whole point of the keyframe track. Feeding a monthly render from the stills would
need 32 days of them — **~380 GB for six channels at a 10 second interval**, on a 200 GB disk. So one
still a day is hardlinked out of the capture window into `keyframe/`, and the monthly and progress
renders read that instead.

## The keyframe track costs almost nothing

| Artefact | Year one, six channels | After that |
| --- | --- | --- |
| Keyframes, 231 KB × 365 × 6 | ~506 MB | +506 MB/yr |
| Monthly videos, ~31 frames × 80 KB × 12 × 6 | ~178 MB | +178 MB/yr |
| Progress videos, one per channel, capped at 360 frames | ~173 MB | flat — each render replaces the last |
| **Total** | **~0.86 GB** | **~0.7 GB/yr** |

Under half a percent of a 200 GB disk carrying ~96 GB of stills. Ten years of keyframes is 5 GB and
still the smallest thing on the box. Inodes grow by ~2,190 a year against ~415,000 stills.

And for the first eight days of its life a keyframe costs **zero bytes** — it is a second name on a
still that already exists. Only once retention unlinks the still does the keyframe start occupying
anything of its own.

Two consequences worth knowing:

* **`du` on `keyframe/` overcounts** while the stills are still there. `du` counts an inode once per
  invocation, so `du -sh */keyframe` reports the full size but `du -sh /var/lib/timelapsed` does not
  double-count it.
* **Keyframes are exempt from the free-space floor.** They are the only unrecoverable artefact here,
  and reclaiming them could not save a disk that 500 MB a year is not filling. See
  [Configuration](Configuration.md).

## Keeping years of footage without keeping years of stills

The stills are the expensive part; the videos are not. The intended pattern is:

* `image_retention_days = 8` — just enough to feed a weekly render
* `timelapse_retention_days.hourly = 7` — hourly clips answer "what happened this morning", not "what happened in March"
* `timelapse_retention_days.daily = 90` — a quarter of day-by-day history, the expensive cadence kept bounded
* `timelapse_retention_days.weekly = 0` — the weekly archive, kept forever, at ~44 GB a year for six channels
* `keyframe_retention_days = 0` — the multi-year record, at ~500 MB a year for six channels

You end up with a permanent hourly, daily and weekly record at a fraction of a percent of the raw
storage, and — with `monthly,progress` enabled — a month-by-month and since-day-one record for the
cost of rounding error. If you want the raw stills archived too, sync them off the box before pruning catches
them — see [Operations](Operations.md).

## Watching it in practice

```bash
du -sh /var/lib/timelapsed/*/image        # stills per channel
du -sh /var/lib/timelapsed/*/keyframe     # the daily record (shares inodes with image/ for 8 days)
du -sh /var/lib/timelapsed/*/timelapse    # videos per channel
df -h /var/lib/timelapsed
df -i /var/lib/timelapsed                 # inodes: check this too
```

Steady state is reached after `image_retention_days` days. Growth that continues past that point
means pruning is not running — check the logs for permission errors on delete.

## Recognition, if you enable it

Recognition adds three things under `{library root}/index/`, none of which scale with the capture
interval the way stills do:

| What | Size |
| --- | --- |
| The four ONNX models | ~155 MB, fixed |
| Crops | ~2.6 MB/day measured across six channels |
| The index itself | a few MB a month |

Crops are per **event**, not per frame, which is what keeps this small: a car parked for eight
hours contributes one event and about three crops, not 2,900. Budget a couple of GB a year and
size the disk for the stills as before.

> Crops are **not** covered by `min_free_disk_gb`'s reclaim, which only walks the per-channel
> `image/` and `timelapse/` directories. They have their own retention
> (`event_retention_days`) and they need it: left to grow unbounded they would eat into the free
> space floor and make reclaim delete **stills** to compensate.
