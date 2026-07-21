#!/usr/bin/env bash
# test-emulator-state.sh - behavioural coverage for the dating-AVD readiness
# probe, PIN unlock, and recovery-hook routing.
#
# Why this suite exists: the 2026-07-21 incident was a check that could not
# fail. A PIN-locked AVD (user state RUNNING_LOCKED, zero launchable apps)
# passed every readiness signal the automation trusted - adb attached,
# boot_completed=1, non-zero screencap, packages installed, window focus
# non-null (the keyguard holds focus) - and the watchdog logged "Emulator
# ready" three times against a device that could not launch one app. The
# deliverable here is proof the new probe DISTINGUISHES locked from unlocked,
# and that the failure paths fail loudly instead of reporting ready.
#
# Self-contained: stubs `adb` (and pgrep/pkill/curl for the hook tests) on
# PATH, driven by fixture files. No emulator, no docker, no network. Fixture
# content is synthesized to match the observed shapes from the incident
# device (`State: RUNNING_LOCKED`, `No activity found`, component lines from
# resolve-activity).
#
# Run: bash plugins/dating/tests/test-emulator-state.sh
#
# shellcheck disable=SC2016  # stub scripts must expand $* at RUN time, not here
# shellcheck disable=SC2030,SC2031  # env exports are deliberately subshell-scoped per scenario

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$desc"
    else
        fail "$desc (expected '$expected', got '$actual')"
    fi
}

assert_contains() {
    local desc="$1" needle="$2" haystack="$3"
    if echo "$haystack" | grep -qF "$needle"; then
        pass "$desc"
    else
        fail "$desc (missing '$needle')"
    fi
}

assert_not_contains() {
    local desc="$1" needle="$2" haystack="$3"
    if echo "$haystack" | grep -qF "$needle"; then
        fail "$desc (found forbidden '$needle')"
    else
        pass "$desc"
    fi
}

# --- Stub environment -------------------------------------------------------

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
STUB_BIN="$WORK/bin"
mkdir -p "$STUB_BIN"

cat > "$STUB_BIN/adb" <<'EOF'
#!/usr/bin/env bash
F="${ADB_FIXTURE_DIR:?ADB_FIXTURE_DIR must be set}"
echo "adb $*" >> "$F/calls.log"
if [[ "${1:-}" == "-s" ]]; then shift 2; fi
case "${1:-}" in
  devices)
    cat "$F/devices" 2>/dev/null || echo "List of devices attached"
    exit 0 ;;
  shell)
    shift
    cmd="$*"
    case "$cmd" in
      "getprop sys.boot_completed")
        cat "$F/boot_completed" 2>/dev/null; exit 0 ;;
      "dumpsys user")
        if [[ -f "$F/unlocked_marker" && -f "$F/dumpsys_user_unlocked" ]]; then
          cat "$F/dumpsys_user_unlocked"
        else
          cat "$F/dumpsys_user" 2>/dev/null
        fi
        exit 0 ;;
      "dumpsys window")
        n=$(cat "$F/window_counter" 2>/dev/null || echo 0)
        n=$((n + 1)); echo "$n" > "$F/window_counter"
        if [[ -f "$F/dumpsys_window.$n" ]]; then
          cat "$F/dumpsys_window.$n"
        else
          cat "$F/dumpsys_window" 2>/dev/null
        fi
        exit 0 ;;
      "wm size")
        echo "Physical size: 1080x2400"; exit 0 ;;
      "cmd package resolve-activity --brief "*)
        pkg="${cmd##* }"
        if [[ -f "$F/resolve_$pkg" ]]; then
          cat "$F/resolve_$pkg"
        else
          echo "No activity found"
        fi
        exit 0 ;;
      "input keyevent 66")
        # The stub "accepts the PIN": pressing enter transitions dumpsys user
        # to the unlocked fixture when the scenario opts in.
        if [[ -f "$F/unlock_on_enter" ]]; then touch "$F/unlocked_marker"; fi
        exit 0 ;;
      input*)
        exit 0 ;;
      *)
        exit 0 ;;
    esac ;;
  *)
    exit 0 ;;
esac
EOF
chmod +x "$STUB_BIN/adb"

# Hook tests: no qemu running, no real kills, no real webhooks.
printf '#!/usr/bin/env bash\nexit 1\n' > "$STUB_BIN/pgrep"
printf '#!/usr/bin/env bash\necho "pkill $*" >> "${ADB_FIXTURE_DIR:?}/calls.log"\nexit 0\n' > "$STUB_BIN/pkill"
printf '#!/usr/bin/env bash\necho "curl $*" >> "${ADB_FIXTURE_DIR:?}/calls.log"\nexit 0\n' > "$STUB_BIN/curl"
chmod +x "$STUB_BIN/pgrep" "$STUB_BIN/pkill" "$STUB_BIN/curl"

