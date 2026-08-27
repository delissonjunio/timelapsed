#!/usr/bin/env bash
#
# Puts nginx in front of the viewer, so that rendered videos come off the disk
# instead of through Python. See the header of deploy/nginx-timelapsed.conf for
# what that is worth and what it is not.
#
# Run as root ON THE GUEST:
#
#   sudo bash deploy/nginx-setup.sh
#
# Idempotent. `install.sh --with-nginx` calls this, and once the site exists
# `update.sh` re-renders it on every upgrade so a change to the checked-in
# config template reaches the guest.
#
# The port swap it performs: nginx takes over whatever port the viewer was
# published on (8080 by default, and what Tailscale Serve and the firewall rules
# already point at), and the viewer moves one port up on 127.0.0.1, where only
# nginx can reach it. Override either with LISTEN_PORT= / UPSTREAM_PORT=.

set -euo pipefail

CONFIG_PATH=${CONFIG_PATH:-/etc/timelapsed.ini}
SERVICE_USER=${SERVICE_USER:-timelapsed}
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)

SITE_AVAILABLE=/etc/nginx/sites-available/timelapsed
SITE_ENABLED=/etc/nginx/sites-enabled/timelapsed

if [[ $EUID -ne 0 ]]; then
    echo "error: run this as root (sudo bash deploy/nginx-setup.sh)" >&2
    exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo "error: no config at ${CONFIG_PATH}; run deploy/install.sh first" >&2
    exit 1
fi

# One value out of an ini section. Only good enough for these keys: it takes the
# first `=` as the separator and does not understand continuations.
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

# nginx needs the real library root baked into the site file, so read it from
# the config rather than assuming the default. An upgrade re-renders the site,
# so moving the library and re-running update.sh is enough to follow it.
LIBRARY_DIR=${LIBRARY_DIR:-$(ini_value image_capture_library root)}
if [[ -z "${LIBRARY_DIR}" ]]; then
    echo "error: no [image_capture_library] root in ${CONFIG_PATH}" >&2
    exit 1
fi

# Whatever the viewer is bound to right now. On a first run that is the public
# port nginx is about to take; on a re-run it is already the private one.
CONFIGURED_PORT=$(ini_value web port)

if [[ -z "${LISTEN_PORT:-}" ]]; then
    if [[ -f "${SITE_AVAILABLE}" ]]; then
        # A re-run must keep publishing where it already publishes, rather than
        # silently reverting a customised port back to the default.
        LISTEN_PORT=$(awk '$1 == "listen" && $2 ~ /^[0-9]+;$/ { sub(/;/, "", $2); print $2; exit }' "${SITE_AVAILABLE}")
    else
        LISTEN_PORT=${CONFIGURED_PORT}
    fi
fi
LISTEN_PORT=${LISTEN_PORT:-8080}

if [[ -z "${UPSTREAM_PORT:-}" ]]; then
    if [[ -f "${SITE_AVAILABLE}" ]]; then
        UPSTREAM_PORT=${CONFIGURED_PORT}
    else
        UPSTREAM_PORT=$((LISTEN_PORT + 1))
    fi
fi
UPSTREAM_PORT=${UPSTREAM_PORT:-8081}

if [[ "${LISTEN_PORT}" == "${UPSTREAM_PORT}" ]]; then
    echo "error: nginx and the viewer cannot both have port ${LISTEN_PORT}" >&2
    exit 1
fi

if ! command -v nginx >/dev/null 2>&1; then
    echo "==> Installing nginx"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq nginx
fi

echo "==> Rendering ${SITE_AVAILABLE}"
sed -e "s|__LIBRARY_ROOT__|${LIBRARY_DIR%/}|g" \
    -e "s|__LISTEN_PORT__|${LISTEN_PORT}|g" \
    -e "s|__UPSTREAM_PORT__|${UPSTREAM_PORT}|g" \
    "${REPO_DIR}/deploy/nginx-timelapsed.conf" > "${SITE_AVAILABLE}"

