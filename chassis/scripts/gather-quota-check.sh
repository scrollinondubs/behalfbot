#!/bin/bash
# gather-quota-check.sh - Anthropic Claude session-quota probe.
#
# Heartbeat-compatible gather script. Reads the active session block via
# `ccusage blocks --active --json`, computes utilization against the
# configured per-session token cap, and decides whether conservation mode
# should engage (soft brake), hard-stop (no further fires), or auto-disable
# at a new billing block boundary.
#
# Conservation mode is honored by the chassis dispatcher: when
# `$CHASSIS_HOME/scheduled-tasks/conservation-mode.json` has `enabled:
# true`, non-critical heartbeats are suspended for the rest of the session.
#
# Output (heartbeat gather JSON contract):
#   {
#     "count": 0 | 1,                      # 1 → dispatcher fires conservation-mode flip
#     "action": "" | "enable" | "disable",
#     "trigger": "" | "soft_<PCT>pct" | "hard_<PCT>pct",
#     "utilization_pct": <float>,
#     "threshold_pct": <int>,
#     "hard_pct": <int>,
#     "time_elapsed_pct": <float>,
#     "remaining_minutes": <int>,
#     "total_tokens_M": <int>,
#     "session_limit_M": <int>,
#     "cost_usd": <float>,
#     "conservation_currently_on": <bool>
#   }
#
# Decision logic:
#   - Hard brake (UNCONDITIONAL): utilization >= HARD_PCT (default 95)
#     → conservation mode ON. Catches the late-block case where ANY further
#     heartbeat fire risks consuming overage credits.
#   - Soft brake: utilization >= ENABLE_PCT (default 50) AND remaining_min
#     > REMAINING_MIN_FLOOR (default 60) → conservation mode ON. Time
#     floor avoids a useless flip at end-of-block.
#   - Auto-disable: when ccusage reports a new block startTime (different
#     from the stored value) AND conservation is currently ON, the script
#     re-flips it OFF for the fresh session.
#
# Tunable env vars (all optional with sensible defaults):
#   SESSION_TOKEN_LIMIT    - raw-token cap per session (default 760000000,
#                            matches Max-tier observation; lower for Pro).
#   ENABLE_PCT             - soft-brake utilization threshold (default 50).
#   HARD_PCT               - hard-stop utilization threshold (default 95).
#   REMAINING_MIN_FLOOR    - soft brake only triggers when more than this
#                            many minutes remain in the block (default 60).
#
# SESSION_TOKEN_LIMIT calibration note:
#   The Anthropic dashboard reports utilization as a percentage on the iOS
#   Usage page. Compare ccusage's `totalTokens` for the active block
#   against that percentage to back-derive your raw-token cap. The V1
#   reference install calibrated to ~760M at one observation point
#   (29% reported = 220M ccusage tokens → 760M cap). Anthropic plan tiers
#   differ; re-spot-check whenever the dashboard says a different number
#   than this script expects.
#
# Why raw-token cap (not cost-weighted):
#   ccusage's `totalTokens` is the raw sum (cache reads + writes counted
#   1:1 despite billing differently). Anthropic's dashboard uses the same
#   raw figure for the session-utilization gauge, so comparing apples to
#   apples requires the cap to also be raw-token. Don't try to
#   cost-weight inside this script - calibrate against the dashboard.

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (chassis dispatcher exports it)}"

CONSERVATION_FILE="${CHASSIS_HOME}/scheduled-tasks/conservation-mode.json"
QUOTA_BLOCK_STATE="${CHASSIS_HOME}/scheduled-tasks/quota-last-block-start.txt"

SESSION_TOKEN_LIMIT="${SESSION_TOKEN_LIMIT:-760000000}"  # 760M raw tokens (Max-tier 5h session)
ENABLE_PCT="${ENABLE_PCT:-50}"
HARD_PCT="${HARD_PCT:-95}"
REMAINING_MIN_FLOOR="${REMAINING_MIN_FLOOR:-60}"

# Probe ccusage. If ccusage isn't installed / network unreachable, exit
# silently (count=0) - we'd rather drop a quota check than have the
# dispatcher noisy about a non-actionable failure.
block_data=$(npx ccusage blocks --active --json 2>/dev/null | jq '.blocks[0]' 2>/dev/null) || {
    echo '{"count": 0, "reason": "ccusage failed"}'
    exit 0
}

