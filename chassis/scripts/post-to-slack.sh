#!/bin/bash
# post-to-slack.sh - post a message to a Slack channel via chat.postMessage.
#
# Sibling to post-to-channel.sh (Discord) + post-to-telegram.sh. Same
# channel-key convention.
#
# Usage:
#   post-to-slack.sh "<channel-key>" "<message text>"
#   post-to-slack.sh ops "Restarted vaultwarden after OOM"
#   post-to-slack.sh briefings "Daily briefing ready: <link>"
#   post-to-slack.sh ops "..." --markdown      # mrkdwn=true (default)
#   post-to-slack.sh ops "..." --thread-ts TS  # reply in thread
#
# Channel keys resolve to env vars:
#   ops          -> ${INSTANCE_NAME}_OPS_SLACK_CHANNEL_ID
#   briefings    -> ${INSTANCE_NAME}_BRIEFINGS_SLACK_CHANNEL_ID
#   leads        -> ${INSTANCE_NAME}_LEADS_SLACK_CHANNEL_ID
#   admin        -> ${INSTANCE_NAME}_ADMIN_SLACK_CHANNEL_ID
#   <custom>     -> upper-cased + _SLACK_CHANNEL_ID suffix
#
# Required env (sourced from $CHASSIS_HOME/.env):
#   SLACK_BOT_TOKEN        xoxb-... bot user OAuth token
#   INSTANCE_NAME          prefix for resolving the per-channel channel_id env var
#
# Exit codes:
#   0 - sent OK
#   2 - bad invocation / missing env
#   3 - Slack API returned not ok

set -euo pipefail

CHANNEL="${1:?usage: post-to-slack.sh <channel-key> <message> [--markdown] [--thread-ts TS]}"
MESSAGE="${2:?message text required}"
shift 2 || true

USE_MRKDWN="true"  # Slack defaults to mrkdwn=true; keep that
THREAD_TS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --markdown)  USE_MRKDWN="true"; shift ;;
        --plain)     USE_MRKDWN="false"; shift ;;
        --thread-ts) THREAD_TS="${2:?--thread-ts requires a timestamp}"; shift 2 ;;
        *)           echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

CHASSIS_HOME="${CHASSIS_HOME:?CHASSIS_HOME must be set}"

if [[ -f "$CHASSIS_HOME/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; . "$CHASSIS_HOME/.env"; set +a
fi

: "${SLACK_BOT_TOKEN:?SLACK_BOT_TOKEN not in customer .env}"

CHANNEL_UPPER=$(printf '%s' "$CHANNEL" | tr '[:lower:]' '[:upper:]')
INSTANCE_PREFIX=""
if [[ -n "${INSTANCE_NAME:-}" ]]; then
    INSTANCE_PREFIX="$(printf '%s' "$INSTANCE_NAME" | tr '[:lower:]' '[:upper:]')_"
fi

CHANNEL_VAR="${INSTANCE_PREFIX}${CHANNEL_UPPER}_SLACK_CHANNEL_ID"
CHANNEL_ID="${!CHANNEL_VAR:-}"

if [[ -z "$CHANNEL_ID" && -n "$INSTANCE_PREFIX" ]]; then
    FALLBACK_VAR="${CHANNEL_UPPER}_SLACK_CHANNEL_ID"
    CHANNEL_ID="${!FALLBACK_VAR:-}"
fi

if [[ -z "$CHANNEL_ID" ]]; then
    echo "no Slack channel_id configured for channel '$CHANNEL' (tried $CHANNEL_VAR)" >&2
    exit 2
fi

API_URL="https://slack.com/api/chat.postMessage"

PAYLOAD=$(jq -n \
    --arg channel "$CHANNEL_ID" \
    --arg text "$MESSAGE" \
    --arg thread "$THREAD_TS" \
    --argjson mrkdwn "$USE_MRKDWN" \
    '{
        channel: $channel,
        text: $text,
        mrkdwn: $mrkdwn
    }
    + (if $thread != "" then {thread_ts: $thread} else {} end)
    ')

RESPONSE=$(curl -fsS -X POST \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    -H "Content-Type: application/json; charset=utf-8" \
    -d "$PAYLOAD" "$API_URL" 2>&1) || {
    echo "Slack API call failed: $RESPONSE" >&2
    exit 3
}

if ! echo "$RESPONSE" | jq -e '.ok' >/dev/null 2>&1; then
    err=$(echo "$RESPONSE" | jq -r '.error // .')
    echo "Slack API returned not-ok: $err" >&2
    exit 3
fi

TS=$(echo "$RESPONSE" | jq -r '.ts')
echo "sent (channel=$CHANNEL_ID, ts=$TS)"
