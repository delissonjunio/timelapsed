# Operations

## Logs

Both services log to the journal.

```bash
journalctl -u timelapsed -f                 # follow the capture daemon
journalctl -u timelapsed-web -f             # follow the viewer
journalctl -u timelapsed --since '1 hour ago' -p warning   # warnings and above
```

At `INFO` you get startup, each render starting and finishing, pruning results, and every failure.
`DEBUG` adds a line per captured frame — at a 10-second interval across 3 channels that is roughly
26,000 lines a day, so turn it on for diagnosis and back off again.

### What healthy output looks like

```
Timelapsed starting: 3 channel(s) [1, 2, 3], cadences [hourly, daily, weekly], every 10s
Initialised NVR capture agent for http://192.168.1.10 as user admin
All timelapsed workers started
Started hourly timelapse render for channel 1 (pid 4412)
Rendering hourly timelapse for channel 1: 360 of 360 images at 30 fps (~12.0s of video)
Timelapse stored for channel 1: /var/lib/timelapsed/1/timelapse/hourly_….mp4 (4192841 bytes)
Pruned 8640 image file(s) older than 8 days, 0:00:00 for channel 1
```

## Common failures

### No images are being captured

```
Capture cycle failed for channel 1, continuing
```

Test the endpoint by hand from the guest:

```bash
curl -v --digest -u 'admin:PASSWORD' \
  'http://192.168.1.10/ISAPI/Streaming/channels/101/picture?videoResolutionWidth=1920&videoResolutionHeight=1080' \
  -o /tmp/test.jpg
```

| Symptom | Cause |
| --- | --- |
| `401 Unauthorized` | Wrong credentials, or the NVR account lacks remote-preview permission. |
| `404 Not Found` | That channel does not exist. Channel `1` in config is ISAPI channel `101`. |
| Connection timeout | Firewall, wrong IP, or the NVR only listens on HTTPS. Try `https://`. |
| `200` but tiny XML body | The NVR is reporting an error with a success status. Timelapsed rejects these (`expected an image`) rather than writing them to disk. |

Some Hikvision firmware rate-limits snapshot requests. If captures succeed and then start failing
in bursts, raise `interval_seconds`.

### No timelapses are being produced

Check for the startup warning first:

```bash
journalctl -u timelapsed | grep 'Configuration problem'
```

Then, in order of likelihood:

| Log line | Meaning | Fix |
| --- | --- | --- |
| `Skipping … only N frames available, minimum is 60` | Not enough stills in the window. | Lower `interval_seconds`, or lower `min_frames`. |
| `No image frames found for channel 1 between … skipping weekly render` | Stills were pruned before the render ran. | Raise `image_retention_days` above your longest still-sourced cadence. |
| `No keyframe frames found … skipping monthly render` | The keyframe track is empty or does not reach that far back. | See *No keyframes are being promoted*, below. |
| `Previous weekly render … still running; skipping this one` | Renders take longer than the gap between them. | Harmless on its own: the skipped window is picked up as a missing window later. If it is constant, give the VM more vCPU or reduce `output_fps` / resolution. |
| `Channel 5 is waiting for a render slot` | `max_concurrent_renders` is doing its job. | Nothing, unless the wait outlasts the cadence. |
| Nothing at all | It has not rolled over yet. | Hourly fires on the hour, daily at midnight, weekly on Monday, monthly and progress on the 1st. |

Rollovers are judged on `[timelapse] timezone`, which defaults to **UTC** — so out of the box a
"daily" video is rendered at 00:00 UTC, not at local midnight. Set the zone if you want your own
midnight. Stored filenames stay UTC either way.

### No keyframes are being promoted

Keyframes are the daily frame the `monthly` and `progress` renders read. Nothing promotes them unless
one of those cadences is enabled.

```bash
ls -l /var/lib/timelapsed/1/keyframe/ | tail
journalctl -u timelapsed | grep -i keyframe
stat -c '%h %n' /var/lib/timelapsed/1/keyframe/* | tail   # link count 2 while the still lives
```

