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
| `Previous weekly render … still running; skipping this one` | Renders take longer than the gap between them. | Give the VM more vCPU, or reduce `output_fps` / resolution. |
| Nothing at all | It has not rolled over yet. | Hourly fires on the hour, daily at midnight UTC, weekly on Monday. |

Remember all rollovers are **UTC**. A "daily" video is rendered at 00:00 UTC, not local midnight.

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

### The viewer shows nothing

```bash
systemctl status timelapsed-web
curl -s localhost:8080/healthz
curl -s localhost:8080/api/timelapses | head
```

An empty JSON array means no videos have been rendered yet — that is a capture/render problem, not
a viewer problem. If `/healthz` fails but the service is running, check `host` and `port` in the
config.

## Upgrades

```bash
cd ~/timelapsed
git pull
sudo bash deploy/install.sh    # idempotent; restarts both services
```

`install.sh` preserves an existing `/etc/timelapsed.ini` and will not overwrite it. When new
settings are added they get defaults, so an old config keeps working — check
[Configuration](Configuration) after an upgrade to see whether anything new is worth setting.

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
