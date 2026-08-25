# Viewing Timelapses

## The built-in viewer

Timelapsed ships a small web viewer with no dependencies beyond the Python standard library. It
runs as its own service:

```bash
sudo systemctl enable --now timelapsed-web
```

It serves:

| Path | What it does |
| --- | --- |
| `/` | Grid of every rendered video, newest first, with inline playback |
| `/?channel=1` | Filter to one channel |
| `/?cadence=weekly` | Filter to one cadence |
| `/?channel=1&cadence=daily` | Both |
| `/api/timelapses` | The same list as JSON |
| `/video/{channel}/{filename}` | The video file, with HTTP Range support |
| `/healthz` | Returns `ok`, for monitoring |

The page is mobile-first and works in Safari on iOS, which is fussier than most browsers: it needs
`Accept-Ranges`, correct `Content-Type`, and `playsinline`. All three are handled.

Videos use `preload="none"`, so opening a page with fifty videos on it costs one HTML request, not
fifty video downloads. Playback starts quickly because renders are written with
`-movflags +faststart`.

## Publishing it with Tailscale Serve

This is the recommended setup. It gives you a real HTTPS certificate, a stable hostname, and no
port forwarding:

```bash
sudo tailscale serve --bg 8080
```

The viewer is then at `https://timelapsed.<your-tailnet>.ts.net` from any device signed into your
tailnet. Add it to your phone's home screen and it behaves like an app.

```bash
sudo tailscale serve status   # show what is published
sudo tailscale serve --https=443 off   # stop publishing
```

### Do not use Funnel

`tailscale funnel` publishes to the public internet. **The viewer has no authentication** — it is
built on the assumption that the network is the access control. Funnel would make your camera
footage available to anyone who guesses or is given the URL.

If you genuinely need public access, put an authenticating reverse proxy in front of it
(Caddy with `basic_auth`, or an OAuth proxy) rather than exposing the viewer directly.

## Why not YouTube

A reasonable question, since it is free and plays anywhere. It does not work for this:

* **API quota.** The YouTube Data API allocates 10,000 units a day by default and an upload costs
  1,600 units — **six uploads a day**. Three channels producing hourly, daily and weekly videos is
  about 75 uploads a day, roughly twelve times over the limit. Quota increases require a written
  audit that is rarely granted for automated surveillance uploads.
* **It is not a storage service,** and bulk automated uploads of repetitive footage is precisely
  the pattern that draws strikes. "Unlisted" is not private; anyone with the link can watch.
* **Re-encoding and processing delay.** Your h264 is transcoded again, and there is a wait before
  each video is playable.

Cloud object storage (Cloudflare R2's free tier, Backblaze B2) is a reasonable *backup* target, but
it does not solve "watch it easily" — you still need something to browse and play it. Treat that as
a separate problem from viewing.

## Jellyfin, if you want apps

If you want native iOS / Apple TV / Android apps, resume-where-you-left-off, and thumbnails, run
Jellyfin on the same guest pointed at the timelapse directories. It costs about 1 GB of RAM.

```bash
curl -fsSL https://repo.jellyfin.org/install-debuntu.sh | sudo bash
```

Then in the setup wizard add a library:

* **Content type:** Home videos and photos
* **Folder:** `/var/lib/timelapsed`

Give Jellyfin read access to the library:

```bash
sudo usermod -aG timelapsed jellyfin
sudo chmod -R g+rX /var/lib/timelapsed
sudo systemctl restart jellyfin
```

Publish it over Tailscale the same way:

```bash
sudo tailscale serve --bg 8096
```

Jellyfin has real user accounts, so it is a better fit if you want to share footage with someone
who should not have access to your whole tailnet.

Note that Jellyfin will try to organise the videos as media, and the filenames
(`weekly_20250601_120000_UTC-20250608_120000_UTC.mp4`) are not what its metadata scrapers expect.
Disable metadata downloading for that library to keep it from inventing movie matches.

## Direct file access

Nothing about the layout is proprietary. The videos are ordinary MP4 files:

```bash
scp delisson@timelapsed:/var/lib/timelapsed/1/timelapse/weekly_*.mp4 .
```

Or mount the directory over SSHFS, or export it read-only over Samba if you want it to show up in
Finder. The web viewer is a convenience, not a gatekeeper.
