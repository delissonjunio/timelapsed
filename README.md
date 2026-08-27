# Timelapsed

Captures stills from an ISAPI (Hikvision-compatible) NVR and renders **hourly, daily and weekly**
timelapses, with a built-in web viewer to watch them from anywhere on your Tailnet.

No database, no cloud dependency, no message queue. Images are files, the filename is the index,
and `ffmpeg` does the rest.

Optionally it will also **recognise people, vehicles and number plates** in the stills it already
captured, and show them on the timeline — see [Recognition](docs/Recognition.md). That part is off
by default and is the one place a real index exists.

```
NVR ──HTTP snapshot──▶ capture worker ──▶ {root}/{channel}/image/20250601_120000_UTC.jpg
                             │
                             ├──on hour/day/week rollover──▶ render process ──ffmpeg──▶
                             │                                {root}/{channel}/timelapse/weekly_….mp4
                             └──hourly──▶ prune stills past retention
                                                                       ▼
                                                            web viewer :8080
```

## What it does, precisely

* **One process per channel.** Each loops forever: fetch a snapshot over HTTP Digest auth, write it
  to disk, check whether a timelapse is due, sleep for the configured interval.
* **Snapshots, not RTSP.** One `GET /ISAPI/Streaming/channels/{channel}01/picture` per frame. No
  stream to keep alive, no video to decode client-side.
* **Renders happen in their own process** so a long `ffmpeg` run never stalls capture. If a render
  is still going when the next one is due, the new one is skipped rather than queued.
* **Frames are sampled, not concatenated.** A day at a 10 second interval is 8,640 stills; a 60
  second video at 30 fps needs 1,800. Timelapsed picks 1,800 evenly spaced across the whole window,
  so the video always spans the full period and always plays at the length you asked for.
* **Retention is enforced.** Stills are pruned hourly. Without this a 10 second interval writes
  about 2.6 GB per channel per day, forever.

## Quick start

```bash
git clone git@github.com:delissonjunio/timelapsed.git
cd timelapsed
python3 -m venv .venv && .venv/bin/pip install -e .

cp timelapsed.ini.example ~/.timelapsed.ini
$EDITOR ~/.timelapsed.ini          # set url, password, channels, root

.venv/bin/python -m timelapsed      # capture daemon
.venv/bin/python -m timelapsed.web  # viewer on http://localhost:8080
```

Requires **Python 3.11+** and **ffmpeg** on `PATH`.

## Deploying to Proxmox

```bash
sudo bash deploy/install.sh
sudoedit /etc/timelapsed.ini
sudo systemctl enable --now timelapsed timelapsed-web
```

Full walkthrough, including creating the VM and publishing the viewer over Tailscale:
**[Proxmox Deployment](docs/Proxmox-Deployment.md)**.

## Configuration

Read from `/etc/timelapsed.ini`, then `~/.timelapsed.ini`, then `./timelapsed.ini` — later files
override earlier ones key by key. Every setting is documented in
[`timelapsed.ini.example`](timelapsed.ini.example) and in **[Configuration](docs/Configuration.md)**.

The one constraint worth stating up front: **`image_retention_days` must be greater than your
longest cadence.** A weekly render reads 7 days of stills, so retention below 8 days deletes the
frames before the render can use them. Timelapsed warns loudly at startup if you get this wrong.

## Documentation

The full documentation lives in [`docs/`](docs/README.md) and is versioned with the code.

| Page | What's in it |
| --- | --- |
| [Architecture](docs/Architecture.md) | Process model, storage layout, why each decision was made |
| [Configuration](docs/Configuration.md) | Every key, its default, and how the settings interact |
| [Storage Planning](docs/Storage-Planning.md) | How to pick a capture interval and size the disk |
| [Proxmox Deployment](docs/Proxmox-Deployment.md) | VM creation through to a running service |
| [Viewing Timelapses](docs/Viewing-Timelapses.md) | The built-in viewer, Tailscale Serve, Jellyfin |
| [Recognition](docs/Recognition.md) | People, vehicles and plates on the timeline |
| [Recognition Feasibility](docs/Recognition-Feasibility.md) | What the cameras can support, measured before building |
| [Operations](docs/Operations.md) | Logs, common failures, upgrades, backups |
| [Development](docs/Development.md) | Running the tests, project layout, contributing |

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

227 tests. They use the real filesystem and the real `ffmpeg` binary — renders are verified by
probing the output with `ffprobe` — and fake only the NVR.

## License

MIT. See [LICENSE](LICENSE).
