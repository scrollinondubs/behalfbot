#!/usr/bin/env bash
# dating-emulator-recovery.sh - chassis recovery hook for the dating
# Android emulator (AVD).
#
# This file is sourced by the chassis heartbeat dispatcher at startup, which
# discovers all *.sh files in plugins/*/scheduled-tasks/recovery-hooks.d/ and
# sources them so they can register `chassis_recovery_*` functions in the
# dispatcher's environment.
#
# Sourcing contract: this script MUST NOT execute side-effects at source time.
# It only declares functions. The dispatcher (or an admin) calls the
# function explicitly when recovery is needed.
#
# Function: chassis_recovery_dating_emulator
#   Self-healing watchdog for the dating Android emulator.
#   - Respects ${CHASSIS_HOME}/plugins/dating/EMULATOR_PAUSE flag
#   - Skips if emulator is already ready
#   - Probes device state via scripts/emulator-state.sh and picks the remedy
#     per state - the remedies are OPPOSITE, so states are never collapsed:
#       emulator_not_running / emulator_not_booted   -> start / restart
#       emulator_locked                              -> PIN unlock, NEVER restart
#       emulator_no_focus / emulator_apps_unresolvable (persisting) -> restart
#   - Requires a wedge signal to persist across two samples before killing
#     anything, and stands down entirely while a PIN unlock is in flight
#   - Enforces a cooldown between restart attempts
#   - Posts to the configured ops webhook after N consecutive failures
#
# Configuration is read from chassis.config.yaml > modules.dating.emulator,
# surfaced as env vars by the chassis bootstrap:
#   - DATING_AVD_NAME            (default: Dating_Pixel)
#   - DATING_EMULATOR_PAUSE_FLAG (default: $CHASSIS_HOME/plugins/dating/EMULATOR_PAUSE)
#   - DATING_EMULATOR_STATE_FILE (default: $CHASSIS_HOME/scheduled-tasks/dating-emulator-state.json)
#   - DATING_OPS_WEBHOOK_URL     (optional, for failure-threshold alerts)
#   - DATING_EMULATOR_COOLDOWN   (default: 1800 seconds)
#   - DATING_EMULATOR_FAILURE_THRESHOLD (default: 3)
#   - DATING_EMULATOR_PIN_ENV_VAR (default: DATING_EMULATOR_PIN) - NAME of the
#     env var holding the lock-screen PIN; see scripts/emulator-state.sh. The
#     PIN itself must never appear in config, logs, or the repo.
#   - DATING_EMULATOR_REQUIRED_PACKAGES (default: co.hinge.app)
#   - DATING_WEDGE_CONFIRM_DELAY (default: 10 seconds between wedge samples)
#
# V1 reference: <v1-reference-install> `scripts/emulator-watchdog.sh`. This
# chassis port keeps the same logic, swaps installer-specific paths /
# AVD-name / webhook for chassis env vars, and packages it as a sourceable
# function rather than a standalone launchd target.

# Guard against re-sourcing.
if declare -F chassis_recovery_dating_emulator >/dev/null; then
    return 0
fi

# Shared state probe + PIN unlock. The library declares functions only, so
# sourcing it here keeps the no-side-effects contract intact.
# shellcheck source=../../scripts/emulator-state.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)/emulator-state.sh"

