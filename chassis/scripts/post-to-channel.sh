#!/bin/bash
# post-to-channel.sh — post a Discord message to a channel via webhook.
#
# Usage:
#   post-to-channel.sh "<channel-key>" "<message text>"
#   post-to-channel.sh ops "Restarted vaultwarden after OOM"
#   post-to-channel.sh briefings "Daily briefing ready: <link>"
#
# Channel keys map to webhook env vars in the installer's .env:
#   ops          → ${INSTANCE_NAME}_OPS_WEBHOOK_URL
#   briefings    → ${INSTANCE_NAME}_BRIEFINGS_WEBHOOK_URL
#   leads        → ${INSTANCE_NAME}_LEADS_WEBHOOK_URL
#   social       → ${INSTANCE_NAME}_SOCIAL_WEBHOOK_URL
#   <custom>     → upper-cased + suffixed with _WEBHOOK_URL
#
# So if INSTANCE_NAME=OZZY, post-to-channel.sh ops "..." reads OZZY_OPS_WEBHOOK_URL.
# If INSTANCE_NAME unset, falls back to a flat <KEY>_WEBHOOK_URL convention.
#
# Environment:
#   CHASSIS_HOME    (required) — sources .env from here
#   INSTANCE_NAME   (optional) — used as a webhook-name prefix
#   INSTANCE_NAME (also)        — used as the displayed sender in the Discord message

set -euo pipefail

CHANNEL="${1:?usage: post-to-channel.sh <channel-key> <message>}"
MESSAGE="${2:?message text required}"

CHASSIS_HOME="${CHASSIS_HOME:?CHASSIS_HOME must be set}"

# Source .env to pick up webhook URLs
if [[ -f "$CHASSIS_HOME/.env" ]]; then
  # shellcheck disable=SC1091
  source "$CHASSIS_HOME/.env"
fi

# Resolve which env var to read for the webhook URL
CHANNEL_UPPER=$(echo "$CHANNEL" | tr '[:lower:]' '[:upper:]')
INSTANCE_PREFIX=""
if [[ -n "${INSTANCE_NAME:-}" ]]; then
  INSTANCE_PREFIX="$(echo "$INSTANCE_NAME" | tr '[:lower:]' '[:upper:]')_"
fi

WEBHOOK_VAR="${INSTANCE_PREFIX}${CHANNEL_UPPER}_WEBHOOK_URL"
WEBHOOK_URL="${!WEBHOOK_VAR:-}"

# Fallback: try without the instance prefix
if [[ -z "$WEBHOOK_URL" && -n "$INSTANCE_PREFIX" ]]; then
  FALLBACK_VAR="${CHANNEL_UPPER}_WEBHOOK_URL"
  WEBHOOK_URL="${!FALLBACK_VAR:-}"
fi

if [[ -z "$WEBHOOK_URL" ]]; then
  echo "ERROR: webhook URL not set for channel '$CHANNEL' (looked at $WEBHOOK_VAR, ${CHANNEL_UPPER}_WEBHOOK_URL)" >&2
  exit 1
fi

# Build payload — username defaults to INSTANCE_NAME or "Behalf.bot"
SENDER="${INSTANCE_NAME:-Behalf.bot}"
PAYLOAD=$(jq -n --arg user "$SENDER" --arg msg "$MESSAGE" '{username: $user, content: $msg}')

curl -sf -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" >/dev/null

echo "✓ posted to $CHANNEL"
