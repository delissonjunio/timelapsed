# Proxmox Deployment

End-to-end: create an LXC container on Proxmox VE, install Timelapsed from a git checkout, reach
the NVR over Tailscale, and publish the viewer over Tailscale.

This is the procedure that built CT 303 (`timelapsed`) on the node at `192.168.50.25`, written from
what actually ran.

## Sizing the container

| Resource | Used | Why |
| --- | --- | --- |
| Cores | 4 | ffmpeg is the only real load and it is bursty. `Nice=10` keeps it from starving the host. |
| Memory | 6 GB | This is a **cap, not a reservation** — the container only takes what it uses, which is one of the reasons to run this as a container at all. The daemon is a few hundred MB per channel worker; the analyzer peaks higher and has its own `MemoryMax`. |
| Root disk | 20 GB | OS, checkout, and virtualenv only. The library lives on its own mount point. |
| Library mount | 150 GB | Six channels, 10-second interval, 8-day still retention is ~96 GB of stills alone. See [Storage Planning](Storage-Planning.md). Thin-provisioned, so it only consumes what is written. |
| Archive mount | 1.4 TB | The NVR segment replica, on its own spinning-disk pool. See the [NVR Roadmap](NVR-Roadmap.md). |

Size the library mount from your own channel count and interval, not from this table. The stills
dominate and they are the term that scales.

## 1. Create the container

From the Proxmox host. `pct` is not on the login `PATH` on this node, hence `sudo -n pct`; the
`perl: warning: Setting locale failed` noise on stderr is harmless.

```bash
CTID=303

sudo -n pveam update
sudo -n pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst

sudo -n pct create $CTID local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname timelapsed \
  --unprivileged 1 \
  --features nesting=1 \
  --ostype ubuntu \
  --cores 4 --memory 6144 --swap 2048 \
  --rootfs local-lvm:20 \
  --mp0 local-lvm:150,mp=/var/lib/timelapsed,backup=0 \
  --mp1 hdd-thin:1400,mp=/var/lib/timelapsed/archive,backup=0 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --onboot 1
```

`--unprivileged 1` is the right default, and `--features nesting=1` is **required** with it: the
systemd units use `PrivateTmp`/`ProtectSystem`, which need mount namespaces that an unprivileged
container only gets with nesting on.

Two pieces of host-side plumbing before first start:

```bash
# Tailscale needs a TUN device.
cat <<'EOF' | sudo -n tee -a /etc/pve/lxc/$CTID.conf
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF

# PVE copies the HOST's resolv.conf into the container by default, and on this
# node that is Tailscale MagicDNS (100.100.100.100) — dead inside a container
# that is not on the tailnet yet. Pin real resolvers.
sudo -n pct set $CTID --nameserver "192.168.50.129 1.1.1.1"

sudo -n pct start $CTID
```

The container has no cloud-init; create the admin user by hand:

```bash
sudo -n pct exec $CTID -- bash -c '
  apt-get update && apt-get install -y openssh-server sudo curl git ca-certificates rsync
  useradd -m -u 1000 -s /bin/bash delisson && usermod -aG sudo delisson
  echo "delisson ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/delisson && chmod 440 /etc/sudoers.d/delisson
  install -d -m 700 -o delisson -g delisson /home/delisson/.ssh
  systemctl enable --now ssh
'
sudo -n pct push $CTID /home/delisson/.ssh/authorized_keys \
  /home/delisson/.ssh/authorized_keys --user 1000 --group 1000 --perms 600
```

Find the address:

```bash
sudo -n pct exec $CTID -- ip -4 addr show dev eth0
```

Mount points can be grown later without downtime — `sudo -n pct resize $CTID mp0 +50G` resizes the
volume and the filesystem in one step, no in-container action needed. They cannot be shrunk, so
start smaller than you think.

### The lost+found trap

The mount-point volumes are ext4, and their `lost+found` belongs to host root — an id an
unprivileged container cannot map, so `install.sh`'s `chown -R` over the library dies on it.
Fix once from the host:

```bash
sudo -n pct stop $CTID && sudo -n pct mount $CTID
sudo -n chown 100000:100000 /var/lib/lxc/$CTID/rootfs/var/lib/timelapsed/lost+found \
  /var/lib/lxc/$CTID/rootfs/var/lib/timelapsed/archive/lost+found
sudo -n pct unmount $CTID && sudo -n pct start $CTID
```