chassis_recovery_dating_emulator() {
    set -u

    local CHASSIS_HOME_RESOLVED="${CHASSIS_HOME:?CHASSIS_HOME must be set before invoking chassis_recovery_dating_emulator}"
    local AVD_NAME="${DATING_AVD_NAME:-Dating_Pixel}"
    local PAUSE_FLAG="${DATING_EMULATOR_PAUSE_FLAG:-$CHASSIS_HOME_RESOLVED/plugins/dating/EMULATOR_PAUSE}"
    local STATE_FILE="${DATING_EMULATOR_STATE_FILE:-$CHASSIS_HOME_RESOLVED/scheduled-tasks/dating-emulator-state.json}"
    local LOG_DIR="${DATING_EMULATOR_LOG_DIR:-$CHASSIS_HOME_RESOLVED/logs/scheduled}"
    local LOG_FILE="$LOG_DIR/dating-emulator-recovery.log"
    local COOLDOWN_SECONDS="${DATING_EMULATOR_COOLDOWN:-1800}"
    local FAILURE_ALERT_THRESHOLD="${DATING_EMULATOR_FAILURE_THRESHOLD:-3}"
    local START_SCRIPT="${DATING_EMULATOR_START_SCRIPT:-$CHASSIS_HOME_RESOLVED/plugins/dating/scripts/emulator-start.sh}"
    local WEDGE_CONFIRM_DELAY="${DATING_WEDGE_CONFIRM_DELAY:-10}"
    local PIN_VAR_NAME="${DATING_EMULATOR_PIN_ENV_VAR:-DATING_EMULATOR_PIN}"

    mkdir -p "$LOG_DIR" "$(dirname "$STATE_FILE")"

    local _now; _now=$(date +%s)
    local _ts; _ts=$(date '+%Y-%m-%d %H:%M:%S')
    _log() { echo "[$_ts] $*" >> "$LOG_FILE"; }

    # --- Load/init state ---
    local LAST_RESTART_ATTEMPT=0
    local CONSECUTIVE_FAILURES=0
    local LAST_ALERT_SENT=0
    if [[ -f "$STATE_FILE" ]]; then
        LAST_RESTART_ATTEMPT=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('last_restart_attempt', 0))" "$STATE_FILE" 2>/dev/null || echo 0)
        CONSECUTIVE_FAILURES=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('consecutive_failures', 0))" "$STATE_FILE" 2>/dev/null || echo 0)
        LAST_ALERT_SENT=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('last_alert_sent', 0))" "$STATE_FILE" 2>/dev/null || echo 0)
    fi

    # Machine-readable state token from the last probe, persisted to the
    # state file so callers (dashboards, gathers, the dispatcher) can see WHY
    # the last run did what it did.
    local LAST_STATE="unknown"

    _save_state() {
        local attempt="${1:-$LAST_RESTART_ATTEMPT}"
        local failures="${2:-$CONSECUTIVE_FAILURES}"
        local alert="${3:-$LAST_ALERT_SENT}"
        python3 -c "
import json, sys
data = {
    'last_restart_attempt': $attempt,
    'consecutive_failures': $failures,
    'last_alert_sent': $alert,
    'last_run': $_now,
    'last_state': sys.argv[2],
}
with open(sys.argv[1], 'w') as f:
    json.dump(data, f, indent=2)
" "$STATE_FILE" "$LAST_STATE"
    }

    _alert_ops() {
        local message="$1"
        local failures="$2"
        local attempt_time="$3"
        local ALERT_ELAPSED=$(( _now - LAST_ALERT_SENT ))
        if (( ALERT_ELAPSED < 21600 )) && (( LAST_ALERT_SENT != 0 )); then
            _log "Alert suppressed - last alert was $(( ALERT_ELAPSED / 3600 ))h ago (throttle: 6h)"
            return 0
        fi
        _log "Sending ops alert (${failures} consecutive failures)"
        local WEBHOOK_URL="${DATING_OPS_WEBHOOK_URL:-${CHASSIS_OPS_WEBHOOK_URL:-}}"
        if [[ -z "$WEBHOOK_URL" ]]; then
            _log "DATING_OPS_WEBHOOK_URL not set (and CHASSIS_OPS_WEBHOOK_URL not set) - can't send alert"
            return 0
        fi
        local PAYLOAD
        PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'content': sys.argv[1]}))" "$message")
        if curl -sS -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" -d "$PAYLOAD" > /dev/null 2>&1; then
            _log "Alert posted to ops webhook"
            _save_state "$attempt_time" "$failures" "$_now"
        else
            _log "Failed to post alert"
        fi
    }

    # Attempt a PIN unlock and report the outcome. Distinct from the restart
    # path on purpose: a restart CANNOT fix a locked device. A PIN-locked AVD
    # cold-boots straight back into RUNNING_LOCKED - which is exactly how the
    # 2026-07-21 restart loop happened: the watchdog killed the device
    # mid-unlock, the fresh boot came back locked, and the old focus-only
    # readiness check declared the lock screen "ready".
    #
    # NEVER escalate a locked device to `-wipe-data`. Wiping clears the lock
    # but logs the installer out of every dating app, each requiring manual
    # GUI re-auth. Sean, 2026-07-21: "don't wipe the emulator - it's a PITA
    # to get every app authenticated." Do not reintroduce wipe-based recovery
    # here for ANY state this hook handles - wiping is a human decision for a
    # genuinely corrupt AVD, made by the installer, never by automation.
    _attempt_unlock() {
        _log "Device is PIN-locked (reason=emulator_locked) - attempting unlock (PIN from \$${PIN_VAR_NAME}; value never logged)"
        local rc=0
        dating_emulator_unlock 2>> "$LOG_FILE" || rc=$?
        case $rc in
            0) _log "Unlock successful - user state RUNNING_UNLOCKED" ;;
            2) _log "UNLOCK BLOCKED: \$${PIN_VAR_NAME} is not set. The AVD has a lock-screen PIN and cannot recover without it. Wire ${PIN_VAR_NAME} to your secret store (see chassis.config.yaml > modules.dating.emulator.pin_env_var)." ;;
            *) _log "Unlock FAILED - device still locked after timeout" ;;
        esac
        return $rc
    }

    # --- Gate 1: Pause flag ---
    if [[ -f "$PAUSE_FLAG" ]]; then
        _log "EMULATOR_PAUSE flag present at $PAUSE_FLAG - skipping"
        _save_state
        return 0
    fi

    # --- Gate 2: Unlock in flight ---
    # A PIN unlock produces transient bad readings (0-byte screencap, null
    # window focus) while the keyguard tears down. Killing qemu on one of
    # those samples reboots the device straight back to the lock screen and
    # throws away the unlock, so the watchdog stands down entirely while an
    # unlock is running. The flag is ignored once stale (>5 min) so a crashed
    # unlock cannot suppress recovery forever.
    if dating_emulator_unlock_in_flight; then
        _log "PIN unlock in flight - standing down this tick"
        _save_state
        return 0
    fi

    # --- Probe device state ---
    local STATE
    STATE=$(dating_emulator_state)
    LAST_STATE="$STATE"

    # --- Already ready ---
    if [[ "$STATE" == "emulator_ready" ]]; then
        _log "Emulator $AVD_NAME ready (reason=emulator_ready) - no action needed"
        _save_state "$LAST_RESTART_ATTEMPT" 0 "$LAST_ALERT_SENT"
        return 0
    fi

    # --- Locked: unlock in place. Never restart, never wipe. ---
    if [[ "$STATE" == "emulator_locked" ]]; then
        local unlock_rc=0
        _attempt_unlock || unlock_rc=$?
        if (( unlock_rc == 0 )); then
            STATE=$(dating_emulator_state)
            LAST_STATE="$STATE"
            if [[ "$STATE" == "emulator_ready" ]]; then
                _log "Emulator $AVD_NAME ready after unlock (reason=emulator_ready)"
                _save_state "$LAST_RESTART_ATTEMPT" 0 "$LAST_ALERT_SENT"
                return 0
            fi
            _log "Unlocked but not ready yet (reason=$STATE) - re-evaluating next tick"
        fi
        local NEW_FAILURES=$(( CONSECUTIVE_FAILURES + 1 ))
        _save_state "$LAST_RESTART_ATTEMPT" "$NEW_FAILURES" "$LAST_ALERT_SENT"
        if (( unlock_rc == 2 )); then
            # A missing PIN var is a config error no amount of retrying or
            # restarting can fix - alert immediately with the fix, instead of
            # burning through the failure threshold in silence.
            _alert_ops "dating-emulator-recovery: $AVD_NAME is PIN-locked (reason=emulator_locked) and \$${PIN_VAR_NAME} is not set - no automated recovery possible. Set ${PIN_VAR_NAME} from your secret store. Do NOT wipe the AVD - that logs out every dating app." "$NEW_FAILURES" "$LAST_RESTART_ATTEMPT"
        elif (( NEW_FAILURES >= FAILURE_ALERT_THRESHOLD )); then
            _alert_ops "dating-emulator-recovery: $AVD_NAME PIN unlock failed ${NEW_FAILURES}x in a row (reason=emulator_locked). Device is up but no dating app can launch. Do NOT wipe the AVD - that logs out every dating app. Investigate the unlock sequence." "$NEW_FAILURES" "$LAST_RESTART_ATTEMPT"
        fi
        return 1
    fi

    # --- Wedge signals must persist across two samples before we kill ---
    # A single bad sample (null focus, unresolvable apps) can be a transient
    # reading taken during boot settle or a keyguard transition this process
    # cannot see. Acting on one sample is how the 2026-07-21 restart loop
    # started.
    if [[ "$STATE" == "emulator_no_focus" || "$STATE" == "emulator_apps_unresolvable" ]]; then
        _log "Wedge signal (reason=$STATE) - re-sampling in ${WEDGE_CONFIRM_DELAY}s before acting"
        sleep "$WEDGE_CONFIRM_DELAY"
        local STATE2
        STATE2=$(dating_emulator_state)
        if [[ "$STATE2" == "emulator_ready" ]]; then
            _log "Wedge signal cleared on second sample - emulator $AVD_NAME ready"
            LAST_STATE="emulator_ready"
            _save_state "$LAST_RESTART_ATTEMPT" 0 "$LAST_ALERT_SENT"
            return 0
        fi
        if [[ "$STATE2" == "emulator_locked" ]]; then
            # The first sample caught a keyguard transition. Route to the
            # unlock path next tick - restarting a PIN-locked device only
            # boots it locked again.
            _log "Second sample shows RUNNING_LOCKED - deferring to the unlock path next tick (no restart)"
            LAST_STATE="emulator_locked"
            _save_state
            return 1
        fi
        _log "Wedge signal persisted (reason=$STATE2) - proceeding to restart"
        STATE="$STATE2"
        LAST_STATE="$STATE"
    fi

    # --- Gate 3: Cooldown check ---
    local ELAPSED=$(( _now - LAST_RESTART_ATTEMPT ))
    if (( ELAPSED < COOLDOWN_SECONDS )) && (( LAST_RESTART_ATTEMPT > 0 )); then
        local REMAINING=$(( COOLDOWN_SECONDS - ELAPSED ))
        _log "Emulator $AVD_NAME not ready (reason=$STATE) but cooldown active (${REMAINING}s remaining) - skipping"
        _save_state
        return 0
    fi

    # --- Attempt restart ---
    _log "Emulator $AVD_NAME not ready (reason=$STATE) - attempting restart (consecutive_failures=$CONSECUTIVE_FAILURES)"
    local RESTART_ATTEMPT_TIME=$_now

    # Kill any zombie qemu processes for this AVD before starting fresh.
    if pgrep -f "qemu-system.*$AVD_NAME" > /dev/null 2>&1; then
        _log "Killing zombie emulator process before restart"
        pkill -f "qemu-system.*$AVD_NAME" 2>/dev/null || true
        sleep 3
    fi

    # If an installer runs the emulator under launchd KeepAlive (or systemd
    # Restart=always, etc.), the supervisor will have respawned qemu by now.
    # In that case calling START_SCRIPT races the supervisor and consistently
    # bails with "already running", causing BOOT_OK to stay false and any
    # explicit-reboot flag to never clear. Detect the respawn and skip the
    # start call.
    local SUPERVISOR_RESPAWNED=false
    if pgrep -f "qemu-system.*$AVD_NAME" > /dev/null 2>&1; then
        _log "qemu respawned after kill - supervisor (launchd/systemd) owns the lifecycle; skipping START_SCRIPT"
        SUPERVISOR_RESPAWNED=true
    fi

    if [[ "$SUPERVISOR_RESPAWNED" == "false" ]]; then
        if [[ ! -x "$START_SCRIPT" ]]; then
            _log "Emulator start script not executable at $START_SCRIPT - installer must provide it"
            _save_state "$RESTART_ATTEMPT_TIME" $(( CONSECUTIVE_FAILURES + 1 )) "$LAST_ALERT_SENT"
            return 1
        fi
        bash "$START_SCRIPT" start >> "$LOG_FILE" 2>&1 &
        local EMULATOR_PID=$!
        wait "$EMULATOR_PID" 2>/dev/null || true
    fi

    # Wait up to 120 seconds for FULL readiness. A PIN-locked AVD ALWAYS
    # cold-boots into RUNNING_LOCKED, so the unlock is part of coming up -
    # a restart that ends on the lock screen is a failed restart, not a
    # success. (The 2026-07-21 incident's exact false-success: "Restart
    # successful" logged against a device that could not launch one app.)
    local BOOT_OK=false
    local UNLOCK_TRIED=false
    local BOOT_STATE="$STATE"
    local _i
    for _i in $(seq 1 120); do
        sleep 1
        BOOT_STATE=$(dating_emulator_state)
        if [[ "$BOOT_STATE" == "emulator_ready" ]]; then
            BOOT_OK=true
            break
        fi
        if [[ "$BOOT_STATE" == "emulator_locked" && "$UNLOCK_TRIED" == "false" ]]; then
            UNLOCK_TRIED=true
            local boot_unlock_rc=0
            _attempt_unlock || boot_unlock_rc=$?
            if (( boot_unlock_rc == 2 )); then
                # No PIN available - this boot can never reach ready. Bail
                # out of the wait instead of burning 120s to the same end.
                break
            fi
        fi
    done
    LAST_STATE="$BOOT_STATE"

    if [[ "$BOOT_OK" == "true" ]]; then
        _log "Restart successful - emulator $AVD_NAME now ready (reason=emulator_ready)"
        _save_state "$RESTART_ATTEMPT_TIME" 0 "$LAST_ALERT_SENT"
        return 0
    fi

    # --- Restart failed ---
    local NEW_FAILURES=$(( CONSECUTIVE_FAILURES + 1 ))
    _log "Restart FAILED (reason=$BOOT_STATE, consecutive_failures now $NEW_FAILURES)"
    _save_state "$RESTART_ATTEMPT_TIME" "$NEW_FAILURES" "$LAST_ALERT_SENT"

    # --- Alert threshold ---
    if (( NEW_FAILURES >= FAILURE_ALERT_THRESHOLD )); then
        _alert_ops "dating-emulator-recovery: $AVD_NAME AVD failed to restart ${NEW_FAILURES}x in a row (last state: ${BOOT_STATE:-unknown}). Dating activity throttled or skipped. Manual restart: bash $START_SCRIPT start" "$NEW_FAILURES" "$RESTART_ATTEMPT_TIME"
    fi

    return 1
}

# Sentinel for chassis-core hook discovery: this file exports
# chassis_recovery_dating_emulator in its loaded environment.
export -f chassis_recovery_dating_emulator
