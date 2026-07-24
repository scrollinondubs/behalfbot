#!/usr/bin/env bash
# test-container-liveness.sh - behavioural tests for gather-container-liveness.sh.
#
# Per the "checks that cannot fail" rule (#119 acceptance): prove the check
# CAN fire before trusting it. These tests stub `docker` on PATH (no daemon,
# no containers) and FORCE the behalfbot-down / restart-loop states, asserting
# the gather alerts - then assert it stays SILENT when every container is up.
#
# Scenarios:
#   1. all containers running                 -> silent
#   2. behalfbot exited                        -> alerts (behalfbot_down_exited)
#   3. behalfbot State.Restarting == true      -> alerts (behalfbot_restarting)
#   4. behalfbot RestartCount rose since tick   -> alerts (behalfbot_restart_loop)
#   5. behalfbot absent (inspect fails)         -> alerts (behalfbot_absent)
#   6. postgres unhealthy healthcheck           -> alerts (behalfbot-postgres_unhealthy)
#   7. docker unreachable                       -> silent no-op (docker_unreachable)
#
# The docker stub reads a per-container fixture from $STUB_STATE/<name>, whose
# contents are the exact pipe-delimited string the real
# `docker inspect -f '...'` would print. Absent fixture => inspect exits 1
# (container absent). `docker info` succeeds unless $STUB_STATE/__no_docker
# exists.
#
# Exit 0 all pass, 1 on failure, 2 on harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATHER="${SCRIPT_DIR}/gather-container-liveness.sh"

if [[ ! -x "$GATHER" ]]; then
    echo "test-container-liveness: gather not executable at $GATHER" >&2
    exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "test-container-liveness: jq required" >&2
    exit 2
fi

fail=0
pass=0

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

STUB_BIN="$TMP/bin"
STUB_STATE="$TMP/dockerstate"
mkdir -p "$STUB_BIN" "$STUB_STATE"
ORIGINAL_PATH="$PATH"

# Install the docker stub. It emulates just the two invocations the gather
# uses: `docker info` and `docker inspect -f '<tmpl>' <name>`.
cat > "$STUB_BIN/docker" <<'STUB'
#!/bin/bash
state="${STUB_DOCKER_STATE:?}"
case "$1" in
    info)
        [[ -e "$state/__no_docker" ]] && exit 1
        exit 0
        ;;
    inspect)
        # Last arg is the container name; a fixture file must exist for it.
        name="${@: -1}"
        if [[ -f "$state/$name" ]]; then
            cat "$state/$name"
            exit 0
        fi
        echo "Error: No such object: $name" >&2
        exit 1
        ;;
esac
exit 0
STUB
chmod +x "$STUB_BIN/docker"

# Reset fixtures between scenarios.
reset_state() {
    rm -rf "$STUB_STATE"
    mkdir -p "$STUB_STATE"
}

# Plant a container fixture: name -> "status|restarting|restartcount|health".
set_container() {
    printf '%s' "$2" > "$STUB_STATE/$1"
}

# Run the gather with the docker stub on PATH and a scratch state file.
run_gather() {
    local statefile="$TMP/liveness-state-$RANDOM.json"
    PATH="$STUB_BIN:$ORIGINAL_PATH" \
    STUB_DOCKER_STATE="$STUB_STATE" \
    CHASSIS_LIVENESS_STATE="$statefile" \
    CHASSIS_LIVENESS_CONTAINERS="behalfbot,behalfbot-postgres" \
    CUSTOMER_HOME="$TMP" CHASSIS_HOME="$TMP" \
        bash "$GATHER" 2>/dev/null
    echo "$statefile"   # last line = state file path, for loop tests
}

# run_gather but return only stdout JSON (strip the trailing state-file line).
gather_json() {
    run_gather | sed '$d'
}

