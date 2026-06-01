#!/bin/bash
# reconcile-heartbeats.sh — independent audit overlay for the chassis
# heartbeat dispatcher.
#
# Why this exists: the chassis dispatcher is a long-running daemon (in a
# container or under systemd). If it dies, hangs, or skips ticks for any
# reason, the install loses observability — nothing else watches the
# dispatcher's own health.
#
# This script runs on a separate cadence (default every 15 min via launchd /
# systemd, INDEPENDENT of the dispatcher itself) and audits the dispatcher's
# state file. For each heartbeat registered in HEARTBEATS.md, computes how
# stale `last_checked` is relative to the heartbeat's declared cadence. If
# the staleness exceeds STALE_MULTIPLIER intervals, the heartbeat is recorded
# in the findings log.
#
# Brings <v1-reference-install> PR #608 + #620 + #617 (2026-05-21) upstream into chassis core.
#
# Writes one JSONL line per reconciler tick to:
#   ${CHASSIS_HOME}/logs/scheduled/heartbeat-reconciler-findings.jsonl
# Schema:
#   {ts: "<ISO-Z>", stale_count: N, stale: [{name, schedule, interval_min, age_min}, ...]}
# A healthy tick still writes a line (stale_count=0, stale=[]) - the
# continuous timeline is needed for post-hoc divergence analysis.
#
# No alerting / Discord posting by design. The reconciler initially alerted
# on every tick that found stale heartbeats; in practice that produced
# repeating alerts every 15 min for the same handful of stuck heartbeats,
# drowning out real signal. The current mode logs continuously and leaves
# divergence triage to a separate overlay tool (or a daily summary).
#
# Configuration (environment-driven):
#   CHASSIS_HOME         REQUIRED. Install root (HEARTBEATS.md + state).
#   STALE_MULTIPLIER     Threshold multiplier (default 3). A heartbeat with
#                        schedule "every 15m" is flagged stale when
#                        last_checked > 45 min ago.
#
# Recommended install wiring: a launchd plist (macOS) / systemd timer (Linux)
# fires this script every 15 min. The cadence is independent of the
# dispatcher itself so a dead dispatcher doesn't take the reconciler with it.

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be exported before running the reconciler}"

HEARTBEATS_FILE="$CHASSIS_HOME/HEARTBEATS.md"
STATE_FILE="$CHASSIS_HOME/scheduled-tasks/heartbeat-state.json"
STALE_MULTIPLIER="${STALE_MULTIPLIER:-3}"

if [[ ! -f "$HEARTBEATS_FILE" ]] || [[ ! -f "$STATE_FILE" ]]; then
    echo "ERR: missing state — HEARTBEATS.md=$HEARTBEATS_FILE STATE=$STATE_FILE" >&2
    exit 2
fi

# Convert a schedule string to interval-seconds. Supported:
#   "every Nm"        -> N*60
#   "every Nh"        -> N*3600
#   "daily HH:MM ..." -> 86400
#   "weekly DAY ..."  -> 604800
# Anything else falls through to 0 (skipped — no reconciliation possible).
schedule_to_seconds() {
    local sched="$1"
    if [[ "$sched" =~ ^every[[:space:]]+([0-9]+)m ]]; then
        echo $(( ${BASH_REMATCH[1]} * 60 ))
    elif [[ "$sched" =~ ^every[[:space:]]+([0-9]+)h ]]; then
        echo $(( ${BASH_REMATCH[1]} * 3600 ))
    elif [[ "$sched" == daily* ]]; then
        echo 86400
    elif [[ "$sched" == weekly* ]]; then
        echo 604800
    else
        echo 0
    fi
}

# Parse HEARTBEATS.md heartbeat names + schedules. Each block looks like:
#   ## name
#   ...
#   ```yaml
#   schedule: every 10m
#   ```
# Skips heartbeat blocks wrapped in HTML comments (<!-- ... -->) so disabled
# heartbeats don't generate false-positive stale alerts (<v1-reference-install> PR #617).
parse_heartbeats() {
    awk '
        in_html_comment { if (/-->/) in_html_comment=0; next }
        /<!--.*-->/ { next }
        /<!--/ { in_html_comment=1; next }
        /^## / { name=$2; sched=""; in_yaml=0; next }
        /^```yaml/ { if (name != "") { in_yaml=1; next } }
        /^```/ && in_yaml { in_yaml=0; if (name != "" && sched != "") printf "%s\t%s\n", name, sched; name=""; sched="" }
        in_yaml && /^schedule:[[:space:]]/ { sub(/^schedule:[[:space:]]+/, ""); sched=$0 }
    ' "$HEARTBEATS_FILE"
}

FINDINGS_LOG="$CHASSIS_HOME/logs/scheduled/heartbeat-reconciler-findings.jsonl"
mkdir -p "$(dirname "$FINDINGS_LOG")"

now_epoch=$(date +%s)
stale_records=()

while IFS=$'\t' read -r name schedule; do
    [[ -z "$name" || -z "$schedule" ]] && continue
    interval=$(schedule_to_seconds "$schedule")
    [[ $interval -eq 0 ]] && continue

    last_checked=$(jq -r --arg n "$name" '.[$n].last_checked // ""' "$STATE_FILE")
    if [[ -z "$last_checked" ]]; then
        # Never checked. For new heartbeats this is normal; only flag if
        # the dispatcher's been running for more than one interval.
        continue
    fi

    # Parse ISO timestamp — Mac (date -j) + Linux (date -d) both supported.
    last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S" "$last_checked" +%s 2>/dev/null || \
                 date -d "$last_checked" +%s 2>/dev/null || echo 0)
    [[ $last_epoch -eq 0 ]] && continue

    age=$(( now_epoch - last_epoch ))
    threshold=$(( interval * STALE_MULTIPLIER ))

    if [[ $age -gt $threshold ]]; then
        age_min=$(( age / 60 ))
        interval_min=$(( interval / 60 ))
        record=$(jq -nc --arg n "$name" --arg s "$schedule" --argjson im "$interval_min" --argjson am "$age_min" \
            '{name:$n, schedule:$s, interval_min:$im, age_min:$am}')
        stale_records+=("$record")
    fi
done < <(parse_heartbeats)

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if [[ ${#stale_records[@]} -eq 0 ]]; then
    stale_json='[]'
else
    stale_json=$(printf '%s\n' "${stale_records[@]}" | jq -sc '.')
fi

jq -nc --arg ts "$ts" --argjson stale "$stale_json" \
    '{ts:$ts, stale_count: ($stale | length), stale: $stale}' >> "$FINDINGS_LOG"
