#!/bin/bash
# gather-second-brain-map-drift.sh - does the second-brain map still describe
# the second brain?
#
# Gather-first, per the dispatcher contract: free on every tick, prints a JSON
# array. [] means the map agrees with the first-level docs under the configured
# roots (or no map is configured) and no model is woken. Each element names one
# doc and which way it drifted: missing_from_map, stale_ref_in_map, or
# probe_failed - a check that cannot run must say so rather than report all
# clear. Logic: chassis/scripts/second-brain-map.py. Setup and heartbeat block:
# chassis/docs/second-brain-map.md.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo '[{"kind":"probe_failed","detail":"python3 not installed; map drift cannot be checked"}]'
    exit 0
fi

OUT="$(python3 "${HERE}/second-brain-map.py" drift 2>/dev/null)"
RC=$?
if [[ $RC -ne 0 || -z "$OUT" ]]; then
    printf '[{"kind":"probe_failed","detail":"second-brain-map.py drift exited %s"}]\n' "$RC"
    exit 0
fi
printf '%s\n' "$OUT"