assert_case() {
    local name="$1" out="$2" want_count="$3" want_tag="${4:-}" want_status="${5:-}"
    local got_count got_tag got_status
    got_count=$(printf '%s' "$out" | jq -r '.count' 2>/dev/null)
    if [[ "$got_count" != "$want_count" ]]; then
        echo "FAIL [$name] expected count=$want_count, got count=$got_count :: $out"
        fail=$((fail + 1)); return
    fi
    if [[ -n "$want_tag" ]]; then
        got_tag=$(printf '%s' "$out" | jq -r --arg t "$want_tag" '.issues | index($t) // "MISSING"' 2>/dev/null)
        if [[ "$got_tag" == "MISSING" ]]; then
            echo "FAIL [$name] expected issues to contain '$want_tag', got :: $out"
            fail=$((fail + 1)); return
        fi
    fi
    if [[ -n "$want_status" ]]; then
        got_status=$(printf '%s' "$out" | jq -r '.status' 2>/dev/null)
        if [[ "$got_status" != "$want_status" ]]; then
            echo "FAIL [$name] expected status=$want_status, got status=$got_status :: $out"
            fail=$((fail + 1)); return
        fi
    fi
    pass=$((pass + 1))
}

# --- 1. all running -> silent -----------------------------------------------
reset_state
set_container "behalfbot"          "running|false|0|none"
set_container "behalfbot-postgres" "running|false|0|healthy"
assert_case "all_running_silent" "$(gather_json)" 0 "" "ok"

# --- 2. behalfbot exited -> alerts ------------------------------------------
reset_state
set_container "behalfbot"          "exited|false|3|none"
set_container "behalfbot-postgres" "running|false|0|healthy"
assert_case "behalfbot_exited_alerts" "$(gather_json)" 1 "behalfbot_down_exited"

# --- 3. behalfbot restarting -> alerts --------------------------------------
reset_state
set_container "behalfbot"          "running|true|7|none"
set_container "behalfbot-postgres" "running|false|0|healthy"
assert_case "behalfbot_restarting_alerts" "$(gather_json)" 1 "behalfbot_restarting"

# --- 4. behalfbot restart-loop (RestartCount rises between ticks) -----------
# First tick primes the state at count=5 (should be silent - no prior). Second
# tick sees count=6 with the SAME state file -> restart_loop.
reset_state
statefile="$TMP/loop-state.json"
set_container "behalfbot"          "running|false|5|none"
set_container "behalfbot-postgres" "running|false|0|healthy"
PATH="$STUB_BIN:$ORIGINAL_PATH" STUB_DOCKER_STATE="$STUB_STATE" \
    CHASSIS_LIVENESS_STATE="$statefile" CHASSIS_LIVENESS_CONTAINERS="behalfbot,behalfbot-postgres" \
    CUSTOMER_HOME="$TMP" CHASSIS_HOME="$TMP" bash "$GATHER" >/dev/null 2>&1
set_container "behalfbot"          "running|false|6|none"
out=$(PATH="$STUB_BIN:$ORIGINAL_PATH" STUB_DOCKER_STATE="$STUB_STATE" \
    CHASSIS_LIVENESS_STATE="$statefile" CHASSIS_LIVENESS_CONTAINERS="behalfbot,behalfbot-postgres" \
    CUSTOMER_HOME="$TMP" CHASSIS_HOME="$TMP" bash "$GATHER" 2>/dev/null)
assert_case "behalfbot_restart_loop_alerts" "$out" 1 "behalfbot_restart_loop"

# --- 5. behalfbot absent -> alerts ------------------------------------------
reset_state
set_container "behalfbot-postgres" "running|false|0|healthy"   # behalfbot fixture omitted
assert_case "behalfbot_absent_alerts" "$(gather_json)" 1 "behalfbot_absent"

# --- 6. postgres unhealthy -> alerts ----------------------------------------
reset_state
set_container "behalfbot"          "running|false|0|none"
set_container "behalfbot-postgres" "running|false|0|unhealthy"
assert_case "postgres_unhealthy_alerts" "$(gather_json)" 1 "behalfbot-postgres_unhealthy"

# --- 7. docker unreachable -> silent no-op ----------------------------------
reset_state
touch "$STUB_STATE/__no_docker"
assert_case "docker_unreachable_silent" "$(gather_json)" 0 "" "docker_unreachable"

# ---------------------------------------------------------------------------
echo "----"
echo "PASS: $pass  FAIL: $fail"
[[ $fail -eq 0 ]] || exit 1
exit 0
