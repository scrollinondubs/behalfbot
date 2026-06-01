#!/usr/bin/env bash
# dating-emulator-recovery.sh — chassis recovery hook for the dating
# Android emulator (AVD).
#
# This file is sourced by the chassis heartbeat dispatcher at startup, which
# discovers all *.sh files in plugins/*/scheduled-tasks/recovery-hooks.d/ and
# sources them so they can register `chassis_recovery_*` functions in the
# dispatcher's environment.
#
# Sourcing contract: this script MUST NOT execute side-effects at source time.
# It only declares the function. The dispatcher (or an admin) calls the
# function explicitly when recovery is needed.
#
# Function: chassis_recovery_dating_emulator
#   Self-healing watchdog for the dating Android emulator.
#   - Respects ${CHASSIS_HOME}/plugins/dating/EMULATOR_PAUSE flag
#   - Skips if emulator is already ready
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
#
# V1 reference: <v1-reference-install> `scripts/emulator-watchdog.sh`. This
# chassis port keeps the same logic, swaps installer-specific paths /
# AVD-name / webhook for chassis env vars, and packages it as a sourceable
# function rather than a standalone launchd target.

# Guard against re-sourcing.
if declare -F chassis_recovery_dating_emulator >/dev/null; then
    return 0
fi

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
}
with open(sys.argv[1], 'w') as f:
    json.dump(data, f, indent=2)
" "$STATE_FILE"
    }

    # --- Gate 1: Pause flag ---
    if [[ -f "$PAUSE_FLAG" ]]; then
        _log "EMULATOR_PAUSE flag present at $PAUSE_FLAG — skipping"
        _save_state
        return 0
    fi

    # --- Emulator status check ---
    _emulator_ready() {
        if ! command -v adb >/dev/null 2>&1; then
            return 1
        fi
        if ! adb devices 2>/dev/null | grep -q "emulator-5554"; then
            return 1
        fi
        if ! adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' | grep -q "^1$"; then
            return 1
        fi
        return 0
    }

    # --- Gate 2: Already running ---
    if _emulator_ready; then
        _log "Emulator $AVD_NAME ready — no action needed"
        _save_state "$LAST_RESTART_ATTEMPT" 0 "$LAST_ALERT_SENT"
        return 0
    fi

    # --- Gate 3: Cooldown check ---
    local ELAPSED=$(( _now - LAST_RESTART_ATTEMPT ))
    if (( ELAPSED < COOLDOWN_SECONDS )) && (( LAST_RESTART_ATTEMPT > 0 )); then
        local REMAINING=$(( COOLDOWN_SECONDS - ELAPSED ))
        _log "Emulator $AVD_NAME down but cooldown active (${REMAINING}s remaining) — skipping"
        _save_state
        return 0
    fi

    # --- Attempt restart ---
    _log "Emulator $AVD_NAME not ready — attempting restart (consecutive_failures=$CONSECUTIVE_FAILURES)"
    local RESTART_ATTEMPT_TIME=$_now

    # Kill any zombie qemu processes for this AVD before starting fresh.
    if pgrep -f "qemu-system.*$AVD_NAME" > /dev/null 2>&1; then
        _log "Killing zombie emulator process before restart"
        pkill -f "qemu-system.*$AVD_NAME" 2>/dev/null || true
        sleep 2
    fi

    if [[ ! -x "$START_SCRIPT" ]]; then
        _log "Emulator start script not executable at $START_SCRIPT — installer must provide it"
        _save_state "$RESTART_ATTEMPT_TIME" $(( CONSECUTIVE_FAILURES + 1 )) "$LAST_ALERT_SENT"
        return 1
    fi

    bash "$START_SCRIPT" start >> "$LOG_FILE" 2>&1 &
    local EMULATOR_PID=$!

    # Wait up to 60 seconds for boot.
    local BOOT_OK=false
    local _i
    for _i in $(seq 1 60); do
        sleep 1
        if _emulator_ready; then
            BOOT_OK=true
            break
        fi
    done
    wait "$EMULATOR_PID" 2>/dev/null || true

    if [[ "$BOOT_OK" == "true" ]]; then
        _log "Restart successful — emulator $AVD_NAME now ready"
        _save_state "$RESTART_ATTEMPT_TIME" 0 "$LAST_ALERT_SENT"
        return 0
    fi

    # --- Restart failed ---
    local NEW_FAILURES=$(( CONSECUTIVE_FAILURES + 1 ))
    _log "Restart FAILED (consecutive_failures now $NEW_FAILURES)"
    _save_state "$RESTART_ATTEMPT_TIME" "$NEW_FAILURES" "$LAST_ALERT_SENT"

    # --- Alert threshold ---
    if (( NEW_FAILURES >= FAILURE_ALERT_THRESHOLD )); then
        # Throttle alerts: don't re-alert more than once per 6 hours.
        local ALERT_ELAPSED=$(( _now - LAST_ALERT_SENT ))
        if (( ALERT_ELAPSED >= 21600 )) || (( LAST_ALERT_SENT == 0 )); then
            _log "Sending ops alert (${NEW_FAILURES} consecutive failures)"
            local WEBHOOK_URL="${DATING_OPS_WEBHOOK_URL:-${CHASSIS_OPS_WEBHOOK_URL:-}}"
            if [[ -n "$WEBHOOK_URL" ]]; then
                local MESSAGE
                MESSAGE="dating-emulator-recovery: $AVD_NAME AVD failed to restart ${NEW_FAILURES}x in a row. Dating activity throttled or skipped. Manual restart: bash $START_SCRIPT start"
                local PAYLOAD
                PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'content': sys.argv[1]}))" "$MESSAGE")
                if curl -sS -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" -d "$PAYLOAD" > /dev/null 2>&1; then
                    _log "Alert posted to ops webhook"
                    _save_state "$RESTART_ATTEMPT_TIME" "$NEW_FAILURES" "$_now"
                else
                    _log "Failed to post alert"
                fi
            else
                _log "DATING_OPS_WEBHOOK_URL not set (and CHASSIS_OPS_WEBHOOK_URL not set) — can't send alert"
            fi
        else
            _log "Alert suppressed — last alert was $(( ALERT_ELAPSED / 3600 ))h ago (throttle: 6h)"
        fi
    fi

    return 1
}

# Sentinel for chassis-core hook discovery: this file exports
# chassis_recovery_dating_emulator in its loaded environment.
export -f chassis_recovery_dating_emulator