| Symptom | Meaning | Fix |
| --- | --- | --- |
| The directory does not exist | No keyframe-sourced cadence is enabled. | Add `monthly` and/or `progress` to `cadences`. |
| `Promoted N keyframe(s)` never appears | As above, or every day already has one. | Nothing, if the file count is growing by one a day per channel. |
| Days missing, camera was fine | No still within `tolerance_minutes` of `[keyframe] at`. | Widen `tolerance_minutes`. Check the camera was recording at that hour. |
| Days missing, camera was down | Expected. That day is simply absent. | Nothing to do — the stills that could have filled it are already pruned. |
| Two frames on the same day | `[keyframe] at` was changed. | Expected for days still inside `image_retention_days`. Delete the unwanted ones by name. |

**Promotion only reaches back as far as the stills survive** — eight days by default. History from
before the daemon started promoting is not in the library and cannot be recovered from it. The NVR's
own recordings may still have it, but getting them out is a separate job.

### There is a gap in the timeline

Missing windows are not permanent. Renders are chosen by comparing what is on disk against the
clock, so any complete window that still has its stills and has no video is re-rendered — newest
first, a few per pass, and once at every worker startup. A gap should close on its own within a few
cadence periods.

If it does not, the stills are the thing to check:

```bash
CH=1; WINDOW=20260826_19       # channel, and the hour in UTC
ls /var/lib/timelapsed/$CH/image | grep -c "^$WINDOW"    # frames still on disk
ls /var/lib/timelapsed/$CH/timelapse | grep "$WINDOW"    # the video, if it exists
```

Fewer frames than `min_frames` — usually because `image_retention_days` has since expired them —
means that window is gone for good and will be skipped deliberately. To force a pass immediately
rather than waiting for the next rollover, `sudo systemctl restart timelapsed`.

Why a window goes missing in the first place: an OOM-killed render, a restart landing mid-ffmpeg
(renders are children of the unit and die with it), or a stretch where the NVR was unreachable and
too few frames were captured. The journal says which:

```bash
journalctl -u timelapsed --since '2026-08-26 19:00' --until '2026-08-26 21:00' | grep -iE 'oom|render|Stopped'
sudo dmesg -T | grep -i 'killed process'
```

### Disk filling up

```bash
df -h /var/lib/timelapsed
df -i /var/lib/timelapsed     # inodes; millions of small files can exhaust these first
du -sh /var/lib/timelapsed/*/image
```

If usage keeps growing past `image_retention_days` days, pruning is not working. Look for:

```
Could not delete /var/lib/timelapsed/1/image/… during pruning
```

That is a permissions problem — the library should be owned by the `timelapsed` user:

```bash
sudo chown -R timelapsed:timelapsed /var/lib/timelapsed
```

Pruning runs once an hour per channel, not on every cycle, so give it an hour before concluding it
is broken.

If free space drops below `min_free_disk_gb` you will see the backstop working:

```
Only 3.9 GB free, below the 5.0 GB floor; reclaiming 1.1 GB past retention
Reclaimed 4820 stills past every render window (1.2 GB so far)
```

That is the system defending itself, not a failure — but it means retention is too generous for this
disk, so shorten it or grow the disk rather than leaving the floor to do the work every hour. This
is an error, and does need action:

```
Reclaimed only 0.2 GB of the 1.1 GB needed: nothing left to delete.
```

It means the library has been emptied down to the weekly archive and still does not fit.

### Renders are slow or the VM is pegged

`ffmpeg` is the only heavy thing here. Check what a render actually costs:

```bash
journalctl -u timelapsed | grep 'Rendering' -A1
```

Levers, cheapest first:

1. Lower `output_fps` from 30 to 24 — 20% fewer frames to encode.
2. Lower `duration_seconds` — directly fewer frames.
3. Reduce capture resolution to 1280×720.
4. Add vCPU to the guest.

The unit files already run renders at `Nice=10` with `CPUWeight=50`, so they yield to anything more
important on the same Proxmox host.

### The OOM killer took a render

