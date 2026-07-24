#!/usr/bin/env bash
# gather-container-liveness.sh - alert when a core chassis container is DOWN
# or restart-looping, not just when an in-container service is unreachable.
#
# The gap this closes
# ===================
# gather-service-health.sh probes HTTPS endpoints (curl a URL, expect 200).
# The primary `behalfbot` container serves no HTTP endpoint, so its liveness
# is invisible to that probe. #118 introduces a real boot-failure mode (an
# exit-5 assertion aborts the entrypoint -> the container can crash-loop or
# stay down). An exit-5 boot loop with no alert is the worst outcome of the
# #118 change. This gather adds the missing signal: a direct `docker inspect`
# of each declared container's State.
#
# STRUCTURAL note (read before trusting this for the FULLY-DOWN case)
# ==================================================================
# The dispatcher runs INSIDE the `behalfbot` container. A gather it invokes
# therefore cannot observe `behalfbot` when `behalfbot` is fully dead - the
# dispatcher is not ticking, so nothing runs. This script catches:
#   - sibling containers (postgres, vaultwarden, a discord bridge) down or
#     unhealthy - fully observable from inside behalfbot via the mounted
#     socket;
#   - `behalfbot` RESTART-LOOPING - each crash-restart bumps RestartCount and
#     leaves State.Restarting / non-running windows the gather samples when
#     behalfbot briefly comes up between crashes.
# It does NOT, on its own, catch a `behalfbot` that never completes a single
# tick. That case needs the SAME script run from OUTSIDE the container - a
# host LaunchAgent / systemd timer / cron invoking it against the host docker
# daemon. The script is location-agnostic by design (pure docker CLI + a
# state file); the accompanying prompt documents the host-side wiring. Run it
# in BOTH places for full coverage: in-container catches siblings + loops,
# host-side catches a dead behalfbot within one interval.
#
# Configuration:
#   CHASSIS_LIVENESS_CONTAINERS  Comma-/newline-separated container names to
#                                check. Default: "behalfbot,behalfbot-postgres"
#                                (the two runtime-critical services in the
#                                reference compose stack). Installs add their
#                                own (e.g. a discord bridge) via this var.
#   CHASSIS_LIVENESS_STATE       State file path for restart-loop detection.
#                                Default: $CUSTOMER_HOME/scheduled-tasks/
#                                container-liveness-state.json
#
# Gather JSON contract:
#   { "count": N, "issues": [...], "checked": M, "status": "...",
#     "ts_utc": "..." }
#
# Issue tags (per container <c>):
#   <c>_absent            - no such container (never created / removed)
#   <c>_down_<status>     - State.Status is not "running" (exited|dead|created|paused)
#   <c>_restarting        - State.Restarting == true (mid restart-loop)
#   <c>_restart_loop      - RestartCount increased since the last tick
#   <c>_unhealthy         - healthcheck reports State.Health.Status == unhealthy
#
# Emits no secrets: only container names and states.
#
# docker unreachable is a deliberate NO-OP (count=0, status=docker_unreachable):
# a broken socket is gather-docker-prune.sh's job to flag, and on an install
# that never mounts the socket this heartbeat must not false-alarm every tick.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/_env.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/_env.sh" 2>/dev/null || true
fi
: "${CUSTOMER_HOME:=${CHASSIS_HOME:-}}"

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

emit() {
    # $1 count, $2 issues_json, $3 checked, $4 status
    jq -n \
        --argjson count "$1" \
        --argjson issues "$2" \
        --argjson checked "$3" \
        --arg status "$4" \
        --arg ts "$TS" \
        '{count: $count, issues: $issues, checked: $checked, status: $status, ts_utc: $ts}'
}

# --- Cheap no-op gate: docker must be reachable. If not, do not alarm. -------
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    emit 0 '[]' 0 "docker_unreachable"
    exit 0
fi

# --- Container list ----------------------------------------------------------
RAW="${CHASSIS_LIVENESS_CONTAINERS:-behalfbot,behalfbot-postgres}"
CONTAINERS=()
while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"   # ltrim
    line="${line%"${line##*[![:space:]]}"}"    # rtrim
    [[ -n "$line" ]] && CONTAINERS+=("$line")
done <<< "${RAW//,/$'\n'}"

if (( ${#CONTAINERS[@]} == 0 )); then
    emit 0 '[]' 0 "no_containers_configured"
    exit 0
fi

STATE_FILE="${CHASSIS_LIVENESS_STATE:-${CUSTOMER_HOME:-/tmp}/scheduled-tasks/container-liveness-state.json}"
if [[ ! -f "$STATE_FILE" ]]; then
    mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
    echo '{}' > "$STATE_FILE" 2>/dev/null || STATE_FILE=""
fi

issues=()
checked=0
state_updates=()

for c in "${CONTAINERS[@]}"; do
    checked=$((checked + 1))

    # One inspect, pipe-delimited. Health may be absent (no healthcheck); the
    # template prints "<no value>" then, which we normalise to "none".
    if ! info=$(docker inspect \
        -f '{{.State.Status}}|{{.State.Restarting}}|{{.RestartCount}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$c" 2>/dev/null); then
        issues+=("${c}_absent")
        continue
    fi

    IFS='|' read -r status restarting restart_count health <<< "$info"
    [[ "$restart_count" =~ ^[0-9]+$ ]] || restart_count=0

    # Restart-loop detection: RestartCount is cumulative since creation, so an
    # absolute value proves nothing. A DELTA since the previous tick means the
    # container restarted within the last interval - a live crash-loop.
    prior=0
    if [[ -n "$STATE_FILE" && -f "$STATE_FILE" ]]; then
        prior=$(jq -r --arg n "$c" '.[$n].restart_count // 0' "$STATE_FILE" 2>/dev/null || echo 0)
        [[ "$prior" =~ ^[0-9]+$ ]] || prior=0
    fi
    state_updates+=(".\"${c}\" = {\"restart_count\": ${restart_count}}")

    if [[ "$status" != "running" ]]; then
        issues+=("${c}_down_${status}")
    elif [[ "$restarting" == "true" ]]; then
        issues+=("${c}_restarting")
    elif [[ "$restart_count" -gt "$prior" ]]; then
        issues+=("${c}_restart_loop")
    elif [[ "$health" == "unhealthy" ]]; then
        issues+=("${c}_unhealthy")
    fi
done

# Persist restart counts for next-tick delta comparison.
if [[ -n "$STATE_FILE" && -f "$STATE_FILE" && ${#state_updates[@]} -gt 0 ]]; then
    filter=$(printf '%s | ' "${state_updates[@]}")
    filter="${filter% | }"
    tmp="${STATE_FILE}.tmp"
    if jq "$filter" "$STATE_FILE" > "$tmp" 2>/dev/null; then
        mv "$tmp" "$STATE_FILE"
    else
        rm -f "$tmp" 2>/dev/null || true
    fi
fi

count=${#issues[@]}
if [[ $count -eq 0 ]]; then
    issues_json="[]"
    status_out="ok"
else
    issues_json=$(printf '%s\n' "${issues[@]}" | jq -R . | jq -s .)
    status_out="containers_unhealthy"
fi

emit "$count" "$issues_json" "$checked" "$status_out"
