# Proxmox Deployment

End-to-end: create a VM on Proxmox VE, install Timelapsed from a git checkout, reach the NVR over
Tailscale, and publish the viewer over Tailscale.

This is the procedure that built VM 302 (`timelapsed`) on the node at `192.168.50.25`, written from
what actually ran.

## Sizing the guest

| Resource | Used | Why |
| --- | --- | --- |
| vCPU | 2 | ffmpeg is the only real load and it is bursty. `Nice=10` keeps it from starving the host. Go to 4 if renders start overlapping the next cadence. |
| RAM | 2 GB | The daemon is a few hundred MB per channel worker. ffmpeg at `-preset veryfast` 1080p stays well under 1 GB. |
| Disk | 200 GB | Six channels, 10-second interval, 8-day still retention is ~96 GB of stills alone. See [Storage Planning](Storage-Planning.md). Thin-provisioned, so it only consumes what is written. |

Size the disk from your own channel count and interval, not from this table. The stills dominate and
they are the term that scales.

## 1. Create the VM

From the Proxmox host. `qm` is not on the login `PATH` on this node, hence `sudo -n qm`; the
`perl: warning: Setting locale failed` noise on stderr is harmless.

```bash
VMID=302

sudo -n qm create $VMID \
  --name timelapsed \
  --ostype l26 \
  --machine q35 \
  --cpu host --cores 2 \
  --memory 2048 --balloon 0 \
  --scsihw virtio-scsi-single \
  --net0 virtio,bridge=vmbr0 \
  --serial0 socket --vga serial0 \
  --agent enabled=1 \
  --onboot 1

# Import the cached Ubuntu 24.04 cloud image straight into a new disk.
# (`qm importdisk` still works but leaves the disk unattached.)
sudo -n qm set $VMID --scsi0 \
  local-lvm:0,import-from=/var/lib/vz/template/iso/noble-server-cloudimg-amd64.img,discard=on,ssd=1

# Cloud images ship a ~3.5 GB root filesystem. Grow it.
sudo -n qm disk resize $VMID scsi0 200G

sudo -n qm set $VMID --ide2 local-lvm:cloudinit --boot order=scsi0
sudo -n qm set $VMID --ciuser delisson --ciupgrade 0 --ipconfig0 ip=dhcp \
  --cicustom "vendor=local:snippets/vendor-qga.yaml" \
  --sshkeys /home/delisson/.ssh/authorized_keys

sudo -n qm start $VMID
```

Find the address once the guest agent answers:

```bash
sudo -n qm guest cmd $VMID network-get-interfaces | grep -A2 '"ip-address"'
```

The disk can be grown later without downtime — `qm disk resize`, then `growpart /dev/sda 1` and
`resize2fs /dev/sda1` inside the guest. It cannot be shrunk, so start smaller than you think.

## 2. Join the tailnet

Do this **before** configuring Timelapsed: on this network the NVR sits on a different subnet
(`192.168.18.0/24`) reachable only through a Tailscale subnet router, so the guest cannot see the
NVR at all until it is on the tailnet.

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
advertises `192.168.50.0/24` — the guest's own LAN. That route lands in routing table 52, which
policy rule 5270 consults **before** the main table, so the guest starts answering its LAN
neighbours (the Proxmox host included) up the tunnel and effectively falls off its own network.

`deploy/tailscale-local-subnet-route.service` fixes this by adding an `ip rule` at priority 5260
that sends anything bound for the local subnet back to the main table. `install.sh` enables it
automatically when `tailscaled` is present. Verify:

```bash
ip rule show | grep -E '5260|5270'
# 5260:	from all to 192.168.50.0/24 lookup main
# 5270:	from all lookup 52
```

## 3. Install Timelapsed

The repo is private, so the guest needs its own read-only deploy key. Generate it on the guest and
register it — never copy a personal key onto a server.

```bash
# On the guest
ssh-keygen -t ed25519 -N '' -C 'timelapsed-vm-deploy' -f ~/.ssh/id_ed25519
ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts
cat ~/.ssh/id_ed25519.pub
```

```bash
# On your workstation, with the printed public key
gh api -X POST repos/delissonjunio/timelapsed/keys \
  -f title='timelapsed-vm (VM 302, read-only)' \
  -f key='ssh-ed25519 AAAA... timelapsed-vm-deploy' \
  -F read_only=true
```

Then clone **to `/opt/timelapsed` directly**. The checkout is the install: upgrading later is
`git pull` in place, so there is no copy step to get out of sync.

```bash
# On the guest
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

## 8. Snapshot the working VM

Once it has run cleanly for a day:

```bash
sudo -n qm snapshot 302 working-install --description "timelapsed configured and capturing"
```

## Backups

`vzdump` of the whole VM would include the entire image library — tens of gigabytes regenerated
continuously and not worth backing up. Exclude it:

```bash
sudo -n vzdump 302 --storage local --mode snapshot --exclude-path /var/lib/timelapsed/
```

What actually needs backing up is `/etc/timelapsed.ini` (small, and holds the NVR password) and, if
you care about history, `/var/lib/timelapsed/*/timelapse/` — the rendered videos. See
[Operations](Operations.md).

## Firewall

The guest needs to reach the NVR, and the viewer needs to be reachable on 8080 — but only from the
tailnet. If you enable the Proxmox firewall on this VM:

```bash
sudo -n pvesh create /nodes/$(hostname)/qemu/302/firewall/rules \
  --type in --action ACCEPT --proto tcp --dport 22 --source 192.168.50.0/24
sudo -n pvesh create /nodes/$(hostname)/qemu/302/firewall/rules \
  --type in --action ACCEPT --proto tcp --dport 8080 --source 100.64.0.0/10
```

`100.64.0.0/10` is the Tailscale CGNAT range. With `tailscale serve` in front, the viewer port does
not need to be reachable from the LAN at all.
