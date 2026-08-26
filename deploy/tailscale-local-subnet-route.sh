#!/usr/bin/env bash
#
# Keep the host's own LAN off the Tailscale route table. See the unit file of
# the same name for why this is needed. Called as `... add` or `... del`.

set -euo pipefail

PRIORITY=5260

default_interface=$(ip -4 route show default | awk '{print $5; exit}')
[[ -n "${default_interface}" ]] || exit 0

subnet=$(ip -4 -o route show scope link dev "${default_interface}" | awk '{print $1; exit}')
[[ -n "${subnet}" ]] || exit 0

# Always clear first so this is idempotent and never stacks duplicate rules.
while ip rule del to "${subnet}" lookup main priority "${PRIORITY}" 2>/dev/null; do :; done

if [[ "${1:-add}" == "add" ]]; then
    ip rule add to "${subnet}" lookup main priority "${PRIORITY}"
    echo "Routing ${subnet} via the main table (priority ${PRIORITY})"
fi
