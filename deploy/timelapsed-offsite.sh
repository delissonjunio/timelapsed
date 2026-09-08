#!/usr/bin/env bash
# Off-site copy of the footage archive to Backblaze B2.
#
# Runs from timelapsed-offsite.timer, hourly, as root -- the key file is
# root-only, like the New Relic one. Always `rclone copy`, never `sync`: the
# archiver's reclaim deletes the oldest local days when the volume hits its
# floor, and the whole point of this copy is that those days survive.
#
# B2 bills every listing, authorisation and upload-URL fetch as a Class C
# transaction -- 2,500 a day free, cents per thousand after, and a daily cap
# in the console that refuses everything once reached -- while the uploads
# themselves are free. So the shape here is "as few invocations and listings
# as possible", not "as fresh as possible":
#
#  * A FULL pass lists both sides once (`--fast-list`: one call per thousand
#    objects) and copies whatever is missing, channel by channel in date
#    order. The first one is the backfill; after that, one a day.
#  * A TAIL pass, every other hour, lists only today's and yesterday's day
#    directories and copies what landed in them -- about forty calls.
#
# Late arrivals into old days (the archiver fetches NVR history oldest-first
# too) ride the next full pass. Progress goes to .offsite-status.json beside
# the archive -- at start, on every rclone stats line, and at the end -- and
# the status page reads it (system_status.py names the same file).
#
# `timelapsed-offsite.sh full` forces a full pass. `timelapsed-offsite.sh
# rclone <args>` runs rclone with the same credentials and remote name, for
# restores and spot checks:
#
#   sudo deploy/timelapsed-offsite.sh rclone lsd b2:timelapsed/archive/6
#   sudo deploy/timelapsed-offsite.sh rclone copy b2:timelapsed/archive/6/20260817 \
#        /var/lib/timelapsed/archive/6/20260817
#
# See docs/Operations.md, "The footage archive, off-site".
set -euo pipefail

CFG=${OFFSITE_CFG:-/etc/backblaze.cfg}
INI=${TIMELAPSED_CONFIG:-/etc/timelapsed.ini}
STATE_DIR=${OFFSITE_STATE_DIR:-/var/lib/timelapsed/offsite}
STATUS_FILENAME=.offsite-status.json
# Seconds between full passes: daily, with slack so the hourly grid catches it.
FULL_EVERY=$((20 * 3600))

cfg() {  # cfg KEY -- from the key file, whitespace stripped
    sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CFG" | head -n1 | tr -d '[:space:]'
}

