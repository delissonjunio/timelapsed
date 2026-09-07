#!/usr/bin/env bash
# Off-site copy of the footage archive to Backblaze B2.
#
# Runs from timelapsed-offsite.timer, hourly, as root -- the key file is
# root-only, like the New Relic one. Both phases are `rclone copy`, never
# `sync`: the archiver's reclaim deletes the oldest local days when the volume
# hits its floor, and the whole point of this copy is that those days survive.
#
#  1. Backfill, oldest day first across every channel -- the same order
#     reclaim deletes in, so the days nearest the axe are the first ones safe.
#     A day that copied cleanly and is not today is recorded in days.done and
#     never re-listed, which is what keeps the steady state cheap.
#  2. One whole-tree pass, which is the steady state: today's tail, plus late
#     arrivals into old days (the archiver fetches NVR history oldest-first
#     too, so an old day can grow after the fact).
#
# Progress goes to .offsite-status.json beside the archive after every day and
# at the end of the run; the status page reads it (system_status.py names the
# same file). `timelapsed-offsite.sh rclone <args>` runs rclone with the same
# credentials and remote name, for restores and spot checks:
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

DONE="$STATE_DIR/days.done"
touch "$DONE"

# Segments land by rename, so nothing here is ever half-written; --min-age is
# belt and braces. Dotfiles are the daemons' own status and write-off files.
# The bandwidth cap is a timetable: the home uplink is shared by day.
RCLONE=(
    rclone copy
    --transfers 4 --checkers 8 --fast-list --min-age 2m
    --exclude '.*' --exclude '*.tmp' --exclude 'lost+found/**'
    --bwlimit "07:00,8M 23:00,14M"
    --retries 3 --low-level-retries 10
    --log-level NOTICE --stats 15m --stats-one-line --stats-log-level NOTICE
)

STARTED=$(date -u +%s)
RUN_STARTED_ISO=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)

write_status() {  # write_status STATE PHASE DAYS_DONE DAYS_TOTAL ERRORS [OBJECTS BYTES]
    local tmp="$ROOT/$STATUS_FILENAME.tmp"
    printf '{"written_at": "%s", "remote": "%s", "state": "%s", "phase": "%s", "days_done": %s, "days_total": %s, "errors": %s, "run_started_at": "%s", "run_seconds": %s, "remote_objects": %s, "remote_bytes": %s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" "$REMOTE" "$1" "$2" "$3" "$4" "$5" \
        "$RUN_STARTED_ISO" "$(( $(date -u +%s) - STARTED ))" "${6:-null}" "${7:-null}" > "$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$ROOT/$STATUS_FILENAME"
}

# Every channel/YYYYMMDD directory, oldest day first regardless of channel.
TODAY=$(date -u +%Y%m%d)
mapfile -t DAYS < <(
    find "$ROOT" -mindepth 2 -maxdepth 2 -type d -regextype posix-extended -regex '.*/[0-9]{8}$' -printf '%P\n' \
    | awk -F/ '{ print $2 "/" $1 }' | sort | awk -F/ '{ print $2 "/" $1 }'
)

PENDING=()
TOTAL=0
DONE_COUNT=0
for day in "${DAYS[@]}"; do
    [[ ${day##*/} < $TODAY ]] || continue   # today's directories belong to the tail pass
    TOTAL=$((TOTAL + 1))
    if grep -qxF "$day" "$DONE"; then
        DONE_COUNT=$((DONE_COUNT + 1))
    else
        PENDING+=("$day")
    fi
done
PHASE=steady
if (( ${#PENDING[@]} )); then
    PHASE=backfill
fi

ERRORS=0
write_status running "$PHASE" "$DONE_COUNT" "$TOTAL" "$ERRORS"

for day in "${PENDING[@]}"; do
    echo "backfill: $day ($((DONE_COUNT + 1))/$TOTAL)"
    if "${RCLONE[@]}" "$ROOT/$day" "$REMOTE/$day"; then
        echo "$day" >> "$DONE"
        DONE_COUNT=$((DONE_COUNT + 1))
    else
        ERRORS=$((ERRORS + 1))
        echo "backfill: $day failed; continuing" >&2
    fi
    write_status running "$PHASE" "$DONE_COUNT" "$TOTAL" "$ERRORS"
done
if (( DONE_COUNT >= TOTAL )); then
    PHASE=steady
fi

echo "tail: whole tree"
if ! "${RCLONE[@]}" "$ROOT" "$REMOTE"; then
    ERRORS=$((ERRORS + 1))
fi

SIZE=$(rclone size --json --fast-list "$REMOTE" 2>/dev/null || echo '{}')
OBJECTS=$(sed -nE 's/.*"count": *([0-9]+).*/\1/p' <<<"$SIZE")
BYTES=$(sed -nE 's/.*"bytes": *([0-9]+).*/\1/p' <<<"$SIZE")
STATE=ok
if (( ERRORS )); then
    STATE=failed
fi
write_status "$STATE" "$PHASE" "$DONE_COUNT" "$TOTAL" "$ERRORS" "${OBJECTS:-null}" "${BYTES:-null}"
echo "done: state=$STATE phase=$PHASE days=$DONE_COUNT/$TOTAL errors=$ERRORS objects=${OBJECTS:-?} bytes=${BYTES:-?}"
(( ERRORS == 0 ))
