#!/usr/bin/env bash
# test-colima-ensure.sh - behavioural tests for colima-ensure.sh.
#
# Per the "checks that cannot fail" rule: prove the recovery CAN fire, and
# prove it stays out of the way when nothing is wrong. These tests stub both
# `colima` and `docker` (no VM, no daemon, no macOS needed - the OS probe is
# overridden), drive every branch, and assert on the exact command sequence the
# wrapper issued.
#
# Scenarios:
#    1. docker already answering            -> exit 0, colima never invoked
#    2. profile Broken                      -> stop --force FIRST, no wasted start
#    3. profile Stopped, plain start works  -> exit 0, stop --force NOT called
#    4. plain start fails (stale ha.sock)   -> stop --force + retry, recovers
#    5. start exits 0, docker never answers -> stop --force + retry, recovers
#    6. unrecoverable                       -> exit 1
#    7. host is not Darwin                  -> exit 0, colima never invoked
#    8. colima not installed                -> exit 0, says why
#   8b. docker missing                     -> exit 1, refuses to claim success
#    9. lock held by a live recent process  -> stands down, touches nothing
#   10. lock abandoned (dead pid)           -> broken, run proceeds
#   11. lock held by a LIVE but stale-aged pid -> broken, run proceeds
#       (the recycled-pid case: the postmortem's stale pid resolved to a live
#       unrelated process, so a pid-liveness check alone deadlocks forever)
#   12. no scenario ever issues `colima delete` or `colima start --reset`
#   13. source-level guard: no invocation of $COLIMA_BIN mentions delete/reset
#
# 12 and 13 are the load-bearing ones. Both of those commands destroy the VM
# disk image and every docker volume on it. A recovery script that can reach
# for them is worse than no recovery script.
#
# Exit 0 all pass, 1 on failure, 2 on harness error.
#
# Background: scrollinondubs/new-jaxity#550, docs/colima-recovery.md

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENSURE="${SCRIPT_DIR}/colima-ensure.sh"

if [[ ! -f "$ENSURE" ]]; then
    echo "test-colima-ensure: script not found at $ENSURE" >&2
    exit 2
fi

fail=0
pass=0

TMP="$(mktemp -d)"
cleanup() { rm -rf "${TMP:?}"; }
trap cleanup EXIT

STUB_BIN="$TMP/bin"
mkdir -p "$STUB_BIN"

# --- colima stub ------------------------------------------------------------
# Emulates the three subcommands the wrapper uses: `list --json`, `start` and
# `stop --force`. State markers in $STUB_STATE drive the behaviour.
#
#   status               Running | Stopped | Broken   (default Stopped)
#   absent               profile does not exist; `list --json` prints nothing
#   wedge_start_fails    start exits 1. CLEARED by stop --force.
#   wedge_no_docker      start exits 0 but leaves docker down. CLEARED by stop --force.
#   hard_start_fails     start exits 1 forever. NOT cleared - nothing recovers it.
#
# Every invocation is appended to $STUB_STATE/colima.log.
cat > "$STUB_BIN/colima" <<'STUB'
#!/bin/bash
state="${STUB_STATE:?}"
printf '%s\n' "$*" >> "$state/colima.log"
sub="$1"
case "$sub" in
    list)
        [[ -f "$state/absent" ]] && exit 0
        st="$(cat "$state/status" 2>/dev/null || echo Stopped)"
        printf '{"name":"default","status":"%s","arch":"aarch64","cpus":4,"memory":6442450944,"disk":32212254720,"runtime":"docker"}\n' "$st"
        exit 0
        ;;
    start)
        if [[ -f "$state/hard_start_fails" ]]; then
            echo "FATA[0000] error starting vm: exiting" >&2
            exit 1
        fi
        if [[ "$(cat "$state/status" 2>/dev/null)" == "Broken" ]] || [[ -f "$state/wedge_start_fails" ]]; then
            # The exact 2026-09-05 signature.
            echo 'FATA[0000] errors inspecting instance: [failed to get Info from "/Users/x/.colima/_lima/colima/ha.sock": dial unix /Users/x/.colima/_lima/colima/ha.sock: connect: connection refused]' >&2
            exit 1
        fi
        rm -f "$state/absent"
        echo Running > "$state/status"
        [[ -f "$state/wedge_no_docker" ]] || touch "$state/docker_up"
        exit 0
        ;;
    stop)
        # stop --force clears the stale lima runtime files. Non-destructive:
        # it must never affect the disk image, so nothing here touches state
        # other than the wedge markers and the status.
        touch "$state/forced"
        echo Stopped > "$state/status"
        rm -f "$state/wedge_start_fails" "$state/wedge_no_docker"
        exit 0
        ;;