(100000 is where the unprivileged id map starts: container uid N is host uid 100000+N.)

## 2. Join the tailnet

Do this **before** configuring Timelapsed: on this network the NVR sits on a different subnet
(`192.168.18.0/24`) reachable only through a Tailscale subnet router, so the container cannot see
the NVR at all until it is on the tailnet.

```bash
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up --accept-routes --hostname=timelapsed
```

`--accept-routes` is the important flag: it is what pulls in the `192.168.18.0/24` route another
node advertises.

Confirm the NVR answers before going further:

```bash
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' \
  --digest -u 'admin:PASSWORD' \
  'http://192.168.18.89/ISAPI/Streaming/channels/101/picture?videoResolutionWidth=1920&videoResolutionHeight=1080'
```

`200 image/jpeg` means you are good. `401` is a credentials problem. `503` means that channel exists
on the NVR but has no camera attached. Remember channel `1` is ISAPI channel `101`.

To list the channels that actually have cameras:

```bash
curl -s --digest -u 'admin:PASSWORD' http://192.168.18.89/ISAPI/Streaming/channels \
  | grep -oE '<id>[0-9]+</id>'
```

IDs ending in `01` are main streams, `02` are sub streams. `101 501 601 701 801 901` means
`channels = 1,5,6,7,8,9`.

### The local-subnet trap

`--accept-routes` accepts *every* approved route on the tailnet, and on this tailnet another node
advertises `192.168.50.0/24` — the container's own LAN. That route lands in routing table 52,
which policy rule 5270 consults **before** the main table, so the container starts answering its
LAN neighbours (the Proxmox host included) up the tunnel and effectively falls off its own
network. The symptom is nasty: every Tailscale path keeps working, ARP for LAN peers never even
fires, and only LAN-direct traffic dies.

`deploy/tailscale-local-subnet-route.service` fixes this by adding an `ip rule` at priority 5260
that sends anything bound for the local subnet back to the main table, and the matching `.timer`
re-asserts it every minute — the rule has been observed to vanish mid-uptime, so it is not trusted
to a single boot-time shot. `install.sh` enables the timer automatically when `tailscaled` is
present. Verify:

```bash
ip rule show | grep -E '5260|5270'
# 5260:	from all to 192.168.50.0/24 lookup main
# 5270:	from all lookup 52
```

## 3. Install Timelapsed

The repo is private, so the container needs its own read-only deploy key. Generate it in the
container and register it — never copy a personal key onto a server.

```bash
# In the container
ssh-keygen -t ed25519 -N '' -C 'timelapsed-ct-deploy' -f ~/.ssh/id_ed25519
ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts
cat ~/.ssh/id_ed25519.pub
```

```bash
# On your workstation, with the printed public key
gh api -X POST repos/delissonjunio/timelapsed/keys \
  -f title='timelapsed-ct (CT 303, read-only)' \
  -f key='ssh-ed25519 AAAA... timelapsed-ct-deploy' \
  -F read_only=true
```

Then clone **to `/opt/timelapsed` directly**. The checkout is the install: upgrading later is
`git pull` in place, so there is no copy step to get out of sync.

```bash
# In the container
sudo mkdir -p /opt/timelapsed && sudo chown "$USER:$USER" /opt/timelapsed
git clone git@github.com:delissonjunio/timelapsed.git /opt/timelapsed
cd /opt/timelapsed
sudo bash deploy/install.sh
```

`install.sh` installs `ffmpeg`, `git` and `python3-venv`, creates the `timelapsed` system user,
builds the virtualenv at `/opt/timelapsed/.venv`, creates `/var/lib/timelapsed`, drops a config
template at `/etc/timelapsed.ini` (`chmod 640`, `root:timelapsed`), and installs the systemd units
and the daily restart timer. It detects that it is running from a checkout already at
`/opt/timelapsed` and installs in place rather than copying.

The checkout stays owned by **you**, not the service user, so `git pull` needs no `sudo`. The
service user only ever reads it.

`install.sh` deliberately does **not** start the capture daemon on a fresh install, because the
config still holds placeholder values.

## 4. Configure

```bash
sudoedit /etc/timelapsed.ini
```