if [[ -z "$block_data" || "$block_data" == "null" ]]; then
    echo '{"count": 0, "reason": "no active block"}'
    exit 0
fi

total_tokens=$(echo "$block_data" | jq -r '.totalTokens // 0')
remaining_min=$(echo "$block_data" | jq -r '.projection.remainingMinutes // 0')
cost_usd=$(echo "$block_data" | jq -r '.costUSD // 0')
block_start=$(echo "$block_data" | jq -r '.startTime // ""')

# Utilization percentage of the raw-token cap.
utilization_pct=$(echo "$total_tokens $SESSION_TOKEN_LIMIT" | awk '{printf "%.1f", ($1 / $2) * 100}')
threshold_pct="$ENABLE_PCT"

conservation_enabled="false"
if [[ -f "$CONSERVATION_FILE" ]]; then
    conservation_enabled=$(jq -r '.enabled // false' "$CONSERVATION_FILE")
fi

# Elapsed % of a nominal 5hr (300min) session window.
elapsed_min=$(echo "$remaining_min" | awk '{printf "%.0f", 300 - $1}')
time_pct=$(echo "$elapsed_min" | awk '{printf "%.1f", ($1 / 300) * 100}')

# Detect new billing block by comparing startTime to last seen.
new_block_detected="false"
stored_block_start=""
if [[ -f "$QUOTA_BLOCK_STATE" ]]; then
    stored_block_start=$(cat "$QUOTA_BLOCK_STATE" 2>/dev/null || echo "")
fi
if [[ -n "$block_start" && "$block_start" != "$stored_block_start" ]]; then
    new_block_detected="true"
    echo "$block_start" > "$QUOTA_BLOCK_STATE"
fi

should_enable="false"
should_disable="false"
trigger_kind=""

remaining_int=$(printf "%.0f" "$remaining_min")
util_int=$(printf "%.0f" "$utilization_pct")

# Hard brake first - fires regardless of remaining time.
if [[ $util_int -ge $HARD_PCT ]]; then
    should_enable="true"
    trigger_kind="hard_${HARD_PCT}pct"
elif [[ $util_int -ge $ENABLE_PCT && $remaining_int -gt $REMAINING_MIN_FLOOR ]]; then
    should_enable="true"
    trigger_kind="soft_${ENABLE_PCT}pct"
fi

# Auto-disable when a new billing block starts - old conservation state is stale.
if [[ "$conservation_enabled" == "true" && "$new_block_detected" == "true" ]]; then
    should_disable="true"
fi

action_count=0
action=""

if [[ "$should_enable" == "true" && "$conservation_enabled" != "true" ]]; then
    action_count=1
    action="enable"
elif [[ "$should_disable" == "true" && "$conservation_enabled" == "true" ]]; then
    action_count=1
    action="disable"
fi

session_limit_M=$(echo "$SESSION_TOKEN_LIMIT" | awk '{printf "%.0f", $1 / 1000000}')

jq -n \
    --argjson count "$action_count" \
    --arg action "$action" \
    --arg trigger "$trigger_kind" \
    --arg utilization_pct "$utilization_pct" \
    --arg threshold_pct "$threshold_pct" \
    --arg hard_pct "$HARD_PCT" \
    --arg time_pct "$time_pct" \
    --arg remaining_min "$remaining_min" \
    --arg total_tokens "$total_tokens" \
    --arg cost_usd "$cost_usd" \
    --arg conservation_on "$conservation_enabled" \
    --argjson session_limit_M "$session_limit_M" \
    '{
        count: $count,
        action: $action,
        trigger: $trigger,
        hard_pct: ($hard_pct | tonumber),
        utilization_pct: ($utilization_pct | tonumber),
        threshold_pct: ($threshold_pct | tonumber),
        time_elapsed_pct: ($time_pct | tonumber),
        remaining_minutes: ($remaining_min | tonumber),
        total_tokens_M: (($total_tokens | tonumber) / 1000000 | floor),
        session_limit_M: $session_limit_M,
        cost_usd: ($cost_usd | tonumber),
        conservation_currently_on: ($conservation_on == "true")
    }'