export PATH="$STUB_BIN:$PATH"

# Fixture builders. Shapes match the 2026-07-21 incident device output.
new_fixture() {
    local name="$1" lock_state="$2"   # locked | unlocked
    local dir="$WORK/fixtures/$name"
    mkdir -p "$dir"
    : > "$dir/calls.log"
    printf 'List of devices attached\nemulator-5554\tdevice\n' > "$dir/devices"
    echo "1" > "$dir/boot_completed"
    cat > "$dir/dumpsys_user" <<USEREOF
Users:
  UserInfo{0:Installer:c13} running
    State: RUNNING_$(echo "$lock_state" | tr '[:lower:]' '[:upper:]')
    Created: +2h11m ago
    Last logged in: +5m ago
USEREOF
    cat > "$dir/dumpsys_user_unlocked" <<'USEREOF'
Users:
  UserInfo{0:Installer:c13} running
    State: RUNNING_UNLOCKED
    Created: +2h11m ago
    Last logged in: +5m ago
USEREOF
    # The keyguard HOLDS window focus on a locked device - this is the
    # signal that fooled the old focus-only readiness check.
    if [[ "$lock_state" == "locked" ]]; then
        echo "  mCurrentFocus=Window{79fa5e1 u0 NotificationShade}" > "$dir/dumpsys_window"
    else
        echo "  mCurrentFocus=Window{8438f3c u0 com.google.android.apps.nexuslauncher/com.google.android.apps.nexuslauncher.NexusLauncherActivity}" > "$dir/dumpsys_window"
        # Unlocked devices resolve their launcher activities.
        echo "co.hinge.app/co.hinge.app.ui.MainActivity" > "$dir/resolve_co.hinge.app"
        echo "com.bumble.app/com.bumble.app.ui.launcher.BumbleLauncherActivity" > "$dir/resolve_com.bumble.app"
        echo "com.tinder/com.tinder.launch.internal.LaunchActivity" > "$dir/resolve_com.tinder"
    fi
    echo "$dir"
}

lib() {
    # Run a snippet with the library sourced, in a subshell with the given
    # fixture. Usage: lib <fixture_dir> <snippet>
    local fixture="$1"; shift
    ( export ADB_FIXTURE_DIR="$fixture"
      export DATING_EMULATOR_UNLOCK_FLAG="$fixture/unlock-in-flight"
      export DATING_EMULATOR_UNLOCK_TIMEOUT=3
      # shellcheck source=../scripts/emulator-state.sh
      source "$PLUGIN_DIR/scripts/emulator-state.sh"
      # shellcheck disable=SC2294  # snippets carry per-test env assignments on purpose
      eval "$@" )
}

# --- 1. State detection -----------------------------------------------------

echo "== dating_emulator_state =="

FX=$(new_fixture locked locked)
assert_eq "PIN-locked device (keyguard has focus, apps installed) is emulator_locked, NOT ready" \
    "emulator_locked" "$(lib "$FX" dating_emulator_state)"

FX=$(new_fixture unlocked unlocked)
assert_eq "unlocked device with resolvable apps is emulator_ready" \
    "emulator_ready" \
    "$(lib "$FX" 'DATING_EMULATOR_REQUIRED_PACKAGES="co.hinge.app com.bumble.app com.tinder" dating_emulator_state')"

FX=$(new_fixture apps-unresolvable unlocked)
rm -f "$FX/resolve_com.tinder"
assert_eq "unlocked device missing one required launcher activity is emulator_apps_unresolvable" \
    "emulator_apps_unresolvable" \
    "$(lib "$FX" 'DATING_EMULATOR_REQUIRED_PACKAGES="co.hinge.app com.tinder" dating_emulator_state')"

FX=$(new_fixture not-running unlocked)
printf 'List of devices attached\n' > "$FX/devices"
assert_eq "no attached device is emulator_not_running" \
    "emulator_not_running" "$(lib "$FX" dating_emulator_state)"

FX=$(new_fixture not-booted unlocked)
echo "0" > "$FX/boot_completed"
assert_eq "boot_completed=0 is emulator_not_booted" \
    "emulator_not_booted" "$(lib "$FX" dating_emulator_state)"

