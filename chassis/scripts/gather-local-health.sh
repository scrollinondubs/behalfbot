#!/bin/bash
# gather-local-health.sh - Local host posture probe for chassis installs.
#
# Heartbeat-compatible gather script. Checks the chassis runtime
# environment for signs of trouble (disk pressure, memory pressure, stuck
# dispatcher, silent dispatcher) and emits the standard gather JSON
# contract.
#
# Cross-platform: detects Linux vs macOS for memory probing. Disk + lock
# staleness + dispatcher-silence checks use POSIX-friendly commands so
# they work on both.
#
# Output (gather JSON contract):
#   {
#     "count": N,                     # number of issues detected
#     "issues": ["disk_92pct", ...],  # tag list - dispatcher logs verbatim
#     "disk_pct": 47,
#     "available_memory": "8192MB"   | "unknown"
#   }
#
# Issue tags emitted:
#   disk_<pct>pct                  - disk usage > DISK_PCT_THRESHOLD (default 85)
#   low_memory_<n>MB               - available memory < MEM_MB_FLOOR (default 2048)
#   dispatcher_stuck_<n>s          - dispatcher.lock older than LOCK_STALE_S (default 1800)
#   dispatcher_silent_<n>s         - no heartbeat-state update for > SILENCE_S (default 7200)
#
# Customer-specific health checks (Ollama presence, launchd / systemd unit
# state, Mac-Mini-specific peripherals, etc.) live in a sibling hook at
# $CHASSIS_HOME/scheduled-tasks/local-health-hooks.sh - sourced AFTER this
# script's core checks complete, with `issues` available as an array. See
# the customer-hooks pattern in scrollinondubs/behalfbot#74.

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (chassis dispatcher exports it)}"

DISK_PCT_THRESHOLD="${DISK_PCT_THRESHOLD:-85}"
MEM_MB_FLOOR="${MEM_MB_FLOOR:-2048}"
LOCK_STALE_S="${LOCK_STALE_S:-1800}"       # 30 min
SILENCE_S="${SILENCE_S:-7200}"             # 2 hr

issues=()

# 1. Disk usage on root filesystem.
disk_pct=$(df -h / | awk 'NR==2 {gsub(/%/,""); print $5}')
if [[ "$disk_pct" =~ ^[0-9]+$ ]] && [[ $disk_pct -gt $DISK_PCT_THRESHOLD ]]; then
    issues+=("disk_${disk_pct}pct")
fi

# 2. Available memory. Linux + macOS have different probes; skip if neither.
avail_mb="unknown"
if [[ -r /proc/meminfo ]]; then
    # Linux: MemAvailable is the right metric post-3.14 kernel.
    avail_mb=$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
elif command -v vm_stat >/dev/null 2>&1; then
    # macOS: free + inactive pages × page-size.
    avail_mb=$(vm_stat | awk '/Pages free/ {free=$3} /Pages inactive/ {inactive=$3} END {gsub(/\./,"",free); gsub(/\./,"",inactive); print int((free+inactive)*4096/1048576)}')
fi
if [[ "$avail_mb" =~ ^[0-9]+$ ]] && [[ $avail_mb -lt $MEM_MB_FLOOR ]]; then
    issues+=("low_memory_${avail_mb}MB")
fi

# 3. Dispatcher lock staleness.
lock_file="${CHASSIS_HOME}/logs/scheduled/dispatcher.lock"
if [[ -f "$lock_file" ]]; then
    # `stat -c %Y` (GNU) and `stat -f %m` (BSD/macOS) both yield mtime epoch.
    lock_mtime=$(stat -c %Y "$lock_file" 2>/dev/null || stat -f %m "$lock_file" 2>/dev/null || echo 0)
    if [[ "$lock_mtime" =~ ^[0-9]+$ ]] && [[ $lock_mtime -gt 0 ]]; then
        lock_age=$(( $(date +%s) - lock_mtime ))
        if [[ $lock_age -gt $LOCK_STALE_S ]]; then
            issues+=("dispatcher_stuck_${lock_age}s")
        fi
    fi
fi

# 4. Dispatcher silence - last heartbeat-state.json mutation > SILENCE_S ago
# means no heartbeat has been processed (dispatcher dead, or every gather
# script erroring before set_state).
state_file="${CHASSIS_HOME}/scheduled-tasks/heartbeat-state.json"
if [[ -f "$state_file" ]]; then
    last_any=$(jq -r '[.[] | .last_checked // empty] | sort | last // empty' "$state_file" 2>/dev/null || echo "")
    if [[ -n "$last_any" ]]; then
        # macOS `date -j -f` and GNU `date -d`. Try macOS first then fall back.
        last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S" "$last_any" +%s 2>/dev/null || \
                     date -d "$last_any" +%s 2>/dev/null || echo 0)
        if [[ "$last_epoch" =~ ^[0-9]+$ ]] && [[ $last_epoch -gt 0 ]]; then
            gap=$(( $(date +%s) - last_epoch ))
            if [[ $gap -gt $SILENCE_S ]]; then
                issues+=("dispatcher_silent_${gap}s")
            fi
        fi
    fi
fi

# Optional customer extension. Hook receives `issues` as a bash array via
# name reference (zsh+bash compatible scoping). Hook can `issues+=("...")`
# to add findings; the JSON emitter below uses the final array.
HEALTH_HOOK="${CHASSIS_HOME}/scheduled-tasks/local-health-hooks.sh"
if [[ -f "$HEALTH_HOOK" ]]; then
    # shellcheck disable=SC1090
    source "$HEALTH_HOOK" || true
fi

count=${#issues[@]}
if [[ $count -eq 0 ]]; then
    issues_json="[]"
else
    issues_json=$(printf '%s\n' "${issues[@]}" | jq -R . | jq -s .)
fi

jq -n \
    --argjson count "$count" \
    --argjson issues "$issues_json" \
    --arg disk "$disk_pct" \
    --arg memory "${avail_mb}MB" \
    '{
        count: $count,
        issues: $issues,
        disk_pct: ($disk | try tonumber catch null),
        available_memory: $memory
    }'
