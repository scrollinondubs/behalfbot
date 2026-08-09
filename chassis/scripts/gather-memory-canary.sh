#!/usr/bin/env bash
# gather-memory-canary.sh - daily gate for the memory-canary heartbeat (#145).
#
# Runs memory-canary.sh (write-read-verify against the memory MCP graph, with
# the read in a separate server invocation from the write) and emits
# {"count": 0} when memory is healthy, {"count": 1, ...} when it is not.
#
# EXIT SEMANTICS - read this before changing anything
# ===================================================
# The dispatcher treats a non-zero gather exit as an error AND as count=0, so
# a gather that exits non-zero on failure goes SILENT exactly when it fires.
# That is the #145 bug wearing the opposite sign: a monitor that reports
# nothing while broken. So this script ALWAYS exits 0 and carries the verdict
# in `count`. memory-canary.sh keeps the conventional 0/1 exit for humans and
# for the test suite; this wrapper converts it.
#
# Cost: two short-lived `npx @modelcontextprotocol/server-memory` spawns per
# run. That is more than the near-free gathers, which is why the heartbeat is
# daily rather than every 15m. Nothing else in the gather touches the network.
#
# Emits: {"count":N, "stage":..., "error":..., "resolved_path":..., "shape":...,
#         "entity":..., "nonce":..., "server_cmd":..., "customer_home":...,
#         "ts_utc":...}
#
# The failure fields are the point: the alert's first line has to name the
# resolved path and the exact error, not say "memory broken". A namespace
# problem is only actionable if you can see which namespace the path belongs to.

set -uo pipefail

CUSTOMER_HOME="${CUSTOMER_HOME:-${HOME}/.behalfbot}"
CANARY="${CHASSIS_HOME:-${HOME}/behalfbot}/chassis/scripts/memory-canary.sh"

# Deliberate divergence from gather-bootstrap-audit.sh, which emits count=0 with
# a note when its helper is missing. This heartbeat is criticality: critical -
# a liveness monitor that cannot run IS the alarm, and a silent count=0 here
# would recreate the sixteen days of nothing that #145 exists to end.
emit_unavailable() {
    if command -v jq >/dev/null 2>&1; then
        jq -nc --arg error "$1" --arg customer_home "$CUSTOMER_HOME" \
            --arg ts_utc "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            '{count:1, ok:false, stage:"unavailable", error:$error,
              resolved_path:"", shape:"", customer_home:$customer_home, ts_utc:$ts_utc}'
    else
        # No jq means no safe JSON encoder. Keep the message to the fixed,
        # quote-free strings this script controls rather than risk emitting
        # malformed JSON the dispatcher would drop.
        printf '{"count": 1, "ok": false, "stage": "unavailable", "error": "jq not installed; memory-canary cannot run on this install", "resolved_path": "", "shape": ""}\n'
    fi
    exit 0
}

if [[ ! -f "$CANARY" ]]; then
    emit_unavailable "memory-canary.sh not found at $CANARY; the memory liveness monitor is not running on this install"
fi
if ! command -v jq >/dev/null 2>&1; then
    emit_unavailable "jq not installed; memory-canary cannot parse .mcp.json or the MCP responses on this install"
fi

OUT="$(bash "$CANARY" --customer-home "$CUSTOMER_HOME" --json 2>/dev/null)"
RC=$?

if [[ -z "$OUT" ]] || ! printf '%s' "$OUT" | jq -e . >/dev/null 2>&1; then
    emit_unavailable "memory-canary.sh exited $RC without emitting JSON; the memory liveness monitor is not reporting"
fi

# ok:true -> count 0 (silent). Anything else -> count 1 (fire).
printf '%s' "$OUT" | jq -c 'if .ok == true then {count: 0} + . else {count: 1} + . end'
exit 0
