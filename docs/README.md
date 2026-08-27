# Timelapsed

Timelapsed turns a security NVR into a timelapse camera. It pulls still frames over HTTP, keeps
them on disk as plain JPEGs, and renders hourly, daily and weekly videos with `ffmpeg` -- plus
monthly and since-day-one ones, from one frame a day, for watching a building go up. A small
built-in web viewer lets you watch them from a phone.

It is deliberately boring: no database, no queue, no cloud account. If you can read a directory
listing, you can debug it.

## Start here

* **[Architecture](Architecture.md)** — what runs, in what process, writing what where
* **[Configuration](Configuration.md)** — every setting and how they interact
* **[Storage Planning](Storage-Planning.md)** — the one decision that matters most
* **[Proxmox Deployment](Proxmox-Deployment.md)** — from `qm create` to a running service
* **[Viewing Timelapses](Viewing-Timelapses.md)** — the viewer, Tailscale Serve, Jellyfin
* **[System Status](System-Status.md)** — the `/status` page: storage, capture health, how far behind analysis is
* **[Recognition](Recognition.md)** — people, vehicles and plates on the timeline
* **[Recognition Feasibility](Recognition-Feasibility.md)** — what these cameras can actually support, measured
* **[Operations](Operations.md)** — logs, failure modes, upgrades, backups
* **[Development](Development.md)** — tests and project layout
* **[NVR Roadmap](NVR-Roadmap.md)** — pulling event video off the NVR (planned, not built)

## The two things that trip people up

**1. Retention must outlast your longest still-sourced cadence.** A weekly render reads seven days
of stills. If `image_retention_days` is 7 or less, pruning deletes those frames before the render
runs and you silently get no weekly video. Set it to at least 8. Timelapsed prints a warning at
startup if you don't. `monthly` and `progress` do not read the stills at all — they read one promoted
frame per day, which is what makes a month-long video affordable.

**2. The capture interval drives disk usage, not video length.** Video length is fixed by
`duration_seconds` — surplus frames get sampled out. What the interval actually controls is how
much disk you burn and how smooth the *hourly* video is. See [Storage Planning](Storage-Planning.md).

## Requirements

* Python 3.11 or newer
* `ffmpeg` and `ffprobe` on `PATH`
* An NVR exposing the ISAPI still-image endpoint (Hikvision and most of its OEM rebadges)