ini() {  # ini SECTION KEY -- from timelapsed.ini, or nothing
    awk -v section="[$1]" -v key="$2" '
        /^\[/ { inside = ($0 == section) }
        inside && match($0, "^[[:space:]]*" key "[[:space:]]*=") {
            value = substr($0, RLENGTH + 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            print value
            exit
        }
    ' "$INI" 2>/dev/null
}

if [[ ! -r "$CFG" ]]; then
    echo "no $CFG; nothing to copy" >&2
    exit 0
fi

ROOT=$(ini archive root)
ROOT=${ROOT:-/var/lib/timelapsed/archive}
BUCKET=$(cfg bucket)
BUCKET=${BUCKET:-$(cfg keyName)}
BUCKET=${BUCKET:-timelapsed}
REMOTE="b2:${BUCKET}/archive"

# The bandwidth timetable below is wall-clock time, and "night on the uplink"
# means the household's night, not UTC's: borrow the [timelapse] timezone the
# renders already use. Everything else here stays UTC -- `date -u` throughout,
# and the day directories are UTC by the archiver's rule.
TZ=$(ini timelapse timezone)
export TZ=${TZ:-UTC}

# The remote is defined entirely by environment, so no rclone.conf is read or
# written. The cache dir moves because the unit's ProtectHome hides /root.
export RCLONE_CONFIG=/dev/null
export RCLONE_CONFIG_B2_TYPE=b2
export RCLONE_CONFIG_B2_ACCOUNT
export RCLONE_CONFIG_B2_KEY
export XDG_CACHE_HOME="$STATE_DIR/cache"
RCLONE_CONFIG_B2_ACCOUNT=$(cfg keyID)
RCLONE_CONFIG_B2_KEY=$(cfg applicationKey)
if [[ -z "$RCLONE_CONFIG_B2_ACCOUNT" || -z "$RCLONE_CONFIG_B2_KEY" ]]; then
    echo "$CFG needs keyID= and applicationKey=" >&2
    exit 1
fi

mkdir -p "$STATE_DIR" "$XDG_CACHE_HOME"

if [[ ${1:-} == rclone ]]; then
    shift
    exec rclone "$@"
fi

FULL_STAMP="$STATE_DIR/full.stamp"   # mtime: when the last full pass finished cleanly
SIZE_CACHE="$STATE_DIR/size.json"    # what the bucket held after it

PASS=tail
PHASE=steady
if [[ ! -f "$FULL_STAMP" ]]; then
    PASS=full
    PHASE=backfill
elif (( $(date +%s) - $(stat -c %Y "$FULL_STAMP") >= FULL_EVERY )); then
    PASS=full
fi
if [[ ${1:-} == full ]]; then
    PASS=full
fi

# Filters are one ordered list: the daemons' dotfiles and scratch out first,
# then, for a tail pass, only the two newest day directories in and everything
# else out. Segments land by rename, so nothing is ever half-written; --min-age
# is belt and braces. --retries 1 because a retry re-lists, and the failures
# worth retrying are the ones rclone already retries at the request level.
RCLONE=(
    rclone copy "$ROOT" "$REMOTE"
    --filter '- .*' --filter '- *.tmp' --filter '- lost+found/**'
    --min-age 2m
    --transfers 4 --checkers 8
    --bwlimit "07:00,8M 23:00,14M"
    --retries 1 --low-level-retries 10
    --log-level NOTICE --stats 10m --stats-one-line --stats-log-level NOTICE
)
if [[ $PASS == full ]]; then
    RCLONE+=(--fast-list --check-first --order-by name,ascending)
else
    RCLONE+=(
        --filter "+ /*/$(date -u +%Y%m%d)/**"
        --filter "+ /*/$(date -u -d yesterday +%Y%m%d)/**"
        --filter '- **'
    )
fi

STARTED=$(date -u +%s)
RUN_STARTED_ISO=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)

bucket_field() {  # bucket_field NAME -- from the cached `rclone size --json`, or null
    local value
    value=$(sed -nE "s/.*\"$1\": *([0-9]+).*/\1/p" "$SIZE_CACHE" 2>/dev/null || true)
    echo "${value:-null}"
}

write_status() {  # write_status STATE ERRORS [PROGRESS]
    local tmp="$ROOT/$STATUS_FILENAME.tmp" progress=null full_finished=null
    if [[ -n ${3:-} ]]; then
        progress="\"${3//[\"\\]/}\""
    fi
    if [[ -f "$FULL_STAMP" ]]; then
        full_finished="\"$(date -u -d "@$(stat -c %Y "$FULL_STAMP")" +%Y-%m-%dT%H:%M:%S+00:00)\""
    fi
    printf '{"written_at": "%s", "remote": "%s", "state": "%s", "pass": "%s", "phase": "%s", "errors": %s, "run_started_at": "%s", "run_seconds": %s, "progress": %s, "remote_objects": %s, "remote_bytes": %s, "full_finished_at": %s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" "$REMOTE" "$1" "$PASS" "$PHASE" "$2" \
        "$RUN_STARTED_ISO" "$(( $(date -u +%s) - STARTED ))" "$progress" \
        "$(bucket_field count)" "$(bucket_field bytes)" "$full_finished" > "$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$ROOT/$STATUS_FILENAME"
}

# Stream rclone's log through, and turn each of its stats lines into a status
# write, so a 30-hour backfill reports progress rather than silence.
run_rclone() {
    "$@" 2>&1 | while IFS= read -r line; do
        printf '%s\n' "$line"
        if [[ $line == *"NOTICE:"*"ETA"* ]]; then
            progress=${line#*NOTICE:}
            progress=${progress#"${progress%%[! ]*}"}
            write_status running 0 "$progress"
        fi
    done
}

ERRORS=0
write_status running 0
echo "$PASS pass: $ROOT -> $REMOTE"
if run_rclone "${RCLONE[@]}"; then
    if [[ $PASS == full ]]; then
        touch "$FULL_STAMP"
        PHASE=steady
        if rclone size --json --fast-list "$REMOTE" > "$SIZE_CACHE.tmp" 2>/dev/null; then
            mv -f "$SIZE_CACHE.tmp" "$SIZE_CACHE"
        else
            rm -f "$SIZE_CACHE.tmp"
        fi
    fi
else
    ERRORS=1
fi

STATE=ok
if (( ERRORS )); then
    STATE=failed
fi
write_status "$STATE" "$ERRORS"
echo "done: pass=$PASS state=$STATE phase=$PHASE objects=$(bucket_field count) bytes=$(bucket_field bytes)"
(( ERRORS == 0 ))
