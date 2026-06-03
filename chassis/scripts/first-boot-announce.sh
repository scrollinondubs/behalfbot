#!/usr/bin/env bash
# chassis/scripts/first-boot-announce.sh
# =======================================
# Emits the bot's Discord user ID + OAuth invite URL on the FIRST dispatcher
# boot. Subsequent runs skip silently because the sentinel exists.
#
# Call site: docker/entrypoint.sh cmd_dispatcher, BEFORE the dispatcher loop
# starts, so the installer sees the emit in the ops channel immediately on
# first container start.
#
# Why this exists: installer-2 install + installer-1 install both stalled on "what is
# my bot's user ID?" during Phase 2 of the Discord channel pattern (PR #51).
# The ID is only visible in Discord Developer Portal and was unknown until Sean
# fished it out of screenshots. This script makes it self-reported.
# See issue #53 item 4.
#
# Flow:
#   1. Check sentinel. If it exists, exit 0 (already announced).
#   2. Call Discord REST GET /users/@me using DISCORD_BOT_TOKEN.
#   3. If token absent or API call fails, log to stdout with BEHALFBOT_FIRST_BOOT:
#      prefix (installer can grep for it) and write sentinel anyway so we do not
#      re-attempt on every boot.
#   4. Build the three-line message and post via post-to-channel.sh ops.
#      If the ops webhook is not configured yet, fall back to stdout.
#   5. Write the sentinel.
#
# Chicken-and-egg note: at truly-first boot the installer's .env may not yet
# have the ops webhook URL - that's fine. The stdout fallback covers that case.
# The installer runs `docker logs <container>` or `grep BEHALFBOT_FIRST_BOOT`
# in the container to find the emit.

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set}"

SENTINEL="$CHASSIS_HOME/state/first-boot-announced.json"
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
    printf '[first-boot %(%H:%M:%S)T] %s\n' -1 "$*"
}

# Already announced - skip entirely.
if [[ -f "$SENTINEL" ]]; then
    log "sentinel exists, skipping first-boot announce"
    exit 0
fi

mkdir -p "$CHASSIS_HOME/state"

# Resolve bot identity via Discord REST API.
BOT_USER_ID=""
BOT_USERNAME=""

if [[ -n "${DISCORD_BOT_TOKEN:-}" ]]; then
    api_response=$(curl -sf \
        -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
        -H "User-Agent: behalfbot (first-boot-announce.sh)" \
        "https://discord.com/api/v10/users/@me" 2>/dev/null) || api_response=""

    if [[ -n "$api_response" ]]; then
        BOT_USER_ID=$(printf '%s' "$api_response" | jq -r '.id // ""' 2>/dev/null || true)
        BOT_USERNAME=$(printf '%s' "$api_response" | jq -r '.username // ""' 2>/dev/null || true)
    fi
fi

EMITTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# chassis#5 item 3: sanity-check INSTANCE_NAME against the live Discord bot
# username. If they disagree, the first briefing will post under the wrong
# persona (e.g. "Captain Hook" instead of "Asimov"). Log loud so the
# installer sees the mismatch in the first-boot log before the dispatcher
# starts firing scheduled posts.
IDENTITY_WARNING=""
if [[ -n "$BOT_USERNAME" && -n "${INSTANCE_NAME:-}" && "$INSTANCE_NAME" != "$BOT_USERNAME" ]]; then
    IDENTITY_WARNING="WARNING: INSTANCE_NAME=${INSTANCE_NAME} does not match Discord bot username=${BOT_USERNAME}. Outbound webhooks will display '${INSTANCE_NAME}' as the sender but the bot account itself is registered as '${BOT_USERNAME}'. Fix INSTANCE_NAME in .env to match before the first heartbeat fires, or rename the Discord bot to match INSTANCE_NAME."
    log "BEHALFBOT_FIRST_BOOT: $IDENTITY_WARNING"
fi

# Build the three-line message (or a degraded version when ID unavailable).
if [[ -n "$BOT_USER_ID" ]]; then
    OAUTH_URL="https://discord.com/oauth2/authorize?client_id=${BOT_USER_ID}&scope=bot+applications.commands&permissions=379968"
    LINE1="Behalfbot connected as ${BOT_USERNAME} (Discord user ID: ${BOT_USER_ID}). Share this with Sean to add me to the install channel."
    LINE2="OAuth invite URL: ${OAUTH_URL}"
    LINE3="Once added: run \`/discord:access group add <channel_id> --no-mention --allow <SEAN_ID>,<JAXBOT_ID>\` on this host to allowlist the channel."
    if [[ -n "$IDENTITY_WARNING" ]]; then
        LINE3="${LINE3}\n\n${IDENTITY_WARNING}"
    fi
else
    LINE1="BEHALFBOT_FIRST_BOOT: DISCORD_BOT_TOKEN not set or Discord API call failed. Set DISCORD_BOT_TOKEN in .env and recheck."
    LINE2="Once the token is set, delete ${SENTINEL} and restart the container to re-emit."
    LINE3="Manual lookup: Discord Developer Portal -> Your Application -> General Information -> Application ID (= bot user ID)."
fi

FULL_MESSAGE="${LINE1}
${LINE2}
${LINE3}"

# Post to ops channel. Fall back to stdout if webhook not configured.
POSTED=false
if [[ -x "$SCRIPTS_DIR/post-to-channel.sh" ]]; then
    if CHASSIS_HOME="$CHASSIS_HOME" bash "$SCRIPTS_DIR/post-to-channel.sh" ops "$FULL_MESSAGE" 2>/dev/null; then
        POSTED=true
    fi
fi

if [[ "$POSTED" == "false" ]]; then
    log "BEHALFBOT_FIRST_BOOT: ops webhook not configured or post failed - emitting to stdout instead"
    log "BEHALFBOT_FIRST_BOOT: ${LINE1}"
    log "BEHALFBOT_FIRST_BOOT: ${LINE2}"
    log "BEHALFBOT_FIRST_BOOT: ${LINE3}"
fi

# Write sentinel regardless of whether posting succeeded. This prevents
# re-posting on every subsequent boot when the webhook is eventually configured
# but the sentinel was never written because of an earlier failure.
jq -n \
    --arg emitted_at "$EMITTED_AT" \
    --arg bot_user_id "$BOT_USER_ID" \
    --arg bot_username "$BOT_USERNAME" \
    --arg instance_name "${INSTANCE_NAME:-}" \
    --arg identity_warning "$IDENTITY_WARNING" \
    '{
        emitted_at: $emitted_at,
        bot_user_id: $bot_user_id,
        bot_username: $bot_username,
        instance_name: $instance_name,
        identity_warning: $identity_warning
    }' \
    > "$SENTINEL"

log "first-boot announce complete - sentinel written at $SENTINEL"