```
oom-kill:…task_memcg=/system.slice/timelapsed.service,task=ffmpeg
Out of memory: Killed process 6349 (ffmpeg) total-vm:887840kB, anon-rss:249944kB
```

A 1080p render peaks around 250 MB, and every channel rolls over on the same tick, so the shape of
this failure is *all* channels rendering at once on a guest that cannot hold them. Three things are
meant to prevent it, and it is worth checking all three:

```bash
grep max_concurrent_renders /etc/timelapsed.ini    # 1 unless deliberately raised
systemctl show timelapsed -p MemoryMax             # the cgroup backstop
swapon --show                                      # headroom for spikes
```

`MemoryMax` matters beyond the render itself: without it the kernel picks a victim anywhere on the
guest, so a render can take out `tailscaled` or the viewer instead of itself. `deploy/install.sh`
provisions a 2 GB swapfile at `/swapfile` with `vm.swappiness=10`; a guest installed before that
existed will not have one.

Windows lost this way are re-rendered automatically — see "There is a gap in the timeline".

### The viewer shows nothing

```bash
systemctl status timelapsed-web
curl -s localhost:8080/healthz
curl -s localhost:8080/api/timelapses | head
```

An empty JSON array means no videos have been rendered yet — that is a capture/render problem, not
a viewer problem. If `/healthz` fails but the service is running, check `host` and `port` in the
config.

With nginx in front, `8080` is nginx and the viewer is on `127.0.0.1:8081`. A `502` from `8080`
means nginx is up and the viewer is not:

```bash
systemctl status nginx timelapsed-web
curl -s localhost:8081/healthz          # the viewer directly, bypassing nginx
sudo tail /var/log/nginx/timelapsed.error.log
```

### Videos play but are no faster than before

Only relevant with nginx in front. It means nginx is falling back to the viewer for every video
rather than serving them itself, which the config does deliberately so that a permissions mistake
costs speed instead of playback.

```bash
curl -sI localhost:8080/video/1/weekly_….mp4 | grep -i 'etag\|server'
```

nginx sets an `ETag`; the viewer does not. If there is none, look for a `403` in
`/var/log/nginx/timelapsed.error.log` and give nginx read access:

```bash
sudo chmod -R g+rX /var/lib/timelapsed
```

The other cause is the library having moved without the site file following it — the root is baked
into `alias` at install time. `sudo /opt/timelapsed/deploy/nginx-setup.sh` re-reads it from the
config and re-renders.

### Recognition is not finding anything

Only relevant with `[analysis] enabled = true`. Full detail in
[Recognition](Recognition.md).

| Symptom | Cause |
| --- | --- |
| `Model not found at ... Run deploy/fetch-models.sh` | Enabled before the models were downloaded |
| `Analysis is disabled in the config` then exit 0 | `enabled` is false; the unit is meant to stay stopped |
| `attempt to write a readonly database` from the viewer | `timelapsed-web.service` lost `ReadWritePaths=/var/lib/timelapsed/index`; WAL writes sidecars even to read |
| Events everywhere, day and night, that never end | `score_threshold` below 0.5, detecting scenery as objects |
| Watermarks hours behind | Backfill still draining, or the guest is CPU-starved |

```bash
# how far each channel has been analysed (no sqlite3 CLI on a stock guest,
# so this goes through the venv's Python, which is always there)
sudo -u timelapsed /opt/timelapsed/.venv/bin/python -c '
import sqlite3
db = sqlite3.connect("file:/var/lib/timelapsed/index/index.sqlite3?mode=ro", uri=True)
for row in db.execute("SELECT channel, datetime(analysed_through, \'unixepoch\') FROM watermark ORDER BY channel"):
    print(*row)
'

# per-pass throughput, logged every pass that did work
journalctl -u timelapsed-analyzer -S -1h | grep 'ms/frame'
```

Recognition is entirely optional and entirely separable. Stopping it, or deleting
`/var/lib/timelapsed/index` outright, leaves capture and the viewer working.

## Upgrades

The checkout at `/opt/timelapsed` **is** the install — there is no copy to get out of sync — so an
upgrade is a pull and a restart:

