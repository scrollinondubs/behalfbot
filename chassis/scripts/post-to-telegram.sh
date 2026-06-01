#!/bin/bash
# post-to-telegram.sh - post a message to a Telegram chat or supergroup thread.
#
# Sibling to post-to-channel.sh (Discord). Same channel-key convention so
# heartbeats can fan out to multiple surfaces without per-platform branching.
#
# Usage:
#   post-to-telegram.sh "<channel-key>" "<message text>"
#   post-to-telegram.sh ops "Restarted vaultwarden after OOM"
#   post-to-telegram.sh briefings "Daily briefing ready: <link>"
#   post-to-telegram.sh ops "..." --silent      # no notification ping
#   post-to-telegram.sh ops "..." --markdown    # parse_mode=MarkdownV2
#
# Channel keys resolve to env vars in the installer .env:
#   ops          -> ${INSTANCE_NAME}_OPS_TELEGRAM_CHAT_ID
#   briefings    -> ${INSTANCE_NAME}_BRIEFINGS_TELEGRAM_CHAT_ID
#   leads        -> ${INSTANCE_NAME}_LEADS_TELEGRAM_CHAT_ID
#   admin        -> ${INSTANCE_NAME}_ADMIN_TELEGRAM_CHAT_ID
#   <custom>     -> upper-cased + _TELEGRAM_CHAT_ID suffix
#
# For supergroups-with-topics: append "/<thread_id>" to the chat_id in the
# env var, e.g. MARC_OPS_TELEGRAM_CHAT_ID="-1001234567890/47" routes to
# message_thread_id=47 within the supergroup.
#
# Required env (sourced from $CHASSIS_HOME/.env):
#   TELEGRAM_BOT_TOKEN     bot token from @BotFather
#   INSTANCE_NAME          prefix for resolving the per-channel chat_id env var
#
# Exit codes:
#   0 - sent OK
#   2 - bad invocation / missing env
#   3 - Telegram API returned non-ok

set -euo pipefail

CHANNEL="${1:?usage: post-to-telegram.sh <channel-key> <message> [--silent] [--markdown]}"
MESSAGE="${2:?message text required}"
shift 2 || true

SILENT="false"
PARSE_MODE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --silent)   SILENT="true"; shift ;;
        --markdown) PARSE_MODE="MarkdownV2"; shift ;;
        --html)     PARSE_MODE="HTML"; shift ;;
        *)          echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

CHASSIS_HOME="${CHASSIS_HOME:?CHASSIS_HOME must be set}"

if [[ -f "$CHASSIS_HOME/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; . "$CHASSIS_HOME/.env"; set +a
fi

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN not in customer .env}"

CHANNEL_UPPER=$(printf '%s' "$CHANNEL" | tr '[:lower:]' '[:upper:]')
INSTANCE_PREFIX=""
if [[ -n "${INSTANCE_NAME:-}" ]]; then
    INSTANCE_PREFIX="$(printf '%s' "$INSTANCE_NAME" | tr '[:lower:]' '[:upper:]')_"
fi

CHAT_VAR="${INSTANCE_PREFIX}${CHANNEL_UPPER}_TELEGRAM_CHAT_ID"
CHAT_TARGET="${!CHAT_VAR:-}"

if [[ -z "$CHAT_TARGET" && -n "$INSTANCE_PREFIX" ]]; then
    FALLBACK_VAR="${CHANNEL_UPPER}_TELEGRAM_CHAT_ID"
    CHAT_TARGET="${!FALLBACK_VAR:-}"
fi

if [[ -z "$CHAT_TARGET" ]]; then
    echo "no chat_id configured for channel '$CHANNEL' (tried $CHAT_VAR)" >&2
    exit 2
fi

# Split CHAT_TARGET into chat_id + optional message_thread_id (forum topic)
CHAT_ID="${CHAT_TARGET%%/*}"
THREAD_ID=""
if [[ "$CHAT_TARGET" == */* ]]; then
    THREAD_ID="${CHAT_TARGET##*/}"
fi

API_URL="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"

# Build JSON payload via jq to escape safely
PAYLOAD=$(jq -n \
    --arg chat_id "$CHAT_ID" \
    --arg text "$MESSAGE" \
    --arg thread "$THREAD_ID" \
    --arg parse "$PARSE_MODE" \
    --argjson silent "$([[ "$SILENT" == "true" ]] && echo true || echo false)" \
    '{
        chat_id: ($chat_id | tonumber? // $chat_id),
        text: $text,
        disable_notification: $silent
    }
    + (if $thread != "" then {message_thread_id: ($thread | tonumber)} else {} end)
    + (if $parse != "" then {parse_mode: $parse} else {} end)
    ')

RESPONSE=$(curl -fsS -X POST -H "Content-Type: application/json" \
    -d "$PAYLOAD" "$API_URL" 2>&1) || {
    echo "Telegram API call failed: $RESPONSE" >&2
    exit 3
}

if ! echo "$RESPONSE" | jq -e '.ok' >/dev/null 2>&1; then
    echo "Telegram API returned not-ok: $RESPONSE" >&2
    exit 3
fi

MSG_ID=$(echo "$RESPONSE" | jq -r '.result.message_id')
echo "sent (chat_id=$CHAT_ID${THREAD_ID:+, thread=$THREAD_ID}, message_id=$MSG_ID)"