# directio hands the worker a blocking read, so it is only safe paired with a
# thread pool. Without --with-threads, drop both and fall back to sendfile.
if ! nginx -V 2>&1 | grep -q -- '--with-threads'; then
    echo "    nginx was built without --with-threads; using sendfile instead of directio"
    sed -i -E 's/^([[:space:]]*)(aio threads;|directio 16m;)/\1# \2/' "${SITE_AVAILABLE}"
fi

ln -sfn "${SITE_AVAILABLE}" "${SITE_ENABLED}"

# The stock welcome page listens on :80 and would be the only thing on this
# guest answering outside the viewer's port. Restore with:
#   ln -s ../sites-available/default /etc/nginx/sites-enabled/default
if [[ "$(readlink /etc/nginx/sites-enabled/default 2>/dev/null)" == *sites-available/default ]]; then
    echo "==> Disabling the stock nginx welcome site on :80"
    rm -f /etc/nginx/sites-enabled/default
fi

NGINX_USER=$(awk '$1 == "user" { sub(/;/, "", $2); print $2; exit }' /etc/nginx/nginx.conf)
NGINX_USER=${NGINX_USER:-www-data}

# Belt and braces: the library is world-readable as installed, so this only
# matters if it has since been tightened. A recursive chmod is deliberately not
# done here — the library holds a hundred thousand stills.
if ! id -nG "${NGINX_USER}" | tr ' ' '\n' | grep -qx "${SERVICE_USER}"; then
    echo "==> Adding ${NGINX_USER} to the ${SERVICE_USER} group"
    usermod -aG "${SERVICE_USER}" "${NGINX_USER}"
fi

echo "==> Pointing the viewer at 127.0.0.1:${UPSTREAM_PORT}, nginx at ${LISTEN_PORT}"
if grep -q '^\[web\]' "${CONFIG_PATH}"; then
    # Only inside [web]: `port` is a plausible key name in other sections.
    (umask 077 && awk -v upstream="${UPSTREAM_PORT}" '
        /^\[/ { section = $0 }
        section == "[web]" && /^[[:space:]]*host[[:space:]]*=/ { print "host = 127.0.0.1"; next }
        section == "[web]" && /^[[:space:]]*port[[:space:]]*=/ { print "port = " upstream; next }
        { print }
    ' "${CONFIG_PATH}" > "${CONFIG_PATH}.nginx-tmp")
    mv "${CONFIG_PATH}.nginx-tmp" "${CONFIG_PATH}"
else
    printf '\n[web]\nhost = 127.0.0.1\nport = %s\n' "${UPSTREAM_PORT}" >> "${CONFIG_PATH}"
fi
# mv replaced the file, so the NVR password needs its mode back.
chown root:"${SERVICE_USER}" "${CONFIG_PATH}"
chmod 640 "${CONFIG_PATH}"

# The viewer has to let go of ${LISTEN_PORT} before nginx can bind it.
systemctl try-restart timelapsed-web

echo "==> Checking the nginx configuration"
nginx -t

# restart, not reload: a reload keeps the old worker's supplementary groups.
systemctl enable nginx
systemctl restart nginx

# One real render, read as nginx will read it. A permissions mistake here shows
# up as every video quietly falling back to Python, which is easy to miss.
SAMPLE=$(find "${LIBRARY_DIR}" -mindepth 3 -maxdepth 3 -path '*/timelapse/*.mp4' -print -quit 2>/dev/null || true)
if [[ -n "${SAMPLE}" ]] && ! runuser -u "${NGINX_USER}" -- test -r "${SAMPLE}"; then
    cat <<MESSAGE >&2

warning: ${NGINX_USER} cannot read ${SAMPLE}, so nginx will fall back to the
viewer for every video and you will get none of the benefit. Fix with:

  sudo chmod -R g+rX ${LIBRARY_DIR}

MESSAGE
fi

echo
echo "nginx is serving the viewer on port ${LISTEN_PORT}; videos come off the disk."
echo "Check: curl -sI localhost:${LISTEN_PORT}/healthz"