esac
exit 0
STUB
chmod +x "$STUB_BIN/colima"

# --- docker stub ------------------------------------------------------------
cat > "$STUB_BIN/docker" <<'STUB'
#!/bin/bash
state="${STUB_STATE:?}"
printf '%s\n' "$*" >> "$state/docker.log"
if [[ "$1" == "ps" ]]; then
    [[ -f "$state/docker_up" ]] && exit 0
    echo "Cannot connect to the Docker daemon at unix:///Users/x/.colima/default/docker.sock. Is the docker daemon running?" >&2
    exit 1
fi
exit 0
STUB
chmod +x "$STUB_BIN/docker"

# --- minimal PATH -----------------------------------------------------------
# Scenarios 8 and 8b assert what happens when colima or docker is NOT installed,
# which means PATH must genuinely not contain them.
#
# `PATH=/usr/bin:/bin` does not achieve that. A GitHub Actions runner ships a
# real /usr/bin/docker with a live daemon behind it, so scenario 8b resolved the
# runner's own docker, `docker ps` answered, and the wrapper took its "already
# healthy" exit instead of the branch under test. It passed on macOS and in a
# bare ubuntu container, and failed only on CI.
#
# So: an explicit directory holding symlinks to exactly the externals
# colima-ensure.sh uses before it finishes resolving binaries, and nothing else.
MIN_BIN="$TMP/minbin"
mkdir -p "$MIN_BIN"
# `bash` is in the list because a `PATH=... bash "$ENSURE"` prefix assignment
# also governs the lookup of `bash` itself; without it the run exits 127 and
# the scenario proves nothing.
for tool in bash date mkdir dirname; do
    src="$(command -v "$tool" 2>/dev/null)" || { echo "test-colima-ensure: $tool not found" >&2; exit 2; }
    ln -sf "$src" "$MIN_BIN/$tool"
done
if [[ -e "$MIN_BIN/docker" || -e "$MIN_BIN/colima" ]]; then
    echo "test-colima-ensure: MIN_BIN must not contain docker or colima" >&2
    exit 2
fi

# --- harness ----------------------------------------------------------------
SCENARIO=0
STATE=""
LOCK=""

new_scenario() {
    SCENARIO=$(( SCENARIO + 1 ))
    STATE="$TMP/s${SCENARIO}"
    LOCK="$TMP/lock${SCENARIO}"
    mkdir -p "$STATE"
    : > "$STATE/colima.log"
    : > "$STATE/docker.log"
}

# run_ensure [extra env assignments...] -> sets RC
run_ensure() {
    STUB_STATE="$STATE" \
    COLIMA_BIN="$STUB_BIN/colima" \
    DOCKER_BIN="$STUB_BIN/docker" \
    COLIMA_ENSURE_OS="${OS_OVERRIDE:-Darwin}" \
    COLIMA_ENSURE_LOG="$STATE/ensure.log" \
    COLIMA_ENSURE_LOCK_DIR="$LOCK" \
    COLIMA_ENSURE_LOCK_MAX_AGE="${LOCK_MAX_AGE:-900}" \
    COLIMA_ENSURE_DOCKER_TIMEOUT=0 \
    COLIMA_ENSURE_POLL=1 \
    CUSTOMER_HOME="$STATE/customer" \
        bash "$ENSURE" >/dev/null 2>&1
    RC=$?
}

ok()  { pass=$(( pass + 1 )); printf '  ok   %s\n' "$1"; }
bad() { fail=$(( fail + 1 )); printf '  FAIL %s\n' "$1"; }

assert_rc() {
    if [[ "$RC" == "$1" ]]; then ok "$2 (rc=$1)"; else bad "$2 (expected rc=$1, got $RC)"; fi
}
assert_log_has() {
    if grep -qE "$1" "$STATE/colima.log" 2>/dev/null; then ok "$2"; else bad "$2 (colima.log has: $(tr '\n' ';' < "$STATE/colima.log"))"; fi
}
assert_log_lacks() {
    if grep -qE "$1" "$STATE/colima.log" 2>/dev/null; then bad "$2 (colima.log has: $(tr '\n' ';' < "$STATE/colima.log"))"; else ok "$2"; fi
}
assert_log_empty() {
    if [[ ! -s "$STATE/colima.log" ]]; then ok "$1"; else bad "$1 (colima.log has: $(tr '\n' ';' < "$STATE/colima.log"))"; fi
}

