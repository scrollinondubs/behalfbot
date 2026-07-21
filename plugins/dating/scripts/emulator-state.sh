#!/usr/bin/env bash
# emulator-state.sh - shared readiness probe + PIN unlock for the dating AVD.
#
# Library only: declares functions, no side effects at source time. Sourced by
# the recovery hook (scheduled-tasks/recovery-hooks.d/dating-emulator-recovery.sh)
# and the gather script (scripts/gather-dating-swipe.sh) so both judge the
# device by the same rules.
#
# Why this exists (2026-07-21 incident, v1 reference install issue #341):
# an AVD with a lock-screen PIN boots into user state RUNNING_LOCKED. Android
# is up, but the credential-encrypted (CE) user storage is never unlocked, so
# no CE app's activities are registered with the package manager. On that
# device EVERY legacy readiness signal reads healthy:
#
#   adb devices            -> "device"        (true)
#   sys.boot_completed     -> 1               (true)
#   screencap              -> ~1.4MB          (true - it renders the LOCK SCREEN)
#   pm list packages       -> all present     (true - installed, not resolvable)
#   dumpsys window focus   -> non-null        (true - the keyguard has focus)
#
# while `cmd package resolve-activity` finds no activity for any dating app
# and `monkey` reports "No activities found to run". A device that cannot
# launch an app must never be reported ready, so readiness here checks the
# two signals that actually distinguish the states:
#
#   1. `dumpsys user` must not report RUNNING_LOCKED
#   2. every required package must resolve a launcher activity
#
# Environment (all optional; defaults match the chassis manifest):
#   DATING_ADB_SERIAL                  device serial          (default emulator-5554)
#   DATING_EMULATOR_REQUIRED_PACKAGES space-separated package list that must
#                                      resolve a launcher activity
#                                      (default: co.hinge.app)
#   DATING_EMULATOR_PIN_ENV_VAR        NAME of the env var holding the
#                                      lock-screen PIN (default DATING_EMULATOR_PIN).
#                                      The PIN itself never lives in config,
#                                      the repo, or logs - the installer wires
#                                      the named var to their own secret store.
#   DATING_EMULATOR_UNLOCK_TIMEOUT     seconds to wait for RUNNING_UNLOCKED
#                                      after sending the PIN (default 30)
#   DATING_EMULATOR_UNLOCK_FLAG        path of the unlock-in-flight flag file.
#                                      Watchdogs must not kill the emulator
#                                      while this flag is fresh - see
#                                      dating_emulator_unlock_in_flight.

# Guard against re-sourcing. The exit branch only runs if the file is
# executed rather than sourced, which shellcheck cannot see (SC2317).
# shellcheck disable=SC2317
if declare -F dating_emulator_state >/dev/null; then
    return 0 2>/dev/null || exit 0
fi

_dating_adb() {
    adb -s "${DATING_ADB_SERIAL:-emulator-5554}" "$@"
}

_dating_unlock_flag_path() {
    if [[ -n "${DATING_EMULATOR_UNLOCK_FLAG:-}" ]]; then
        echo "$DATING_EMULATOR_UNLOCK_FLAG"
    elif [[ -n "${CHASSIS_HOME:-}" ]]; then
        echo "$CHASSIS_HOME/plugins/dating/EMULATOR_UNLOCK_IN_FLIGHT"
    else
        echo "${TMPDIR:-/tmp}/dating-emulator-unlock-in-flight"
    fi
}

# True while a PIN unlock is in progress AND the flag is fresh (< 5 min old).
# Watchdog callers check this before treating a bad sample (0-byte screencap,
# null window focus) as a wedged emulator: the unlock transition produces
# exactly those transient readings, and killing qemu mid-unlock reboots the
# device straight back to the lock screen (observed 2026-07-21 11:13 - the
# watchdog killed a device mid-unlock, then declared the relocked boot ready).
# The staleness bound stops a crashed unlock from suppressing recovery forever.
dating_emulator_unlock_in_flight() {
    local flag
    flag=$(_dating_unlock_flag_path)
    [[ -f "$flag" ]] || return 1
    # find -mmin -5: prints the file only if modified within the last 5 min.
    [[ -n $(find "$flag" -mmin -5 2>/dev/null) ]]
}

# Current-user lock state parsed from `dumpsys user`.
# Prints: locked | unlocked | unknown
dating_emulator_lock_state() {
    local out
    out=$(_dating_adb shell dumpsys user 2>/dev/null | tr -d '\r')
    [[ -z "$out" ]] && { echo "unknown"; return; }
    # Single-user AVD: any RUNNING_LOCKED user means the CE storage we need
    # is locked. Checked before RUNNING_UNLOCKED so a locked user cannot hide
    # behind an unlocked system line.
    if echo "$out" | grep -q "State: RUNNING_LOCKED"; then
        echo "locked"
    elif echo "$out" | grep -q "State: RUNNING_UNLOCKED"; then
        echo "unlocked"
    else
        echo "unknown"
    fi
}

# True when every required package resolves a launcher activity. This is the
# check that actually proves an app can launch - `pm list packages` passes on
# a locked device, `resolve-activity` does not.
dating_emulator_apps_resolvable() {
    local pkgs="${DATING_EMULATOR_REQUIRED_PACKAGES:-co.hinge.app}"
    local pkg out
    for pkg in $pkgs; do
        out=$(_dating_adb shell cmd package resolve-activity --brief "$pkg" 2>/dev/null | tr -d '\r')
        # Resolvable output ends in a component line like
        # "co.hinge.app/co.hinge.app.MainActivity"; a locked or missing app
        # yields "No activity found".
        if ! echo "$out" | grep -q "/"; then
            return 1
        fi
    done
    return 0
}