```bash
/opt/timelapsed/deploy/update.sh
```

It pulls fast-forward-only, and when the commit actually changed it refreshes dependencies and
reinstalls the systemd units before restarting both services and printing their status. By hand:

```bash
cd /opt/timelapsed && git pull && sudo systemctl restart timelapsed timelapsed-web
```

The checkout is owned by you rather than the service user, so `git pull` needs no `sudo`; only the
restart does. The guest authenticates to the private repo with a read-only deploy key in
`~/.ssh/id_ed25519`, so a pull works unattended and cannot push.

If a release changes packaging rather than just code, `sudo bash deploy/install.sh` is still the
full path and is idempotent.

`/etc/timelapsed.ini` lives outside the checkout, so it is never touched by a pull or an install.
New settings get defaults, so an old config keeps working — skim
[Configuration](Configuration.md) after an upgrade to see whether anything new is worth setting.

### The daily viewer restart

`timelapsed-web-restart.timer` bounces `timelapsed-web` at 04:00 (±15 minutes) every day, so a slow
leak or a wedged socket in the long-lived stdlib HTTP server never accumulates. It runs
`systemctl try-restart`, so it does nothing when the viewer is deliberately stopped.

```bash
systemctl list-timers timelapsed-web-restart.timer
journalctl -u timelapsed-web-restart -n 20
```

The capture daemon is deliberately **not** on this timer: restarting it drops the in-memory cadence
state, and a restart near an hour boundary would skip that hour's render.

To turn the bounce off:

```bash
sudo systemctl disable --now timelapsed-web-restart.timer
```

## Backups

Three things with very different value:

| What | Size | Worth backing up? |
| --- | --- | --- |
| `/etc/timelapsed.ini` | 1 KB | **Yes.** Holds the NVR password. |
| `/var/lib/timelapsed/*/timelapse/` | MBs–GBs | **Yes**, if the history matters. This is the whole point of the system. |
| `/var/lib/timelapsed/*/keyframe/` | ~500 MB/year | **Yes, most of all.** The only unrecoverable thing here: a pruned still cannot be re-promoted, and every monthly and progress video is re-renderable from these. |
| `/var/lib/timelapsed/*/image/` | Tens of GBs | **No.** Regenerated continuously and pruned within days. |

Exclude the stills from `vzdump`:

```bash
sudo -n vzdump 302 --storage local --mode snapshot --exclude-path /var/lib/timelapsed/
```

To archive just the videos off-box:

```bash
rsync -av --include='*/' --include='*/timelapse/***' --include='*/keyframe/***' --exclude='*' \
  /var/lib/timelapsed/ backup-host:/archive/timelapsed/
```

Note that `--exclude-path /var/lib/timelapsed/` above drops the keyframes from the `vzdump` too, so
the `rsync` is the thing actually protecting them. Worth a cron entry rather than a one-off.

## Monitoring

Start at the viewer's **[system status page](System-Status.md)**, at `/status`. It answers most of
what is on this page — is the disk filling, is every camera still writing, is analysis keeping up,
are renders being produced — from one screen, and its checks list is empty on a healthy server.

For an actual alert, the same report is JSON at `/api/system`, and an empty `checks` array is the
whole probe:

```bash
# Exits non-zero, and prints what is wrong, when anything is.
curl -sf localhost:8080/api/system \
  | jq -e -r 'if (.checks | map(select(.level == "error")) | length) == 0
              then empty else (.checks[] | "\(.level): \(.title) — \(.detail)"), false end'
```

Without the viewer running, the simplest useful check is whether new stills are still arriving:

```bash
find /var/lib/timelapsed -name '*.jpg' -newermt '-5 minutes' | wc -l
```

Zero across all channels means capture has stopped. With Home Assistant on the same network, a
`command_line` sensor over SSH running either check makes a decent alert. `/healthz` on the viewer
covers the web service itself.

`Restart=always` with `RestartSec=10` means both services come back on their own after a crash or
an OOM kill, so alerting on "no new images for 15 minutes" catches the cases that matter without
firing on transient restarts.