echo "test-colima-ensure"

# 1 -------------------------------------------------------------------------
new_scenario
echo Running > "$STATE/status"
touch "$STATE/docker_up"
run_ensure
assert_rc 0 "1: healthy install exits clean"
assert_log_lacks '^start' "1: never starts colima when docker already answers"
assert_log_lacks '^stop' "1: never force-stops a healthy VM"

# 2 -------------------------------------------------------------------------
# The 2026-09-05 state. `colima start` against a Broken profile dies on the
# stale ha.sock every time, so the wrapper must not burn a boot minute on it.
new_scenario
echo Broken > "$STATE/status"
run_ensure
assert_rc 0 "2: Broken profile recovers"
assert_log_has '^stop --force' "2: issues stop --force"
if [[ "$(grep -cE '^start' "$STATE/colima.log")" == "1" ]]; then
    ok "2: exactly one start - skips the doomed pre-stop attempt"
else
    bad "2: expected 1 start, got $(grep -cE '^start' "$STATE/colima.log")"
fi

# 3 -------------------------------------------------------------------------
new_scenario
echo Stopped > "$STATE/status"
run_ensure
assert_rc 0 "3: plain start recovers a Stopped profile"
assert_log_has '^start' "3: issues start"
assert_log_lacks '^stop' "3: does NOT force-stop when a plain start works"

# 4 -------------------------------------------------------------------------
new_scenario
echo Stopped > "$STATE/status"
touch "$STATE/wedge_start_fails"
run_ensure
assert_rc 0 "4: stale-socket start failure recovers via stop --force"
assert_log_has '^stop --force' "4: escalates to stop --force"
if [[ "$(grep -cE '^start' "$STATE/colima.log")" == "2" ]]; then
    ok "4: retries start exactly once after the forced stop"
else
    bad "4: expected 2 starts, got $(grep -cE '^start' "$STATE/colima.log")"
fi

# 5 -------------------------------------------------------------------------
# The failure a `colima start` exit code cannot see: start reports success and
# the socket still refuses connections. `docker ps` is the only real signal.
new_scenario
echo Stopped > "$STATE/status"
touch "$STATE/wedge_no_docker"
run_ensure
assert_rc 0 "5: start exiting 0 with a dead socket still escalates"
assert_log_has '^stop --force' "5: escalates on the docker probe, not the exit code"

# 6 -------------------------------------------------------------------------
new_scenario
echo Stopped > "$STATE/status"
touch "$STATE/hard_start_fails"
run_ensure
assert_rc 1 "6: unrecoverable VM reports failure"
assert_log_has '^stop --force' "6: tried the forced stop before giving up"

# 7 -------------------------------------------------------------------------
new_scenario
OS_OVERRIDE=Linux run_ensure
assert_rc 0 "7: Linux host is a clean no-op"
assert_log_empty "7: never invokes colima on Linux"
unset OS_OVERRIDE

# 8 -------------------------------------------------------------------------
# A Mac with no Colima installed. Exercises the real resolution path: no
# COLIMA_BIN override, a PATH with no colima on it, and the Homebrew fallback
# prefixes redirected at an empty root so they cannot accidentally resolve on
# a developer machine that does have Colima.
new_scenario
mkdir -p "$TMP/noprefix"
RC=0
STUB_STATE="$STATE" \
PATH="$MIN_BIN" \
DOCKER_BIN="$STUB_BIN/docker" \
COLIMA_ENSURE_PREFIX_ROOT="$TMP/noprefix" \
COLIMA_ENSURE_OS=Darwin \
COLIMA_ENSURE_LOG="$STATE/ensure.log" \
COLIMA_ENSURE_LOCK_DIR="$LOCK" \
COLIMA_ENSURE_DOCKER_TIMEOUT=0 \
CUSTOMER_HOME="$STATE/customer" \
    bash "$ENSURE" >/dev/null 2>&1 || RC=$?
