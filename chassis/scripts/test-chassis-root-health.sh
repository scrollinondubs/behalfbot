#!/usr/bin/env bash
# test-chassis-root-health.sh - behavioural tests for gather-chassis-root-health.sh.
#
# Per the "checks that cannot fail" rule (#119 acceptance): a monitor is
# worthless until you have SEEN it fire. These tests FORCE each alert
# condition and assert the gather reports it, and assert it stays SILENT in
# every healthy / unknown state so it cannot cry wolf.
#
# Scenarios:
#   1. mode=baked + usable live tree present + no error -> alerts (stale_baked)
#   2. state records an exit-5 error                    -> alerts (assertion_failed)
#   3. state file ABSENT (pre-#118 install)             -> silent, no false alarm
#   4. healthy mode=live, no error                      -> silent
#   5. mode=baked, NO live tree present                 -> silent (baked is correct)
#   6. explicit operator override                       -> silent (deliberate)
#   7. corrupt / unparseable state file                 -> alerts (state_unparseable)
#
# No docker, no network - pure temp dirs + jq. Exit 0 all pass, 1 on failure,
# 2 on harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATHER="${SCRIPT_DIR}/gather-chassis-root-health.sh"

if [[ ! -x "$GATHER" ]]; then
    echo "test-chassis-root-health: gather not executable at $GATHER" >&2
    exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "test-chassis-root-health: jq required" >&2
    exit 2
fi

fail=0
pass=0

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Build a customer home with an optional usable live chassis tree.
# $1 = customer home dir, $2 = "live" to plant a usable live tree.
make_home() {
    local home="$1" want_live="${2:-}"
    mkdir -p "$home/scheduled-tasks"
    if [[ "$want_live" == "live" ]]; then
        local live="$home/chassis/chassis"
        mkdir -p "$live/scripts" "$live/scheduled-tasks"
        echo "0.3.0" > "$live/VERSION"
        echo '#!/bin/bash' > "$live/scheduled-tasks/heartbeat-dispatcher.sh"
    fi
}

write_state() {
    # $1 home, $2 raw JSON (or literal string for the corrupt case)
    printf '%s\n' "$2" > "$1/chassis-root.state.json"
}

# Run the gather with CUSTOMER_HOME set to $1 and echo its stdout.
run_gather() {
    CUSTOMER_HOME="$1" CHASSIS_HOME="$1" \
        bash "$GATHER" 2>/dev/null
}

# Assert the gather output's .count and that .issues contains an expected tag
# (empty expected_tag = assert count 0 / silent).
assert_case() {
    local name="$1" out="$2" want_count="$3" want_tag="${4:-}"
    local got_count got_tag
    got_count=$(printf '%s' "$out" | jq -r '.count' 2>/dev/null)
    if [[ "$got_count" != "$want_count" ]]; then
        echo "FAIL [$name] expected count=$want_count, got count=$got_count :: $out"
        fail=$((fail + 1))
        return
    fi
    if [[ -n "$want_tag" ]]; then
        got_tag=$(printf '%s' "$out" | jq -r --arg t "$want_tag" '.issues | index($t) // "MISSING"' 2>/dev/null)
        if [[ "$got_tag" == "MISSING" ]]; then
            echo "FAIL [$name] expected issues to contain '$want_tag', got :: $out"
            fail=$((fail + 1))
            return
        fi
    fi
    pass=$((pass + 1))
}

# --- 1. Silent staleness: baked mode while a usable live tree exists ---------
H="$TMP/c1"; make_home "$H" live
write_state "$H" '{"schema":1,"mode":"baked","resolved_root":"/app/chassis","baked_root":"/app/chassis","live_root":null,"baked_version":"0.3.0","live_version":null,"resolved_at":"2026-07-24T00:00:00Z","error":null}'
assert_case "stale_baked_with_live_tree" "$(run_gather "$H")" 1 "chassis_root_stale_baked"

# --- 2. Loud-fail: exit-5 assertion recorded in error -----------------------
H="$TMP/c2"; make_home "$H" live
write_state "$H" '{"schema":1,"mode":"baked","resolved_root":"/app/chassis","baked_root":"/app/chassis","live_root":"/app/customer/chassis/chassis","baked_version":"0.3.0","live_version":"1.0.0","resolved_at":"2026-07-24T00:00:00Z","error":"live chassis v1.0.0 has a different MAJOR than baked v0.3.0 - Running BAKED."}'
assert_case "assertion_failed_major_skew" "$(run_gather "$H")" 1 "chassis_root_assertion_failed"

# --- 2b. Loud-fail: torn tree (baked + error, live tree NOT usable) ---------
H="$TMP/c2b"; make_home "$H"   # no live tree planted
mkdir -p "$H/chassis/chassis"  # dir exists but torn (no VERSION/scripts/dispatcher)
write_state "$H" '{"schema":1,"mode":"baked","resolved_root":"/app/chassis","baked_root":"/app/chassis","live_root":"/app/customer/chassis/chassis","baked_version":"0.3.0","live_version":"","resolved_at":"2026-07-24T00:00:00Z","error":"live chassis tree exists but is not usable - running BAKED"}'
assert_case "assertion_failed_torn_tree" "$(run_gather "$H")" 1 "chassis_root_assertion_failed"

# --- 3. Pre-#118 install: no state file -> silent ---------------------------
H="$TMP/c3"; make_home "$H" live   # live tree present but resolver never ran
assert_case "no_state_file_silent" "$(run_gather "$H")" 0

# --- 4. Healthy: mode=live, no error -> silent ------------------------------
H="$TMP/c4"; make_home "$H" live
write_state "$H" '{"schema":1,"mode":"live","resolved_root":"/app/customer/chassis/chassis","baked_root":"/app/chassis","live_root":"/app/customer/chassis/chassis","baked_version":"0.3.0","live_version":"0.3.0","resolved_at":"2026-07-24T00:00:00Z","error":null}'
assert_case "healthy_live_silent" "$(run_gather "$H")" 0

# --- 5. Baked with NO live tree present -> silent (baked is correct) ---------
H="$TMP/c5"; make_home "$H"   # no live tree at all
write_state "$H" '{"schema":1,"mode":"baked","resolved_root":"/app/chassis","baked_root":"/app/chassis","live_root":null,"baked_version":"0.3.0","live_version":null,"resolved_at":"2026-07-24T00:00:00Z","error":null}'
assert_case "baked_no_live_tree_silent" "$(run_gather "$H")" 0

# --- 6. Explicit operator override -> silent (deliberate choice) ------------
H="$TMP/c6"; make_home "$H" live   # live tree present but operator chose override
write_state "$H" '{"schema":1,"mode":"explicit","resolved_root":"/some/override","baked_root":null,"live_root":null,"baked_version":null,"live_version":null,"resolved_at":"2026-07-24T00:00:00Z","error":null}'
assert_case "explicit_override_silent" "$(run_gather "$H")" 0

# --- 7. Corrupt state file -> alerts (unparseable) --------------------------
H="$TMP/c7"; make_home "$H"
write_state "$H" 'this is not json {{{'
assert_case "corrupt_state_alerts" "$(run_gather "$H")" 1 "chassis_root_state_unparseable"

# ---------------------------------------------------------------------------
echo "----"
echo "PASS: $pass  FAIL: $fail"
[[ $fail -eq 0 ]] || exit 1
exit 0
