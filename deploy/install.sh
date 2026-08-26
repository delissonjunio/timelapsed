#!/usr/bin/env bash
#
# Installs timelapsed on a Debian/Ubuntu host (tested on Ubuntu 24.04).
# Run as root ON THE GUEST, not on the Proxmox host:
#
#   sudo bash deploy/install.sh
#
# Idempotent: safe to re-run to upgrade an existing install.
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

if [[ $EUID -ne 0 ]]; then
    echo "error: run this as root (sudo bash deploy/install.sh)" >&2
    exit 1
fi

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg git

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
"${INSTALL_DIR}/.venv/bin/pip" install --quiet requests backoff rich python-dateutil

echo "==> Preparing library directory ${LIBRARY_DIR}"
mkdir -p "${LIBRARY_DIR}"
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
cp "${REPO_DIR}"/deploy/timelapsed*.service "${REPO_DIR}"/deploy/timelapsed*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now timelapsed-web-restart.timer

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
