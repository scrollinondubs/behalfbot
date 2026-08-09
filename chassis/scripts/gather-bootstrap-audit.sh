#!/bin/bash
# gather-bootstrap-audit.sh - cheap weekly gate for the bootstrap-audit heartbeat.
#
# Runs bootstrap-audit.sh and emits {"count": N} where N is the number of
# failing checks. Dispatcher threshold of `count > 0` fires Claude to dig in
# and propose fixes; count == 0 is silent (per `feedback_no_nag_heartbeats`).
#
# The audit itself walks the 5 install gaps (HEARTBEATS.md backup row,
# customer GH remote, memory MCP, LaunchDaemons loaded, tmux session).
# See chassis/scripts/bootstrap-audit.sh for the full check matrix.

set -uo pipefail

CUSTOMER_HOME="${CUSTOMER_HOME:-${HOME}/.behalfbot}"

# Locate the audit the same way gather-memory-canary.sh locates the canary
# (#151): sibling first, then the boot-time chassis-root record, then the
# legacy host layout. `${CHASSIS_HOME}/chassis/...` only names the chassis tree
# on a host-side install; in a container CHASSIS_HOME is the customer root and
# that path does not exist, so this gather has been reporting "cannot gate" on
# every containerized install instead of running the audit.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
LEGACY_AUDIT="${CHASSIS_HOME:-${HOME}/behalfbot}/chassis/scripts/bootstrap-audit.sh"
STATE_FILE="$CUSTOMER_HOME/chassis-root.state.json"

AUDIT_SCRIPT=""
if [[ -n "$SCRIPT_DIR" && -x "$SCRIPT_DIR/bootstrap-audit.sh" ]]; then
    AUDIT_SCRIPT="$SCRIPT_DIR/bootstrap-audit.sh"
elif [[ -f "$STATE_FILE" ]] && command -v jq >/dev/null 2>&1 &&
     RESOLVED_ROOT="$(jq -r '.resolved_root // ""' "$STATE_FILE" 2>/dev/null)" &&
     [[ -n "$RESOLVED_ROOT" && -x "$RESOLVED_ROOT/scripts/bootstrap-audit.sh" ]]; then
    AUDIT_SCRIPT="$RESOLVED_ROOT/scripts/bootstrap-audit.sh"
else
    AUDIT_SCRIPT="$LEGACY_AUDIT"
fi

if [[ ! -x "$AUDIT_SCRIPT" ]]; then
    # No audit script means we can't say anything. Emit count=0 + a note,
    # so the heartbeat stays silent and the dispatcher doesn't loop on an
    # error from a missing helper.
    printf '{"count": 0, "note": "bootstrap-audit.sh not found at %s; cannot gate"}\n' "$AUDIT_SCRIPT"
    exit 0
fi

# Strip ANSI colors from audit output (heartbeat artifact + gather output
# should be plain text so the prompt template can quote it cleanly).
AUDIT_OUT="$(CUSTOMER_HOME="$CUSTOMER_HOME" bash "$AUDIT_SCRIPT" 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g')"

# Final line is "Summary: N passed, M warned, K failed". Parse K.
FAILED="$(printf '%s\n' "$AUDIT_OUT" | awk -F'[, ]+' '/^Summary:/ {for(i=1;i<=NF;i++) if($i=="failed") print $(i-1)}' | tail -1)"
[[ -z "$FAILED" ]] && FAILED=0

# Save the audit transcript to a state file for the prompt to reference
# without re-running the audit (it's not expensive but consistency matters
# - same data the threshold gate saw should be the data Claude reads).
STATE_DIR="${CUSTOMER_HOME}/state/bootstrap-audit"
mkdir -p "$STATE_DIR"
printf '%s\n' "$AUDIT_OUT" > "$STATE_DIR/latest.txt"

# Emit JSON for the dispatcher. count == failure count drives the threshold.
# transcript_path lets the prompt template read the full audit for context.
printf '{"count": %d, "transcript_path": "%s/latest.txt"}\n' "$FAILED" "$STATE_DIR"