FX=$(new_fixture no-focus unlocked)
echo "  mCurrentFocus=null" > "$FX/dumpsys_window"
assert_eq "booted but null window focus is emulator_no_focus" \
    "emulator_no_focus" "$(lib "$FX" dating_emulator_state)"

# --- 2. Gather script -------------------------------------------------------

echo "== gather-dating-swipe.sh =="

FX=$(new_fixture gather-locked locked)
OUT=$(ADB_FIXTURE_DIR="$FX" DATING_EMULATOR_UNLOCK_FLAG="$FX/flag" bash "$PLUGIN_DIR/scripts/gather-dating-swipe.sh")
assert_eq "gather on a locked device reports count 0 / emulator_locked (the incident's exact gap)" \
    '{"count": 0, "reason": "emulator_locked"}' "$OUT"

FX=$(new_fixture gather-ready unlocked)
OUT=$(ADB_FIXTURE_DIR="$FX" DATING_EMULATOR_UNLOCK_FLAG="$FX/flag" bash "$PLUGIN_DIR/scripts/gather-dating-swipe.sh")
assert_eq "gather on a ready device reports count 1" \
    '{"count": 1, "reason": "emulator_ready"}' "$OUT"

FX=$(new_fixture gather-down unlocked)
printf 'List of devices attached\n' > "$FX/devices"
OUT=$(ADB_FIXTURE_DIR="$FX" DATING_EMULATOR_UNLOCK_FLAG="$FX/flag" bash "$PLUGIN_DIR/scripts/gather-dating-swipe.sh")
assert_eq "gather with no device reports emulator_not_running (distinct reason, distinct remedy)" \
    '{"count": 0, "reason": "emulator_not_running"}' "$OUT"

# --- 3. Unlock: failure path first ------------------------------------------

echo "== dating_emulator_unlock =="

FX=$(new_fixture unlock-no-pin locked)
RC=0
ERR=$(lib "$FX" 'unset DATING_EMULATOR_PIN; dating_emulator_unlock' 2>&1) || RC=$?
assert_eq "unlock with PIN var unset returns 2 (distinct config-error code)" "2" "$RC"
assert_contains "missing-PIN error names the exact env var to set" "DATING_EMULATOR_PIN" "$ERR"
assert_not_contains "no input events sent when the PIN is unavailable" "input text" "$(cat "$FX/calls.log")"

FX=$(new_fixture unlock-custom-var locked)
RC=0
ERR=$(lib "$FX" 'unset MY_PIN_VAR; DATING_EMULATOR_PIN_ENV_VAR=MY_PIN_VAR dating_emulator_unlock' 2>&1) || RC=$?
assert_eq "unlock honors a custom pin_env_var name" "2" "$RC"
assert_contains "error names the CUSTOM var, not the default" "MY_PIN_VAR" "$ERR"

FX=$(new_fixture unlock-success locked)
touch "$FX/unlock_on_enter"
RC=0
OUT=$(lib "$FX" 'DATING_EMULATOR_PIN=424242 dating_emulator_unlock' 2>&1) || RC=$?
assert_eq "unlock with PIN set succeeds once dumpsys user reports RUNNING_UNLOCKED" "0" "$RC"
assert_contains "the PIN was actually typed into the device" "input text 424242" "$(cat "$FX/calls.log")"
assert_not_contains "the PIN never appears in the function's own output" "424242" "$OUT"
assert_eq "unlock-in-flight flag removed after success" "gone" "$([[ -f "$FX/unlock-in-flight" ]] && echo present || echo gone)"

FX=$(new_fixture unlock-timeout locked)
# No unlock_on_enter marker: the device stays locked no matter what is typed.
RC=0
lib "$FX" 'DATING_EMULATOR_PIN=424242 dating_emulator_unlock' >/dev/null 2>&1 || RC=$?
assert_eq "unlock that never reaches RUNNING_UNLOCKED returns 1 after timeout" "1" "$RC"

# --- 4. Recovery hook routing -----------------------------------------------

echo "== chassis_recovery_dating_emulator =="

run_hook() {
    # Usage: run_hook <fixture> [extra env exports...]
    local fixture="$1"; shift
    local home="$fixture/chassis-home"
    mkdir -p "$home/plugins/dating"
    ( export ADB_FIXTURE_DIR="$fixture"
      export CHASSIS_HOME="$home"
      export DATING_EMULATOR_UNLOCK_FLAG="$fixture/unlock-in-flight"
      export DATING_EMULATOR_UNLOCK_TIMEOUT=3
      export DATING_WEDGE_CONFIRM_DELAY=0
      for kv in "$@"; do export "${kv?}"; done
      # shellcheck source=../scheduled-tasks/recovery-hooks.d/dating-emulator-recovery.sh
      source "$PLUGIN_DIR/scheduled-tasks/recovery-hooks.d/dating-emulator-recovery.sh"
      chassis_recovery_dating_emulator )
}

