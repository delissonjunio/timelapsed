#!/usr/bin/env bash
#
# Puts go2rtc behind the /live page: installs the binary, renders its config
# from the NVR settings in /etc/timelapsed.ini, and starts it as a service.
#
# go2rtc pulls each camera's RTSP main stream on demand and remuxes it -- no
# transcode -- to WebRTC or MSE, which is what lets the live wall show real
# video without an ffmpeg encode per viewer. See docs/Live.md.
#
# Run as root ON THE GUEST:
#
#   sudo bash deploy/go2rtc-setup.sh
#
# Idempotent. Once the service exists, update.sh re-runs this on every upgrade
# so a change to the checked-in unit or to the rendering below reaches the
# guest. The binary is only downloaded when missing or when GO2RTC_VERSION
# moves.

set -euo pipefail

CONFIG_PATH=${CONFIG_PATH:-/etc/timelapsed.ini}
SERVICE_USER=${SERVICE_USER:-timelapsed}
GO2RTC_VERSION=${GO2RTC_VERSION:-1.9.14}
GO2RTC_BIN=/usr/local/bin/go2rtc
GO2RTC_CONF=/etc/go2rtc.yaml
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)

if [[ $EUID -ne 0 ]]; then
    echo "error: run this as root (sudo bash deploy/go2rtc-setup.sh)" >&2
    exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo "error: no config at ${CONFIG_PATH}; run deploy/install.sh first" >&2
    exit 1
fi

# --- binary -----------------------------------------------------------------

case "$(uname -m)" in
    x86_64) GO2RTC_ARCH=amd64 ;;
    aarch64) GO2RTC_ARCH=arm64 ;;
    armv7l) GO2RTC_ARCH=arm ;;
    *) echo "error: no go2rtc build for $(uname -m)" >&2; exit 1 ;;
esac

if ! "${GO2RTC_BIN}" --version 2>/dev/null | grep -qF "${GO2RTC_VERSION}"; then
    echo "==> Downloading go2rtc ${GO2RTC_VERSION} (${GO2RTC_ARCH})"
    DOWNLOAD=$(mktemp)
    trap 'rm -f "${DOWNLOAD}"' EXIT
    curl -fsSL -o "${DOWNLOAD}" \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${GO2RTC_ARCH}"
    install -m 0755 "${DOWNLOAD}" "${GO2RTC_BIN}"
fi

# --- config -----------------------------------------------------------------

# Rendered by timelapsed.go2rtc_config, which reads every [nvr]/[nvr.*] section
# and knows each device's RTSP dialect. Stream names are `ch<channel id>`; the
# /live page derives the same names from the same config, so the two lists
# cannot drift apart. The repo's venv has the package installed; fall back to
# the system python3 with PYTHONPATH for a checkout that has no venv yet.
PYTHON="${REPO_DIR}/.venv/bin/python"
[[ -x "${PYTHON}" ]] || PYTHON=python3

RENDERED=$(mktemp)
if ! CONFIG_PATH="${CONFIG_PATH}" PYTHONPATH="${REPO_DIR}" "${PYTHON}" -m timelapsed.go2rtc_config > "${RENDERED}"; then
    rm -f "${RENDERED}"
    echo "error: could not render go2rtc config from ${CONFIG_PATH}" >&2
    exit 1
fi

if ! cmp -s "${RENDERED}" "${GO2RTC_CONF}" 2>/dev/null; then
    echo "==> Rendering ${GO2RTC_CONF}"
    install -m 0640 -o root -g "${SERVICE_USER}" "${RENDERED}" "${GO2RTC_CONF}"
fi
rm -f "${RENDERED}"

# --- service ----------------------------------------------------------------

cp "${REPO_DIR}/deploy/go2rtc.service" /etc/systemd/system/go2rtc.service
systemctl daemon-reload
systemctl enable --quiet go2rtc
systemctl restart go2rtc

# Fail here, loudly, rather than when the first tile spins forever.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    if curl -fs http://127.0.0.1:1984/api > /dev/null 2>&1; then
        echo "==> go2rtc is answering on 127.0.0.1:1984"
        exit 0
    fi
done
echo "error: go2rtc did not come up; journalctl -u go2rtc" >&2
exit 1
