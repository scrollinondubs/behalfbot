#!/usr/bin/env bash
# test-swipe-host-session-lifecycle.sh - dating-swipe-host.sh runs the
# emulator only for the session, skips on low memory, pins the model, and
# records every outcome for the pipeline-health check.
#
# Self-contained: a temp CHASSIS_HOME with stubs for `claude` (records its
# args and the session env), `sleep` (skips the jitter), `memory_pressure`
# (reports MEM_FREE percent) and the install's emulator control script
# (records start/stop, fails start when EMU_FAIL=1). No emulator, no network.
#
# Run: bash plugins/dating/tests/test-swipe-host-session-lifecycle.sh

# shellcheck disable=SC2015  # pass/fail always succeed, so A && B || C is safe here

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_SH="$(cd "$SCRIPT_DIR/.." && pwd)/scripts/dating-swipe-host.sh"

command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 2; }

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# setup <name> [with_ctl] - a fresh install root; prints its path.
setup() {
    local home="$TMP/$1"
    mkdir -p "$home/scripts" "$home/scheduled-tasks" "$home/dating-context" "$home/bin"
    printf '#!/bin/bash\necho %s\n' "'{\"count\": 1}'" > "$home/scripts/gather-dating-swipe.sh"
    echo "swipe" > "$home/scheduled-tasks/dating-swipe-prompt.md"
    cat > "$home/bin/claude" <<STUB
#!/bin/bash
printf '%s\n' "\$@" > "$home/claude-args"
env | grep '^DATING_SESSION_' | sort > "$home/claude-env"
exit "\${CLAUDE_RC:-0}"
STUB
    printf '#!/bin/bash\nexit 0\n' > "$home/bin/sleep"
    cat > "$home/bin/memory_pressure" <<'STUB'
#!/bin/bash
echo "The system has 25769803776 (1572864 pages with a page size of 16384)."
echo "System-wide memory free percentage: ${MEM_FREE:-60}%"
STUB
    if [[ "${2:-}" == "with_ctl" ]]; then
        cat > "$home/scripts/dating-emulator-session.sh" <<STUB
#!/bin/bash
echo "\$1" >> "$home/emu-calls"
if [[ "\$1" == "start" && "\${EMU_FAIL:-0}" == "1" ]]; then echo "boot timeout"; exit 1; fi
echo "emulator \$1 ok"
STUB
        chmod +x "$home/scripts/dating-emulator-session.sh"
    fi
    chmod +x "$home/scripts/gather-dating-swipe.sh" "$home/bin/"*
    printf '%s\n' "$home"
}

run_host() {
    local home="$1"
    shift
    env -u CUSTOMER_HOME -u DATING_SWIPE_MODEL -u DATING_MIN_FREE_PCT -u DATING_EMULATOR_CTL \
        CHASSIS_HOME="$home" PATH="$home/bin:$PATH" "$@" bash "$HOST_SH" 2 >/dev/null 2>&1
}

log_of() { cat "$1"/logs/scheduled/*-dating-swipe-2.log 2>/dev/null; }
state() { jq -r "$2" "$1/scheduled-tasks/dating-session-state.json" 2>/dev/null; }

echo "dating-swipe-host.sh session lifecycle"

home=$(setup normal with_ctl)
run_host "$home"
[[ "$(tr '\n' ' ' < "$home/emu-calls" 2>/dev/null)" == "start stop " ]] \
    && pass "emulator started before and stopped after the session" || fail "emulator calls: $(cat "$home/emu-calls" 2>/dev/null)"
grep -qx -- '--model' "$home/claude-args" && grep -qx 'claude-opus-4-7' "$home/claude-args" \
    && pass "claude gets a pinned --model" || fail "no pinned model in claude args"
grep -q '^DATING_SESSION_SLOT=2$' "$home/claude-env" && grep -q '^DATING_SESSION_MODEL=claude-opus-4-7$' "$home/claude-env" \
    && grep -q '^DATING_SESSION_ID=' "$home/claude-env" \
    && pass "session id, slot and model exported to the session" || fail "session env missing: $(cat "$home/claude-env" 2>/dev/null)"
[[ "$(state "$home" '.last_session.outcome')" == "ran" && "$(state "$home" '.last_ran.slot')" == "2" ]] \
    && pass "state records outcome=ran and last_ran" || fail "state after a run: $(cat "$home/scheduled-tasks/dating-session-state.json" 2>/dev/null)"

home=$(setup override with_ctl)
run_host "$home" DATING_SWIPE_MODEL=claude-test-model
grep -qx 'claude-test-model' "$home/claude-args" && pass "DATING_SWIPE_MODEL overrides the pin" || fail "model override ignored"

home=$(setup lowmem with_ctl)
run_host "$home" MEM_FREE=20
[[ ! -f "$home/claude-args" ]] && pass "low memory: claude not invoked" || fail "low memory: claude ran"
[[ ! -f "$home/emu-calls" ]] && pass "low memory: emulator never started" || fail "low memory: emulator touched"
log_of "$home" | grep -q "skip: memory too tight - 20% free, need 35%" \
    && pass "low memory: skip logged with numbers" || fail "low memory: no skip line"
[[ "$(state "$home" '.last_session.outcome')" == "skipped_memory" && "$(state "$home" '.last_ran')" == "null" ]] \
    && pass "low memory: recorded as skipped, last_ran untouched" || fail "low memory state wrong"

home=$(setup threshold with_ctl)
run_host "$home" MEM_FREE=20 DATING_MIN_FREE_PCT=15
[[ -f "$home/claude-args" ]] && pass "DATING_MIN_FREE_PCT lowers the bar" || fail "threshold override ignored"

home=$(setup emufail with_ctl)
run_host "$home" EMU_FAIL=1
[[ ! -f "$home/claude-args" ]] && pass "emulator start failure: claude not invoked" || fail "emulator start failure: claude ran"
[[ "$(tr '\n' ' ' < "$home/emu-calls")" == "start stop " ]] \
    && pass "emulator start failure: half-started emulator still stopped" || fail "emulator start failure calls: $(cat "$home/emu-calls")"
[[ "$(state "$home" '.last_session.outcome')" == "emulator_failed" ]] \
    && pass "emulator start failure: recorded" || fail "emulator start failure: state wrong"

home=$(setup noctl)
run_host "$home"
[[ -f "$home/claude-args" && ! -f "$home/emu-calls" ]] \
    && pass "no control script: always-on model, session runs untouched" || fail "no control script: unexpected behaviour"

home=$(setup claudefail with_ctl)
run_host "$home" CLAUDE_RC=1
[[ "$(state "$home" '.last_session.outcome')" == "claude_error" && "$(tr '\n' ' ' < "$home/emu-calls")" == "start stop " ]] \
    && pass "claude error: recorded, emulator still stopped" || fail "claude error handling wrong"

home=$(setup keepran with_ctl)
run_host "$home"
ran_id=$(state "$home" '.last_ran.session_id')
run_host "$home" MEM_FREE=5
[[ "$(state "$home" '.last_ran.session_id')" == "$ran_id" && "$(state "$home" '.last_session.outcome')" == "skipped_memory" ]] \
    && pass "a skip after a run keeps last_ran" || fail "last_ran lost after a skip"

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
