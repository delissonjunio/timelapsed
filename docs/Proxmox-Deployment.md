# Proxmox Deployment

End-to-end: create a VM on Proxmox VE, install Timelapsed, publish the viewer over Tailscale.

## Sizing the guest

| Resource | Recommendation | Why |
| --- | --- | --- |
| vCPU | 2–4 | ffmpeg is the only real load, and it is bursty. 2 is enough for 3 channels; go to 4 if renders take longer than the gap between them. |
| RAM | 2 GB | The daemon is a few hundred MB. ffmpeg with `-preset veryfast` at 1080p stays well under 1 GB. |
| Disk | 100 GB | Three channels, 10-second interval, 8-day retention. See [Storage Planning](Storage-Planning.md). |

The renders are what make CPU matter. If you enable `weekly` on many channels, the Monday-morning
renders all land at once — hence `Nice=10` and `CPUWeight=50` in the unit file, so they lose the
scheduler fight against anything more important on the same host.

## 1. Create the VM

From the Proxmox host, mirroring the conventions used by the other guests on this node:

```bash
VMID=302

sudo -n qm create $VMID \
  --name timelapsed \
  --machine q35 \
  --cpu host --cores 4 \
  --memory 2048 --balloon 0 \
  --scsihw virtio-scsi-single \
  --net0 virtio,bridge=vmbr0 \
  --ipconfig0 ip=dhcp \
  --ciuser delisson \
  --cicustom "vendor=local:snippets/vendor-qga.yaml" \
  --agent enabled=1 \
  --serial0 socket --vga serial0 \
  --ostype l26

# Import the cached Ubuntu 24.04 cloud image as the boot disk
sudo -n qm importdisk $VMID /var/lib/vz/template/iso/noble-server-cloudimg-amd64.img local-lvm
sudo -n qm set $VMID --scsi0 local-lvm:vm-$VMID-disk-0,discard=on
sudo -n qm set $VMID --boot order=scsi0

# Cloud images ship a tiny root filesystem; grow it to 100 GB
sudo -n qm resize $VMID scsi0 100G

# SSH key for the cloud-init user
sudo -n qm set $VMID --sshkeys ~/.ssh/authorized_keys

sudo -n qm start $VMID
```

`qm` is not on the login `PATH` on this node, hence `sudo -n qm`. The `perl: warning: Setting locale
failed` noise on stderr is harmless.

Find the address once the guest agent is up:

```bash
sudo -n qm guest cmd $VMID network-get-interfaces | grep -A2 '"ip-address"'
```

## 2. Install Timelapsed

SSH into the **guest** (not the Proxmox host):

```bash
ssh delisson@<guest-ip>

sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/delissonjunio/timelapsed.git
cd timelapsed
sudo bash deploy/install.sh
```

`install.sh` installs `ffmpeg` and `python3-venv`, creates a `timelapsed` system user, sets up
`/opt/timelapsed` with a virtualenv, creates `/var/lib/timelapsed`, drops a config template at
`/etc/timelapsed.ini` with `chmod 640 root:timelapsed`, and installs both systemd units. It is
idempotent — re-run it to upgrade.

It deliberately does **not** start the service on a fresh install, because the config still has
placeholder values.

## 3. Configure

```bash
sudoedit /etc/timelapsed.ini
```

Set at minimum `url`, `username`, `password`, `channels`, and `interval_seconds`. Everything else
has a working default. See [Configuration](Configuration.md).

Check the NVR is reachable from the guest before starting anything:

```bash
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' \
  --digest -u 'admin:PASSWORD' \
  'http://192.168.1.10/ISAPI/Streaming/channels/101/picture?videoResolutionWidth=1920&videoResolutionHeight=1080'
```

`200 image/jpeg` means you are good. `401` is a credentials problem. `404` means that channel does
not exist — remember channel `1` is ISAPI channel `101`.

## 4. Start it

```bash
sudo systemctl enable --now timelapsed timelapsed-web
journalctl -u timelapsed -f
```

You want to see, within a few seconds:

```
Timelapsed starting: 3 channel(s) [1, 2, 3], cadences [hourly, daily, weekly], every 10s
Initialised NVR capture agent for http://192.168.1.10 as user admin
All timelapsed workers started
```

Any `Configuration problem:` lines are startup warnings worth reading — they mean a setting will
not do what you expect. Confirm images are landing:

```bash
watch -n5 'ls /var/lib/timelapsed/1/image | tail -3; ls /var/lib/timelapsed/1/image | wc -l'
```

The first hourly video appears at the next hour boundary, the first daily at the next midnight UTC,
the first weekly on the next Monday.

## 5. Publish the viewer over Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=timelapsed
```

Then put Tailscale in front of the viewer. This gives you real HTTPS with a valid certificate and
no port forwarding:

```bash
sudo tailscale serve --bg 8080
sudo tailscale serve status
```

The viewer is now at `https://timelapsed.<your-tailnet>.ts.net` from any device on the tailnet,
including your phone. Nothing is exposed to the internet.

> **Do not use `tailscale funnel` here.** Funnel publishes to the public internet, and the viewer
> has no authentication. Anyone with the URL would be able to watch your cameras.

Verify from another tailnet device:

```bash
curl -s https://timelapsed.<your-tailnet>.ts.net/healthz
```

## 6. Take a snapshot of the working VM

Once it has been running cleanly for a day:

```bash
sudo -n qm snapshot 302 working-install --description "timelapsed configured and capturing"
```

## Backups

`vzdump` of the whole VM will include the entire image library, which is tens of gigabytes of data
that is regenerated continuously and not worth backing up. Exclude it:

```bash
sudo -n vzdump 302 --storage local --mode snapshot --exclude-path /var/lib/timelapsed/
```

What actually needs backing up is `/etc/timelapsed.ini` (small, and holds the password) and, if you
care about the history, `/var/lib/timelapsed/*/timelapse/` — the rendered videos. See
[Operations](Operations.md).

## Firewall

The guest needs to reach the NVR on port 80 or 443, and the viewer needs to be reachable on 8080 —
but only from the tailnet. If you enable the Proxmox firewall on this VM:

```bash
# Inbound: SSH from the LAN, viewer from the tailnet only
sudo -n pvesh create /nodes/$(hostname)/qemu/302/firewall/rules \
  --type in --action ACCEPT --proto tcp --dport 22 --source 192.168.50.0/24
sudo -n pvesh create /nodes/$(hostname)/qemu/302/firewall/rules \
  --type in --action ACCEPT --proto tcp --dport 8080 --source 100.64.0.0/10
```

`100.64.0.0/10` is the Tailscale CGNAT range. With `tailscale serve` in front, the viewer port does
not need to be reachable from the LAN at all.
