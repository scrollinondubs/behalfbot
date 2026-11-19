#!/usr/bin/env bash
# alert-post.sh - the one entrypoint for a chassis prompt or script that has to
# tell the operator something. It honours CHASSIS_ALERT_TRANSPORT, so a prompt
# never needs to know which chat app the install uses.
#
# Usage:
#   alert-post.sh [--discord-channel <id>] <alerts|ops|dm|name> <text|@file> [attachment ...]
#
# Transport (CHASSIS_ALERT_TRANSPORT, from the environment or the install's
# .env; default discord):
#   discord  discord-post.sh only. Exactly what the prompts did before this
#            script existed.
#   matrix   matrix-post.py. When Matrix fails and a Discord channel resolves,
#            falls back to Discord and says so on stderr.
#   both     Both legs, always both attempted. Exits 0 when EITHER delivered:
#            the edge-triggered callers re-fire on failure, so requiring both
#            would repost the leg that worked on every tick while the other
#            one is down.
#
# Discord channel for a key, first one set wins:
#   alerts  DISCORD_ALERTS_CHANNEL_ID, DISCORD_OPS_CHANNEL_ID, DISCORD_PRIMARY_CHANNEL_ID
#   ops     DISCORD_OPS_CHANNEL_ID, DISCORD_ALERTS_CHANNEL_ID, DISCORD_PRIMARY_CHANNEL_ID
#   dm      DISCORD_PRIMARY_CHANNEL_ID
#   <name>  DISCORD_<NAME>_CHANNEL_ID, then the ops chain
# `--discord-channel` overrides that for callers with their own channel id
# (TRIPWIRE_CHANNEL_ID, SPEND_ALERT_CHANNEL_ID). Matrix rooms resolve in
# matrix-post.py from CHASSIS_MATRIX_<KEY>_ROOM.
#
# Prints one line, `alert-post: matrix=ok|fail|skip discord=ok|fail|skip`.
# Exit 0 delivered, 1 bad arguments, 3 nothing delivered.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "usage: alert-post.sh [--discord-channel <id>] <alerts|ops|dm|name> <text|@file> [attachment ...]" >&2
    exit 1
}

DISCORD_OVERRIDE=""
if [[ "${1:-}" == "--discord-channel" ]]; then
    [[ $# -ge 2 ]] || usage
    DISCORD_OVERRIDE="$2"
    shift 2
fi
[[ $# -ge 2 ]] || usage
KEY="$1"
BODY="$2"
shift 2

# The install's .env carries the transport and the ids when this runs from a
# launchd job or a prompt that did not export them. Values the caller set
# explicitly win over the file.
# .env.baked first: the raw .env can hold a Vaultwarden hydration block that
# only works on the host (same preference as docker/entrypoint.sh).
HOME_DIR="${CHASSIS_HOME:-${CUSTOMER_HOME:-${HOME}/.behalfbot}}"
ENV_FILE="${HOME_DIR}/.env"
[[ -f "${HOME_DIR}/.env.baked" ]] && ENV_FILE="${HOME_DIR}/.env.baked"
if [[ -f "$ENV_FILE" ]]; then
    KEEP=""
    for v in CHASSIS_ALERT_TRANSPORT CHASSIS_MATRIX_HOMESERVER CHASSIS_MATRIX_TOKEN_FILE \
        CHASSIS_MATRIX_ALERTS_ROOM CHASSIS_MATRIX_OPS_ROOM CHASSIS_MATRIX_DM_ROOM DISCORD_BOT_TOKEN \
        DISCORD_ALERTS_CHANNEL_ID DISCORD_OPS_CHANNEL_ID DISCORD_PRIMARY_CHANNEL_ID; do
        if [[ -n "${!v:-}" ]]; then
            KEEP="${KEEP}${v}=$(printf '%q' "${!v}")"$'\n'
        fi
    done
    set +eu
    set -a
    # shellcheck source=/dev/null
    . "$ENV_FILE" >/dev/null 2>&1
    set +a
    [[ -n "$KEEP" ]] && eval "$KEEP"
    set -eu
fi
export CHASSIS_HOME="${CHASSIS_HOME:-$HOME_DIR}"

TRANSPORT="$(printf '%s' "${CHASSIS_ALERT_TRANSPORT:-discord}" | tr '[:upper:]' '[:lower:]')"
case "$TRANSPORT" in
    discord|matrix|both) ;;
    *)
        echo "alert-post: unknown CHASSIS_ALERT_TRANSPORT '$TRANSPORT', using discord" >&2
        TRANSPORT=discord
        ;;
esac

discord_channel() {
    if [[ -n "$DISCORD_OVERRIDE" ]]; then
        printf '%s' "$DISCORD_OVERRIDE"
        return
    fi
    local upper chain v
    upper="$(printf '%s' "$KEY" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9\n' '_')"
    case "$upper" in
        ALERTS) chain="DISCORD_ALERTS_CHANNEL_ID DISCORD_OPS_CHANNEL_ID DISCORD_PRIMARY_CHANNEL_ID" ;;
        OPS) chain="DISCORD_OPS_CHANNEL_ID DISCORD_ALERTS_CHANNEL_ID DISCORD_PRIMARY_CHANNEL_ID" ;;
        DM) chain="DISCORD_PRIMARY_CHANNEL_ID" ;;
        *) chain="DISCORD_${upper}_CHANNEL_ID DISCORD_OPS_CHANNEL_ID DISCORD_ALERTS_CHANNEL_ID DISCORD_PRIMARY_CHANNEL_ID" ;;
    esac
    for v in $chain; do
        if [[ -n "${!v:-}" ]]; then
            printf '%s' "${!v}"
            return
        fi
    done
}

post_discord() {
    local chan
    chan="$(discord_channel)"
    if [[ -z "$chan" ]]; then
        echo "alert-post: no Discord channel for '$KEY'" >&2
        return 1
    fi
    [[ -n "${DISCORD_BOT_TOKEN:-}" ]] || { echo "alert-post: DISCORD_BOT_TOKEN unset" >&2; return 1; }
    export DISCORD_BOT_TOKEN
    "${DIR}/discord-post.sh" "$chan" "$BODY" "$@" >&2
}

post_matrix() {
    python3 "${DIR}/matrix-post.py" "$KEY" "$BODY" "$@" >&2
}

M=skip
D=skip
case "$TRANSPORT" in
    discord)
        if post_discord "$@"; then D=ok; else D=fail; fi
        ;;
    matrix)
        if post_matrix "$@"; then
            M=ok
        else
            M=fail
            echo "alert-post: Matrix failed, falling back to Discord" >&2
            if post_discord "$@"; then D=ok; else D=fail; fi
        fi
        ;;
    both)
        if post_matrix "$@"; then M=ok; else M=fail; fi
        if post_discord "$@"; then D=ok; else D=fail; fi
        ;;
esac

echo "alert-post: matrix=$M discord=$D"
[[ "$M" == ok || "$D" == ok ]] || exit 3
