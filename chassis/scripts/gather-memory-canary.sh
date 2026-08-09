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
# Cost: three short-lived `npx @modelcontextprotocol/server-memory` spawns per
# run (clear, write, read - one tool call each, because the server serves
# pipelined requests concurrently). ~1.7s warm. That is more than the near-free
# gathers, which is why the heartbeat is daily rather than every 15m. Nothing
# else in the gather touches the network.
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

# LOCATING THE CANARY (#151)
# ==========================
# This used to be `${CHASSIS_HOME}/chassis/scripts/memory-canary.sh`. Deriving
# a chassis path from CHASSIS_HOME assumes the chassis tree sits exactly one
# level under it, which is true only on a host-side install. In a container
# CHASSIS_HOME is the CUSTOMER root (/app/customer) and the chassis tree is at
# /app/chassis (baked) or /app/customer/chassis/chassis (a clone mounted at
# $CUSTOMER_HOME/chassis, which nests its own chassis/ one level down). On both
# shapes the derived path does not exist, so the gather reported the monitor
# unavailable while the canary next to it passed.
#
# Order of resolution:
#   1. SCRIPT_DIR. The gather and the canary ship in the same directory, so a
#      sibling cannot be the wrong tree. Deliberately first even when the state
#      file names a different root: whichever tree's gather the dispatcher
#      runs, that tree's canary is the one that should run.
#   2. chassis-root.state.json, written by resolve-chassis-root.sh at boot and
#      read here exactly as gather-chassis-root-health.sh reads it. Covers an
#      invocation that copied the gather somewhere on its own.
#   3. The legacy host layout, kept so a pre-#118 install with no state file
#      still resolves.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
LEGACY_CANARY="${CHASSIS_HOME:-${HOME}/behalfbot}/chassis/scripts/memory-canary.sh"
STATE_FILE="$CUSTOMER_HOME/chassis-root.state.json"

CANARY=""
SEARCHED=()

if [[ -n "$SCRIPT_DIR" ]]; then
    SEARCHED+=("$SCRIPT_DIR/memory-canary.sh")
    [[ -f "$SCRIPT_DIR/memory-canary.sh" ]] && CANARY="$SCRIPT_DIR/memory-canary.sh"
fi

if [[ -z "$CANARY" && -f "$STATE_FILE" ]] && command -v jq >/dev/null 2>&1; then
    RESOLVED_ROOT="$(jq -r '.resolved_root // ""' "$STATE_FILE" 2>/dev/null)"
    if [[ -n "$RESOLVED_ROOT" ]]; then
        SEARCHED+=("$RESOLVED_ROOT/scripts/memory-canary.sh")
        [[ -f "$RESOLVED_ROOT/scripts/memory-canary.sh" ]] && CANARY="$RESOLVED_ROOT/scripts/memory-canary.sh"
    fi
fi

if [[ -z "$CANARY" ]]; then
    SEARCHED+=("$LEGACY_CANARY")
    [[ -f "$LEGACY_CANARY" ]] && CANARY="$LEGACY_CANARY"
fi

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

if [[ -z "$CANARY" ]]; then
    # Name every location searched, not just the last one. An unavailable
    # verdict is only actionable if the operator can see which trees were
    # considered - that is the whole lesson of #151.
    printf -v SEARCH_LIST '%s, ' "${SEARCHED[@]}"
    emit_unavailable "memory-canary.sh not found (searched: ${SEARCH_LIST%, }); the memory liveness monitor is not running on this install"
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
