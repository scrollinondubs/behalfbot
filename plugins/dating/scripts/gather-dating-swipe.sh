#!/usr/bin/env bash
# gather-dating-swipe.sh - heartbeat gather: is the dating AVD actually able
# to run a swipe session?
#
# Output MUST be JSON - the dispatcher parses object output via jq. A bare
# `count=N` key=value string falls through to the line-count fallback and
# fires every tick.
#
# Contract:
#   {"count": 1, "reason": "emulator_ready"}     device can launch the apps
#   {"count": 0, "reason": "<state>"}            anything else
#
# The reason token comes from dating_emulator_state (see
# scripts/emulator-state.sh for the full token list) and is machine-readable
# on purpose: emulator_not_running wants a start, emulator_locked wants a PIN
# unlock, emulator_no_focus wants a restart. Callers that collapse them pick
# the wrong remedy - restarting a PIN-locked device boots it locked again.
#
# Why not just `adb devices` + `sys.boot_completed`: both are TRUE on a
# PIN-locked device that cannot launch a single app (2026-07-21 incident -
# the legacy two-check gather reported emulator_ready three consecutive times
# against exactly that device). Readiness must prove the apps resolve.
#
# This probe is read-only: it never restarts, unlocks, or otherwise mutates
# the device. Recovery belongs to chassis_recovery_dating_emulator.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=emulator-state.sh
source "$SCRIPT_DIR/emulator-state.sh"

STATE=$(dating_emulator_state)

if [[ "$STATE" == "emulator_ready" ]]; then
    echo "{\"count\": 1, \"reason\": \"$STATE\"}"
else
    echo "{\"count\": 0, \"reason\": \"$STATE\"}"
fi