Set at minimum `url`, `username`, `password`, `channels` and `interval_seconds`. Everything else has
a working default. See [Configuration](Configuration.md).

## 5. Start it

```bash
sudo systemctl enable --now timelapsed timelapsed-web
journalctl -u timelapsed -f
```

Within a few seconds you want:

```
Initialised NVR capture agent for http://192.168.18.89 as user admin
Timelapsed starting: 6 channel(s) [1, 5, 6, 7, 8, 9], cadences [hourly, daily, weekly], every 10s
All timelapsed workers started
```

Any `Configuration problem:` line is a startup warning worth reading — it means a setting will not
do what you expect. Confirm images are landing:

```bash
watch -n5 'ls /var/lib/timelapsed/1/image | tail -3; ls /var/lib/timelapsed/1/image | wc -l'
```

The first hourly video appears at the next hour boundary that has a full hour of stills behind it,
the first daily at the next midnight UTC, the first weekly on the next Monday.

## 6. Publish the viewer over Tailscale

```bash
sudo tailscale serve --bg 8080
sudo tailscale serve status
```

The viewer is now at `https://timelapsed.<your-tailnet>.ts.net` from any device on the tailnet,
phone included, with a real certificate and no port forwarding.

> **Do not use `tailscale funnel` here.** Funnel publishes to the public internet and the viewer has
> no authentication. Anyone with the URL could watch your cameras.

Optionally put nginx in front, so rendered videos come off the disk instead of through Python. The
published port stays `8080`, so this command and the firewall rules below are unaffected:

```bash
sudo bash /opt/timelapsed/deploy/install.sh --with-nginx
```

See [Viewing Timelapses](Viewing-Timelapses.md) for what it is and is not worth.

Verify from another tailnet device:

```bash
curl -s https://timelapsed.<your-tailnet>.ts.net/healthz
```

## 7. Updating

The checkout at `/opt/timelapsed` is the install, so an upgrade is a pull and a restart:

```bash
/opt/timelapsed/deploy/update.sh
```

That pulls fast-forward-only, refreshes dependencies and reinstalls the systemd units when the
commit changed, then restarts both services and prints their status. Or by hand:

```bash
cd /opt/timelapsed && git pull && sudo systemctl restart timelapsed timelapsed-web
```

The config lives at `/etc/timelapsed.ini`, outside the checkout, so it is never touched by a pull.

`timelapsed-web-restart.timer` also bounces the viewer daily at 04:00 (±15 minutes) so any slow leak
or wedged socket in the long-lived HTTP server never accumulates. It uses `try-restart`, so it does
nothing if the viewer is deliberately stopped.

```bash
systemctl list-timers timelapsed-web-restart.timer
```

## 8. Snapshot the working container

Once it has run cleanly for a day:

```bash
sudo -n pct snapshot 303 working-install --description "timelapsed configured and capturing"
```

## Backups

`vzdump` of the whole container would include the entire image library — tens of gigabytes
regenerated continuously and not worth backing up. The library and archive mount points already
carry `backup=0`, so a plain dump skips them:

```bash
sudo -n vzdump 303 --storage local --mode snapshot
```

What actually needs backing up is `/etc/timelapsed.ini` (small, and holds the NVR password) and, if
you care about history, `/var/lib/timelapsed/*/timelapse/` — the rendered videos. See
[Operations](Operations.md).

## Firewall

The container needs to reach the NVR, and the viewer needs to be reachable on 8080 — but only from
the tailnet. If you enable the Proxmox firewall on this container:

```bash
sudo -n pvesh create /nodes/$(hostname)/lxc/303/firewall/rules \
  --type in --action ACCEPT --proto tcp --dport 22 --source 192.168.50.0/24
sudo -n pvesh create /nodes/$(hostname)/lxc/303/firewall/rules \
  --type in --action ACCEPT --proto tcp --dport 8080 --source 100.64.0.0/10
```

`100.64.0.0/10` is the Tailscale CGNAT range. With `tailscale serve` in front, the viewer port does
not need to be reachable from the LAN at all.

## A rescue hatch VMs never had

If the container falls off the network entirely (see the local-subnet trap above), the Proxmox
host can always get a root shell inside it with no networking at all:

```bash
sudo -n pct enter 303
```
