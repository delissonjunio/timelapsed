#!/usr/bin/env bash
#
# Upgrade an in-place install: pull, refresh dependencies, restart.
#
#   /opt/timelapsed/deploy/update.sh
#
# `git pull` runs as you (the checkout is yours); only the restart needs sudo.

set -euo pipefail

INSTALL_DIR=${INSTALL_DIR:-/opt/timelapsed}
cd "${INSTALL_DIR}"

echo "==> Pulling"
BEFORE=$(git rev-parse HEAD)
git pull --ff-only
AFTER=$(git rev-parse HEAD)

if [[ "${BEFORE}" == "${AFTER}" ]]; then
    echo "Already up to date at ${AFTER:0:8}; restarting anyway."
else
    echo "==> ${BEFORE:0:8} -> ${AFTER:0:8}"
    echo "==> Refreshing dependencies"
    # Not --quiet: this step once exited 0 having installed nothing, and the
    # first sign of it was the analyzer failing to import onnxruntime long
    # afterwards. Let it be noisy, and check the result rather than the exit code.
    sudo "${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"
    if ! sudo "${INSTALL_DIR}/.venv/bin/python" -c "import timelapsed" 2>/dev/null; then
        echo "ERROR: pip reported success but timelapsed is not importable in ${INSTALL_DIR}/.venv" >&2
        exit 1
    fi
    echo "==> Reinstalling systemd units"
    sudo cp "${INSTALL_DIR}"/deploy/timelapsed*.service "${INSTALL_DIR}"/deploy/tailscale-local-subnet-route.service "${INSTALL_DIR}"/deploy/timelapsed*.timer \
        /etc/systemd/system/
    sudo systemctl daemon-reload

    # Only where nginx is already in front. Re-rendered every upgrade so an edit
    # to the checked-in template actually reaches the guest.
    if [[ -f /etc/nginx/sites-available/timelapsed ]]; then
        echo "==> Refreshing the nginx site"
        sudo "${INSTALL_DIR}/deploy/nginx-setup.sh"
    fi

    # Same pattern: only where the live wall's relay is already installed.
    if [[ -f /etc/systemd/system/go2rtc.service ]]; then
        echo "==> Refreshing go2rtc"
        sudo "${INSTALL_DIR}/deploy/go2rtc-setup.sh"
    fi
fi

echo "==> Restarting"
UNITS=(timelapsed timelapsed-web)
# The analyzer is optional: it is not installed on a capture-only guest, and
# where [analysis] is off it exits 0 and is meant to stay stopped. Restarting it
# unconditionally would fail the upgrade on both. It does have to be restarted
# where it is wanted -- it is the only writer of the recognition index, so it is
# also the only process that migrates it.
if systemctl is-enabled --quiet timelapsed-analyzer 2>/dev/null; then
    UNITS+=(timelapsed-analyzer)
fi
sudo systemctl restart "${UNITS[@]}"
systemctl --no-pager --lines=0 status "${UNITS[@]}"
