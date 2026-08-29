#!/usr/bin/env bash
#
# Installs timelapsed on a Debian/Ubuntu host (tested on Ubuntu 24.04).
# Run as root ON THE GUEST, not on the Proxmox host:
#
#   sudo bash deploy/install.sh
#   sudo bash deploy/install.sh --with-nginx    # serve videos from nginx
#   sudo bash deploy/install.sh --with-go2rtc   # live video on /live (needs nginx)
#
# Idempotent: safe to re-run to upgrade an existing install. --with-nginx and
# --with-go2rtc are sticky — once set up, re-running without the flag keeps them.
#
# The intended layout is a git checkout living at /opt/timelapsed, so upgrading
# is `git pull` in place followed by a restart — see deploy/update.sh. When this
# script is run from a checkout that is already at INSTALL_DIR it installs in
# place; run from anywhere else it copies the code in instead.

set -euo pipefail

INSTALL_DIR=${INSTALL_DIR:-/opt/timelapsed}
LIBRARY_DIR=${LIBRARY_DIR:-/var/lib/timelapsed}
CONFIG_PATH=${CONFIG_PATH:-/etc/timelapsed.ini}
SERVICE_USER=${SERVICE_USER:-timelapsed}
# Who owns the checkout, and so who can `git pull` without sudo.
REPO_OWNER=${REPO_OWNER:-${SUDO_USER:-root}}
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)

# Already set up once, so keep it set up: a bare re-run must not silently move
# the viewer back onto the public port and leave nginx proxying to nothing.
WITH_NGINX=${WITH_NGINX:-0}
[[ -f /etc/nginx/sites-available/timelapsed ]] && WITH_NGINX=1
WITH_GO2RTC=${WITH_GO2RTC:-0}
[[ -f /etc/systemd/system/go2rtc.service ]] && WITH_GO2RTC=1

for argument in "$@"; do
    case "${argument}" in
        --with-nginx) WITH_NGINX=1 ;;
        # The /live page reaches go2rtc through the nginx /go2rtc/ proxy, so
        # asking for one is asking for both.
        --with-go2rtc) WITH_GO2RTC=1; WITH_NGINX=1 ;;
        *) echo "error: unknown option ${argument}" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "error: run this as root (sudo bash deploy/install.sh)" >&2
    exit 1
fi

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg git

# A 2 GB guest with no swap has no shock absorber: a render spike is an instant
# kill rather than a few seconds of paging. This is headroom for those spikes,
# not a place to run from, hence the low swappiness.
echo "==> Ensuring swap exists"
SWAP_FILE=${SWAP_FILE:-/swapfile}
SWAP_SIZE_MB=${SWAP_SIZE_MB:-2048}
if [[ -z "$(swapon --show --noheadings)" ]]; then
    fallocate -l "${SWAP_SIZE_MB}M" "${SWAP_FILE}" \
        || dd if=/dev/zero of="${SWAP_FILE}" bs=1M count="${SWAP_SIZE_MB}" status=none
    chmod 600 "${SWAP_FILE}"
    mkswap -q "${SWAP_FILE}"
    swapon "${SWAP_FILE}"
    grep -q "^${SWAP_FILE}[[:space:]]" /etc/fstab || echo "${SWAP_FILE} none swap sw 0 0" >> /etc/fstab
else
    echo "Swap is already configured; leaving it alone."
fi
echo "vm.swappiness = 10" > /etc/sysctl.d/60-timelapsed-swappiness.conf
sysctl -q -w vm.swappiness=10

echo "==> Creating service user ${SERVICE_USER}"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

if [[ "${REPO_DIR}" == "$(readlink -f "${INSTALL_DIR}")" ]]; then
    echo "==> Installing in place from the checkout at ${INSTALL_DIR}"
else
    echo "==> Copying application to ${INSTALL_DIR}"
    mkdir -p "${INSTALL_DIR}"
    cp -r "${REPO_DIR}/timelapsed" "${REPO_DIR}/pyproject.toml" "${INSTALL_DIR}/"
    [[ -f "${REPO_DIR}/README.md" ]] && cp "${REPO_DIR}/README.md" "${INSTALL_DIR}/"
fi

echo "==> Building the virtualenv"
python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip
# Editable, and from pyproject.toml rather than a hand-kept list. This used to be
# a literal `pip install requests backoff rich ...` duplicated here and in
# update.sh, which meant adding a dependency to pyproject.toml silently did not
# reach the guest. Editable keeps the code running from the checkout, so
# `git pull` still takes effect without a reinstall.
"${INSTALL_DIR}/.venv/bin/pip" install --quiet -e "${INSTALL_DIR}"

