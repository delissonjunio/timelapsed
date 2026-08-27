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
| `No images found for channel 1 between … skipping weekly render` | Stills were pruned before the render ran. | Raise `image_retention_days` above your longest cadence. |
| `Previous weekly render … still running; skipping this one` | Renders take longer than the gap between them. | Harmless on its own: the skipped window is picked up as a missing window later. If it is constant, give the VM more vCPU or reduce `output_fps` / resolution. |
| `Channel 5 is waiting for a render slot` | `max_concurrent_renders` is doing its job. | Nothing, unless the wait outlasts the cadence. |
| Nothing at all | It has not rolled over yet. | Hourly fires on the hour, daily at midnight UTC, weekly on Monday. |

Remember all rollovers are **UTC**. A "daily" video is rendered at 00:00 UTC, not local midnight.

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
# how far each channel has been analysed
sudo -u timelapsed sqlite3 -readonly /var/lib/timelapsed/index/index.sqlite3 \
  'SELECT channel, datetime(analysed_through, "unixepoch") FROM watermark ORDER BY channel;'

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
| `/var/lib/timelapsed/*/image/` | Tens of GBs | **No.** Regenerated continuously and pruned within days. |

Exclude the stills from `vzdump`:

```bash
sudo -n vzdump 302 --storage local --mode snapshot --exclude-path /var/lib/timelapsed/
```

To archive just the videos off-box:

```bash
rsync -av --include='*/' --include='*/timelapse/***' --exclude='*' \
  /var/lib/timelapsed/ backup-host:/archive/timelapsed/
```

## Monitoring

The simplest useful check is whether new stills are still arriving:

```bash
find /var/lib/timelapsed -name '*.jpg' -newermt '-5 minutes' | wc -l
```

Zero across all channels means capture has stopped. With Home Assistant on the same network, a
`command_line` sensor over SSH running that check makes a decent alert. `/healthz` on the viewer
covers the web service.

`Restart=always` with `RestartSec=10` means both services come back on their own after a crash or
an OOM kill, so alerting on "no new images for 15 minutes" catches the cases that matter without
firing on transient restarts.
