#!/usr/bin/env bash
#
# Local development launcher.
#
# Configuration lives in an INI file, NOT in environment variables.
# Copy timelapsed.ini.example to ~/.timelapsed.ini and fill it in.
# See docs/Configuration.md for every key.

set -euo pipefail

if [[ ! -f "${HOME}/.timelapsed.ini" && ! -f /etc/timelapsed.ini && ! -f ./timelapsed.ini ]]; then
    echo "error: no config found. Copy timelapsed.ini.example to ~/.timelapsed.ini first." >&2
    exit 1
fi

exec python -m timelapsed