echo "==> Preparing library directory ${LIBRARY_DIR}"
# index/ is created even when recognition is off: the viewer unit mounts it
# read-write for SQLite's WAL sidecars, and an empty directory costs nothing.
mkdir -p "${LIBRARY_DIR}" "${LIBRARY_DIR}/index"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${LIBRARY_DIR}"

# The code is owned by a human so `git pull` needs no sudo; the service user only
# ever reads it, which world-readable 755 already allows.
chown -R "${REPO_OWNER}" "${INSTALL_DIR}"
chmod 755 "${INSTALL_DIR}"

if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo "==> Installing config template to ${CONFIG_PATH}"
    cp "${REPO_DIR}/timelapsed.ini.example" "${CONFIG_PATH}"
    sed -i "s|^root = .*|root = ${LIBRARY_DIR}|" "${CONFIG_PATH}"
    CONFIG_IS_NEW=1
else
    echo "==> Keeping existing config at ${CONFIG_PATH}"
    CONFIG_IS_NEW=0
fi

# The config holds the NVR password: readable by the service user, nobody else.
chown root:"${SERVICE_USER}" "${CONFIG_PATH}"
chmod 640 "${CONFIG_PATH}"

echo "==> Installing systemd units"
cp "${REPO_DIR}"/deploy/timelapsed*.service "${REPO_DIR}"/deploy/tailscale-local-subnet-route.{service,timer} "${REPO_DIR}"/deploy/timelapsed*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now timelapsed-web-restart.timer

# Only relevant where Tailscale is providing the route to the NVR. The timer
# owns the rule now; installs from before it enabled the service directly.
if systemctl list-unit-files tailscaled.service >/dev/null 2>&1 && \
   systemctl is-enabled tailscaled.service >/dev/null 2>&1; then
    systemctl disable tailscale-local-subnet-route.service 2>/dev/null || true
    systemctl enable --now tailscale-local-subnet-route.timer
    systemctl restart tailscale-local-subnet-route.service
fi

# Recognition is opt-in. Only fetch the ~170 MB of models when the config
# actually asks for it, so a plain timelapse install stays small.
if grep -qE '^\s*enabled\s*=\s*(true|yes|1)\s*$' "${CONFIG_PATH}" 2>/dev/null; then
    echo "==> Fetching recognition models"
    SERVICE_USER="${SERVICE_USER}" "${REPO_DIR}/deploy/fetch-models.sh" "${LIBRARY_DIR}/index/models"
    mkdir -p "${LIBRARY_DIR}/index/crops"
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${LIBRARY_DIR}/index"
    systemctl enable --now timelapsed-analyzer
    systemctl restart timelapsed-analyzer
else
    echo "==> Recognition disabled ([analysis] enabled), skipping model download"
fi

# Last, because it moves the viewer off the port it was just configured with.
if [[ ${WITH_NGINX} -eq 1 ]]; then
    LIBRARY_DIR="${LIBRARY_DIR}" CONFIG_PATH="${CONFIG_PATH}" SERVICE_USER="${SERVICE_USER}" \
        "${REPO_DIR}/deploy/nginx-setup.sh"
fi

if [[ ${WITH_GO2RTC} -eq 1 && ${CONFIG_IS_NEW} -eq 0 ]]; then
    CONFIG_PATH="${CONFIG_PATH}" SERVICE_USER="${SERVICE_USER}" \
        "${REPO_DIR}/deploy/go2rtc-setup.sh"
elif [[ ${WITH_GO2RTC} -eq 1 ]]; then
    # go2rtc's config is rendered from the NVR credentials, which are still
    # placeholders on a first run.
    echo "==> Skipping go2rtc: ${CONFIG_PATH} still has placeholder credentials."
    echo "    After editing it, run: sudo bash ${REPO_DIR}/deploy/go2rtc-setup.sh"
fi

if [[ ${CONFIG_IS_NEW} -eq 1 ]]; then
    cat <<MESSAGE

Installed, but NOT started: ${CONFIG_PATH} still has placeholder values.

  1. Edit it:      sudoedit ${CONFIG_PATH}
  2. Start it:     sudo systemctl enable --now timelapsed timelapsed-web
  3. Watch it:     journalctl -u timelapsed -f

MESSAGE
else
    systemctl enable --now timelapsed timelapsed-web
    systemctl restart timelapsed timelapsed-web
    echo
    echo "Upgraded and restarted. Check: systemctl status timelapsed"
fi