assert_rc 0 "8: a Mac without Colima is a clean no-op"
if grep -q 'not applicable: colima not found' "$STATE/ensure.log" 2>/dev/null; then
    ok "8: says why it did nothing"
else
    bad "8: expected the 'colima not found' line in the log"
fi

# 8b ------------------------------------------------------------------------
# Colima present, docker missing. The wrapper cannot verify the daemon answers,
# so it must refuse to claim success rather than exit 0 on a start that may
# have produced nothing.
new_scenario
mkdir -p "$TMP/noprefix"
echo Stopped > "$STATE/status"
RC=0
STUB_STATE="$STATE" \
PATH="$MIN_BIN" \
COLIMA_BIN="$STUB_BIN/colima" \
COLIMA_ENSURE_PREFIX_ROOT="$TMP/noprefix" \
COLIMA_ENSURE_OS=Darwin \
COLIMA_ENSURE_LOG="$STATE/ensure.log" \
COLIMA_ENSURE_LOCK_DIR="$LOCK" \
COLIMA_ENSURE_DOCKER_TIMEOUT=0 \
CUSTOMER_HOME="$STATE/customer" \
    bash "$ENSURE" >/dev/null 2>&1 || RC=$?
assert_rc 1 "8b: refuses to report success when docker cannot be probed"
assert_log_empty "8b: does not start colima it cannot verify"

# 9 -------------------------------------------------------------------------
# A concurrent run must stand down, not fight it. Two recoveries racing on the
# same ha.pid / ha.sock is the mess this whole script exists to clean up.
new_scenario
echo Stopped > "$STATE/status"
mkdir -p "$LOCK"
printf '%s' "$$" > "$LOCK/pid"   # this test process: alive, and the lock is new
run_ensure
assert_rc 0 "9: stands down when another run holds the lock"
assert_log_empty "9: issues no colima commands while standing down"
if [[ -d "$LOCK" ]]; then ok "9: leaves the other run's lock in place"; else bad "9: deleted a lock it did not own"; fi
rmdir "$LOCK" 2>/dev/null || true

# 10 ------------------------------------------------------------------------
new_scenario
echo Stopped > "$STATE/status"
mkdir -p "$LOCK"
printf '%s' "999999" > "$LOCK/pid"   # no such process
run_ensure
assert_rc 0 "10: breaks a lock whose holder is gone"
assert_log_has '^start' "10: proceeds after breaking the abandoned lock"

# 11 ------------------------------------------------------------------------
# The recycled-pid case. The postmortem's stale ha.pid pointed at PID 789,
# which by then was `sirittsd` - a live, unrelated process. A lock that trusts
# `kill -0` alone would hand the box a permanent deadlock on exactly that.
new_scenario
echo Stopped > "$STATE/status"
mkdir -p "$LOCK"
printf '%s' "$$" > "$LOCK/pid"   # alive, but we age the lock past the limit
touch -t 200001010000 "$LOCK"
LOCK_MAX_AGE=60 run_ensure
assert_rc 0 "11: breaks a stale-aged lock even though its pid is alive"
assert_log_has '^start' "11: proceeds rather than deadlocking on a recycled pid"
unset LOCK_MAX_AGE

# 12 ------------------------------------------------------------------------
# Across every scenario above.
destructive=0
for f in "$TMP"/s*/colima.log; do
    [[ -f "$f" ]] || continue
    if grep -qE '(^|[[:space:]])delete([[:space:]]|$)|--reset' "$f"; then
        echo "    destructive invocation in $f: $(tr '\n' ';' < "$f")"
        destructive=1
    fi
done
if [[ "$destructive" == "0" ]]; then
    ok "12: no scenario ever ran 'colima delete' or 'colima start --reset'"
else
    bad "12: a scenario issued a destructive colima command"
fi

# 13 ------------------------------------------------------------------------
# Static guard so a future edit cannot quietly add one. Only the lines that
# actually invoke the binary are inspected, so the safety comments explaining
# why these commands are banned do not trip it.
if grep -n '"\$COLIMA_BIN"' "$ENSURE" | grep -qE 'delete|--reset'; then
    bad "13: colima-ensure.sh invokes COLIMA_BIN with a destructive verb"
else
    ok "13: no invocation of COLIMA_BIN mentions delete or --reset"
fi

echo ""
echo "test-colima-ensure: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]] || exit 1
exit 0