hook_log() { cat "$1/chassis-home/logs/scheduled/dating-emulator-recovery.log" 2>/dev/null; }
hook_last_state() {
    python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('last_state',''))" \
        "$1/chassis-home/scheduled-tasks/dating-emulator-state.json" 2>/dev/null
}

# 4a. Locked + no PIN var: must fail loudly, never report ready, never kill.
FX=$(new_fixture hook-locked-no-pin locked)
RC=0
( unset DATING_EMULATOR_PIN 2>/dev/null; run_hook "$FX" ) || RC=$?
assert_eq "hook on locked device with no PIN var returns failure, not success" "1" "$RC"
assert_contains "hook log carries the machine-readable reason" "reason=emulator_locked" "$(hook_log "$FX")"
assert_contains "hook log names the missing env var loudly" "UNLOCK BLOCKED" "$(hook_log "$FX")"
assert_not_contains "hook never logs 'ready' for a locked device" "no action needed" "$(hook_log "$FX")"
assert_not_contains "hook never kills qemu for a locked device (restart cannot fix a lock)" "pkill" "$(cat "$FX/calls.log")"
assert_eq "state file records last_state=emulator_locked for downstream callers" \
    "emulator_locked" "$(hook_last_state "$FX")"

# 4b. Locked + PIN available: hook unlocks in place and reports ready.
FX=$(new_fixture hook-locked-unlocks locked)
touch "$FX/unlock_on_enter"
# Once unlocked, apps must resolve for full readiness.
echo "co.hinge.app/co.hinge.app.ui.MainActivity" > "$FX/resolve_co.hinge.app"
echo "  mCurrentFocus=Window{8438f3c u0 com.google.android.apps.nexuslauncher/x}" > "$FX/dumpsys_window"
RC=0
run_hook "$FX" DATING_EMULATOR_PIN=424242 || RC=$?
assert_eq "hook unlocks a locked device and returns success" "0" "$RC"
assert_contains "hook log shows the unlock path ran" "attempting unlock" "$(hook_log "$FX")"
assert_contains "hook reaches full readiness after unlock" "ready after unlock" "$(hook_log "$FX")"
assert_not_contains "PIN value never lands in the hook log" "424242" "$(hook_log "$FX")"
assert_not_contains "no restart performed to fix a lock" "attempting restart" "$(hook_log "$FX")"

# 4c. Transient wedge signal: second sample healthy, no kill.
FX=$(new_fixture hook-transient-wedge unlocked)
echo "  mCurrentFocus=null" > "$FX/dumpsys_window.1"
RC=0
run_hook "$FX" DATING_EMULATOR_REQUIRED_PACKAGES=co.hinge.app || RC=$?
assert_eq "transient null-focus sample does not fail the hook" "0" "$RC"
assert_contains "hook re-sampled instead of acting on one bad reading" "re-sampling" "$(hook_log "$FX")"
assert_contains "wedge signal cleared on the second sample" "cleared on second sample" "$(hook_log "$FX")"
assert_not_contains "no qemu kill on a transient reading" "pkill" "$(cat "$FX/calls.log")"

# 4d. Unlock in flight: hook stands down entirely.
FX=$(new_fixture hook-unlock-in-flight locked)
touch "$FX/unlock-in-flight"
RC=0
run_hook "$FX" || RC=$?
assert_eq "hook stands down while an unlock is in flight" "0" "$RC"
assert_contains "stand-down is logged" "standing down" "$(hook_log "$FX")"
assert_not_contains "no kill while unlock in flight" "pkill" "$(cat "$FX/calls.log")"

# 4e. Not-booted routes to the restart path, not the unlock path.
FX=$(new_fixture hook-not-booted unlocked)
echo "0" > "$FX/boot_completed"
RC=0
run_hook "$FX" || RC=$?
assert_eq "hook on a non-booted device takes the restart path and fails without a start script" "1" "$RC"
assert_contains "restart path logged with machine-readable reason" "reason=emulator_not_booted" "$(hook_log "$FX")"
assert_not_contains "no unlock attempted for a boot problem" "attempting unlock" "$(hook_log "$FX")"

# --- Summary ----------------------------------------------------------------

echo ""
echo "$PASS passed, $FAIL failed"
if (( FAIL > 0 )); then
    exit 1
fi
exit 0