# Single-sample state probe. Prints exactly one machine-readable token:
#
#   emulator_not_running       no adb / device not attached      -> start it
#   emulator_not_booted        sys.boot_completed != 1           -> wait / start
#   emulator_locked            user state RUNNING_LOCKED         -> UNLOCK it
#   emulator_no_focus          booted but no focused window      -> wedged; restart
#   emulator_apps_unresolvable unlocked but a required package   -> investigate /
#                              resolves no launcher activity        restart
#   emulator_ready             all checks pass
#
# The token IS the contract: callers branch on it, and the remedies are
# opposite (restarting a locked device just boots it locked again; unlocking
# a wedged device does nothing). Collapsing these states into ready/not-ready
# is what caused the 2026-07-21 restart loop.
#
# This probe never mutates the device. Destructive remedies (kill/restart)
# belong to the recovery hook, which must confirm a wedge signal persists
# across two samples before acting - a single bad sample can be a transient
# reading taken mid-unlock.
dating_emulator_state() {
    if ! command -v adb >/dev/null 2>&1; then
        echo "emulator_not_running"
        return
    fi
    if ! adb devices 2>/dev/null | grep -Eq "^${DATING_ADB_SERIAL:-emulator-5554}[[:space:]]+device"; then
        echo "emulator_not_running"
        return
    fi
    if ! _dating_adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' | grep -q "^1$"; then
        echo "emulator_not_booted"
        return
    fi
    # Lock state BEFORE window focus: the keyguard holds window focus, so a
    # locked device passes the focus check and must be caught first.
    local lock_state
    lock_state=$(dating_emulator_lock_state)
    if [[ "$lock_state" == "locked" ]]; then
        echo "emulator_locked"
        return
    fi
    # boot_completed=1 is necessary but not sufficient: a wedged AVD can hit
    # boot_completed=1 with the launcher dead (mCurrentFocus=null).
    local focus
    focus=$(_dating_adb shell dumpsys window 2>/dev/null | grep -m1 mCurrentFocus | tr -d '\r')
    if [[ -z "$focus" ]] || echo "$focus" | grep -q "mCurrentFocus=null"; then
        echo "emulator_no_focus"
        return
    fi
    if ! dating_emulator_apps_resolvable; then
        echo "emulator_apps_unresolvable"
        return
    fi
    echo "emulator_ready"
}

# Unlock a PIN-locked device. Sequence proven on the 2026-07-21 incident
# device (verified twice: RUNNING_LOCKED -> RUNNING_UNLOCKED, all apps
# resolvable immediately after):
#
#   input keyevent 82   (wake / dismiss)
#   swipe up            (reveal the PIN pad)
#   input text <PIN>
#   input keyevent 66   (enter)
#   poll `dumpsys user` until RUNNING_UNLOCKED or timeout
#
# Return codes (distinct on purpose - callers alert differently):
#   0  device unlocked
#   1  unlock attempted but device still locked after timeout
#   2  PIN env var unset/empty - actionable config error, message names the var
#
# The PIN value is NEVER logged or echoed by this function. Note: `adb shell
# input text` does expose the PIN briefly in the host process list; there is
# no adb input path that avoids that, and for an emulator lock-screen PIN the
# trade-off is accepted. The PIN must still never land in config files, logs,
# or the repo.
#
# NEVER "fix" a locked device with `-wipe-data`. Wiping clears the lock but
# logs the installer out of every dating app, each requiring manual GUI
# re-auth. Sean, 2026-07-21: "don't wipe the emulator - it's a PITA to get
# every app authenticated." Unlocking with the PIN is the remedy; wiping is
# for a genuinely corrupt AVD only, and never as automated recovery for
# emulator_locked.
dating_emulator_unlock() {
    local pin_var="${DATING_EMULATOR_PIN_ENV_VAR:-DATING_EMULATOR_PIN}"
    local pin="${!pin_var:-}"
    if [[ -z "$pin" ]]; then
        echo "dating_emulator_unlock: device is PIN-locked but \$${pin_var} is not set." \
             "Set ${pin_var} (see plugins/dating/openclaw.plugin.json > emulator.pin_env_var)" \
             "from your secret store - the device CANNOT recover without it." >&2
        return 2
    fi

    local flag
    flag=$(_dating_unlock_flag_path)
    mkdir -p "$(dirname "$flag")" 2>/dev/null || true
    touch "$flag"

    # `|| true` throughout: callers may run under set -e, and an aborted
    # unlock that leaves the in-flight flag behind would suppress recovery
    # until the flag goes stale.
    _dating_adb shell input keyevent 82 >/dev/null 2>&1 || true
    sleep 1
    # Swipe up to reveal the PIN pad. Coordinates derived from the reported
    # screen size, falling back to 1080x2400 defaults.
    local size w h
    size=$(_dating_adb shell wm size 2>/dev/null | tr -d '\r' | grep -oE '[0-9]+x[0-9]+' | head -1)
    w=${size%x*}; h=${size#*x}
    [[ "$w" =~ ^[0-9]+$ ]] || w=1080
    [[ "$h" =~ ^[0-9]+$ ]] || h=2400
    _dating_adb shell input swipe $((w / 2)) $((h * 2 / 3)) $((w / 2)) $((h / 4)) 200 >/dev/null 2>&1 || true
    sleep 1
    _dating_adb shell input text "$pin" >/dev/null 2>&1 || true
    _dating_adb shell input keyevent 66 >/dev/null 2>&1 || true

    local timeout="${DATING_EMULATOR_UNLOCK_TIMEOUT:-30}"
    local i
    for (( i = 0; i < timeout; i++ )); do
        if [[ $(dating_emulator_lock_state) == "unlocked" ]]; then
            rm -f "$flag"
            return 0
        fi
        sleep 1
    done
    rm -f "$flag"
    return 1
}
