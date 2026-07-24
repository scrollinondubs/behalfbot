#!/usr/bin/env bash
# gather-chassis-root-health.sh - scheduled health check for the boot-time
# chassis-root resolution introduced by #118 (resolve-chassis-root.sh).
#
# The gap this closes
# ===================
# #118 resolves the effective chassis tree at container boot and records the
# outcome in $CUSTOMER_HOME/chassis-root.state.json. Its smoke-test check
# (check_chassis_root_resolution) runs ONCE, at boot / on demand - nothing
# re-evaluates it on a schedule. Two failure modes can therefore go dark for
# days, exactly the "SiYuan MCP stale-config, silent, no alert fired" defect
# class (a check that exists but never fires is not monitoring):
#
#   1. Silent staleness - the runtime resolved `baked` while a USABLE live
#      tree now exists at $CUSTOMER_HOME/chassis/chassis. This is the drift
#      #118 exists to prevent. It re-appears when the mount was absent at
#      boot (resolver picked baked) and the operator's live tree later
#      becomes available (mount restored, clone landed) without a restart -
#      the state file still says `baked` while a live tree sits unused.
#   2. Loud-fail / assertion - the resolver hit an exit-5 assertion (torn
#      live tree, MAJOR version skew, or symlink materialisation failure).
#      #118 records this in the state file's `error` field; surface it.
#
# This gather re-reads the state file every tick (pure file read + a live-tree
# usability probe - no docker, no network, near-free) and fires the dispatcher
# threshold when either condition holds.
#
# State file schema (written by resolve-chassis-root.sh, #118):
#   {
#     "schema": 1,
#     "mode": "explicit" | "live" | "baked",
#     "resolved_root": "<path>",
#     "baked_root": "<path>" | null,
#     "live_root": "<path>" | null,
#     "baked_version": "<x.y.z>" | null,
#     "live_version": "<x.y.z>" | null,
#     "resolved_at": "<iso8601>",
#     "error": "<string>" | null
#   }
#
# Graceful degradation: on a pre-#118 install the state file is ABSENT. That
# is "unknown", NOT a failure - emit count=0 with an info status so no false
# alarm fires. A resolver that never ran cannot have drifted.
#
# Gather JSON contract:
#   { "count": N, "issues": [...], "mode": "...", "resolved_root": "...",
#     "baked_version": "...", "live_version": "...", "status": "..." }
#
# Issue tags:
#   chassis_root_stale_baked        - mode=baked + usable live tree present +
#                                     no recorded error (the SILENT case)
#   chassis_root_assertion_failed   - state file records a non-null error
#                                     (the exit-5 / LOUD case)
#
# Emits no secrets: chassis roots are container paths and versions are semver
# strings - neither is sensitive.

set -uo pipefail

# CUSTOMER_HOME is exported by the dispatcher (heartbeat-dispatcher.sh sets
# `: "${CUSTOMER_HOME:=$CHASSIS_HOME}"; export ... CUSTOMER_HOME`). Prefer
# _env.sh's canonical resolution when it is present (#118 ships it), but do
# not hard-depend on it so this gather runs on a pre-#118 tree too.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/_env.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/_env.sh" 2>/dev/null || true
fi
: "${CUSTOMER_HOME:=${CHASSIS_HOME:-}}"

if [[ -z "$CUSTOMER_HOME" ]]; then
    # No customer root at all - cannot locate the state file. Info, not alarm.
    printf '{"count": 0, "status": "no_customer_home"}\n'
    exit 0
fi

STATE_FILE="$CUSTOMER_HOME/chassis-root.state.json"
LIVE_ROOT="${CHASSIS_LIVE_TREE_ROOT:-$CUSTOMER_HOME/chassis/chassis}"

# Pre-#118 install (or an install that has never booted the resolving
# entrypoint): no state file. Unknown, not a failure.
if [[ ! -f "$STATE_FILE" ]]; then
    printf '{"count": 0, "status": "no_state_file"}\n'
    exit 0
fi

# Defensive parse: a truncated / hand-mangled state file must not crash the
# gather (which would surface as a non-JSON line the dispatcher treats as
# count=0 anyway, but we would rather emit a clean signal).
if ! jq -e . "$STATE_FILE" >/dev/null 2>&1; then
    printf '{"count": 1, "issues": ["chassis_root_state_unparseable"], "status": "state_file_corrupt"}\n'
    exit 0
fi

mode=$(jq -r '.mode // "unknown"' "$STATE_FILE")
error=$(jq -r '.error // ""' "$STATE_FILE")
resolved_root=$(jq -r '.resolved_root // ""' "$STATE_FILE")
baked_version=$(jq -r '.baked_version // ""' "$STATE_FILE")
live_version=$(jq -r '.live_version // ""' "$STATE_FILE")

# Replicate resolve-chassis-root.sh's `usable()` guard so "a usable live tree
# exists NOW" is judged the same way the resolver would judge it at boot.
live_tree_usable() {
    local dir="$1"
    [[ -d "$dir" ]] || return 1
    [[ -s "$dir/VERSION" ]] || return 1
    [[ -d "$dir/scripts" ]] || return 1
    [[ -f "$dir/scheduled-tasks/heartbeat-dispatcher.sh" ]] || return 1
}

issues=()

# --- Condition 2: loud-fail / assertion (checked first so it takes the tag) --
# Any non-null error means the resolver hit an exit-5 assertion path (torn
# tree, MAJOR skew, or symlink failure). Surface it verbatim-ish via a tag.
if [[ -n "$error" ]]; then
    issues+=("chassis_root_assertion_failed")
fi

# --- Condition 1: silent staleness ------------------------------------------
# mode=baked with NO recorded error is the resolver's "no live tree at boot"
# branch. If a usable live tree is present NOW, the runtime is silently on the
# stale baked copy - the precise drift #118 exists to kill. Gate on error==""
# so this stays mutually exclusive with the loud-fail tag above (a torn tree
# is not "usable" and a MAJOR-skew tree already carries an error).
if [[ "$mode" == "baked" && -z "$error" ]] && live_tree_usable "$LIVE_ROOT"; then
    issues+=("chassis_root_stale_baked")
fi

count=${#issues[@]}
if [[ $count -eq 0 ]]; then
    issues_json="[]"
    status="ok"
else
    issues_json=$(printf '%s\n' "${issues[@]}" | jq -R . | jq -s .)
    status="drift"
fi

jq -n \
    --argjson count "$count" \
    --argjson issues "$issues_json" \
    --arg mode "$mode" \
    --arg resolved_root "$resolved_root" \
    --arg baked_version "$baked_version" \
    --arg live_version "$live_version" \
    --arg error "$error" \
    --arg status "$status" \
    '{
        count: $count,
        issues: $issues,
        mode: $mode,
        resolved_root: $resolved_root,
        baked_version: ($baked_version | if . == "" then null else . end),
        live_version: ($live_version | if . == "" then null else . end),
        error: ($error | if . == "" then null else . end),
        status: $status
    }'
