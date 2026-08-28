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

# One value out of an ini section; same reader as nginx-setup.sh and good
# enough for the same reason: these keys are single-line.
ini_value() {  # section key
    awk -F= -v want_section="[$1]" -v want_key="$2" '
        /^\[/ { section = $0; next }
        section == want_section {
            key = $1; gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
            if (key == want_key) {
                sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, "")
                print; exit
            }
        }
    ' "${CONFIG_PATH}"
}

NVR_URL=$(ini_value nvr url)
NVR_USERNAME=$(ini_value nvr username)
NVR_PASSWORD=$(ini_value nvr password)
NVR_CHANNELS=$(ini_value nvr channels)

if [[ -z "${NVR_URL}" || -z "${NVR_USERNAME}" || -z "${NVR_PASSWORD}" || -z "${NVR_CHANNELS}" ]]; then
    echo "error: [nvr] url, username, password and channels are all required in ${CONFIG_PATH}" >&2
    exit 1
fi

# The snapshot URL is HTTP; RTSP wants the bare host on its own port.
NVR_HOST=$(echo "${NVR_URL}" | sed -E 's|^[a-z]+://||; s|[:/].*$||')

# Credentials go into an RTSP URL, so anything URL-significant in them has to
# be percent-encoded or the NVR sees a mangled username.
urlencode() {
    python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}
AUTH="$(urlencode "${NVR_USERNAME}"):$(urlencode "${NVR_PASSWORD}")"

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

# Stream names are `ch<channel>`; the /live page derives the same names from
# the same [nvr] channels line, so the two lists cannot drift apart.
#
# The api listens on localhost only: nginx proxies it under /go2rtc/ (see
# deploy/nginx-timelapsed.conf) and Tailscale is the authentication, exactly
# as for the viewer. WebRTC's media port has to be reachable directly -- the
# browser connects to it after the proxied signalling -- so it binds wide.
RENDERED=$(mktemp)
{
    echo "# Rendered by deploy/go2rtc-setup.sh from ${CONFIG_PATH} -- edit those, not this."
    echo "api:"
    echo "  listen: \"127.0.0.1:1984\""
    echo ""
    echo "rtsp:"
    echo "  # Local restream, for checking a camera with ffprobe from the guest."
    echo "  listen: \"127.0.0.1:8554\""
    echo ""
    echo "webrtc:"
    echo "  listen: \":8555\""
    echo ""
    echo "streams:"
    IFS=',' read -ra CHANNELS <<< "${NVR_CHANNELS}"
    for channel in "${CHANNELS[@]}"; do
        channel=$(echo "${channel}" | tr -d '[:space:]')
        [[ -z "${channel}" ]] && continue
        # Channel N's main stream is N01, same mapping as the snapshot URL.
        echo "  ch${channel}: \"rtsp://${AUTH}@${NVR_HOST}:554/Streaming/Channels/${channel}01\""
    done
} > "${RENDERED}"

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
