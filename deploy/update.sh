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
    sudo "${INSTALL_DIR}/.venv/bin/pip" install --quiet requests backoff rich python-dateutil
    echo "==> Reinstalling systemd units"
    sudo cp "${INSTALL_DIR}"/deploy/timelapsed*.service "${INSTALL_DIR}"/deploy/tailscale-local-subnet-route.service "${INSTALL_DIR}"/deploy/timelapsed*.timer \
        /etc/systemd/system/
    sudo systemctl daemon-reload
fi

echo "==> Restarting"
sudo systemctl restart timelapsed timelapsed-web
systemctl --no-pager --lines=0 status timelapsed timelapsed-web
