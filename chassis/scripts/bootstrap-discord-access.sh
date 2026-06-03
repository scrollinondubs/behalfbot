#!/usr/bin/env bash
# chassis/scripts/bootstrap-discord-access.sh
# ============================================
# Populate the Claude Code Discord plugin's access.json with the install
# channel ID + principal user_id at install time. Without this, the bot
# joins the install Discord server but ignores every message because no
# channel is allowlisted - Toby hit this on his Asimov install and had to
# manually run `/discord:access group add ...` from inside the running
# tmux session before the bot could function. See chassis#5 item 1.
#
# Inputs (read from env, typically sourced from $CUSTOMER_HOME/.env):
#   INSTALLER_DISCORD_USER_ID   - principal Discord user_id (numeric, 17-19 digits)
#                                 The human the agent works for. Allowlisted on
#                                 every channel populated below. Required.
#   DISCORD_PRIMARY_CHANNEL_ID  - daily-driver conversation channel. Marked as
#                                 a "primary" channel (requireMention=false per
#                                 chassis#5 item 2). Optional; skipped if unset.
#   DISCORD_ALERTS_CHANNEL_ID   - infrastructure alerts channel. requireMention=true.
#   DISCORD_OPS_CHANNEL_ID      - ops signal channel. requireMention=true.
#   DISCORD_BRIEFINGS_CHANNEL_ID - daily briefing channel. requireMention=true.
#   DISCORD_LEADS_CHANNEL_ID    - lead signals channel. requireMention=true.
#   DISCORD_SOCIAL_CHANNEL_ID   - social/dating channel. requireMention=true.
#
# Output:
#   $CLAUDE_ACCESS_FILE (default ~/.claude/channels/discord/access.json)
#
# Idempotency:
#   - Re-running is safe. Existing entries are preserved unless they conflict
#     (same channel_id with different allowlist). Conflicts cause a refusal
#     unless --force is passed; in that case the configured value wins.
#   - Channels not in env are left alone (not removed).
#
# Flags:
#   --dry-run    print the resulting JSON without writing
#   --force      overwrite conflicting entries
#   --access-file PATH  override target file (default ~/.claude/channels/discord/access.json)

set -euo pipefail

DRY_RUN=false
FORCE=false
ACCESS_FILE="${CLAUDE_ACCESS_FILE:-$HOME/.claude/channels/discord/access.json}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)       DRY_RUN=true; shift ;;
        --force)         FORCE=true; shift ;;
        --access-file)   ACCESS_FILE="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,40p' "$0" | sed 's/^# *//'
            exit 0 ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2 ;;
    esac
done

if [[ -z "${INSTALLER_DISCORD_USER_ID:-}" ]]; then
    echo "ERROR: INSTALLER_DISCORD_USER_ID not set in environment" >&2
    echo "  Set it in \$CUSTOMER_HOME/.env (sourced before this script runs)." >&2
    echo "  Source: principal's Discord user_id (right-click their name in" >&2
    echo "  Discord with Developer Mode on -> Copy User ID)." >&2
    exit 2
fi

# Validate the user_id shape - Discord IDs are snowflakes, 17-19 numeric digits.
if ! [[ "$INSTALLER_DISCORD_USER_ID" =~ ^[0-9]{17,20}$ ]]; then
    echo "ERROR: INSTALLER_DISCORD_USER_ID does not look like a Discord snowflake id: $INSTALLER_DISCORD_USER_ID" >&2
    echo "  Expected 17-20 numeric digits." >&2
    exit 2
fi

# Collect declared channels into a structured list:
#   <channel_id> <key> <require_mention>
declare -a CHANNEL_ROWS
if [[ -n "${DISCORD_PRIMARY_CHANNEL_ID:-}" ]]; then
    CHANNEL_ROWS+=("$DISCORD_PRIMARY_CHANNEL_ID primary false")
fi
if [[ -n "${DISCORD_ALERTS_CHANNEL_ID:-}" ]]; then
    CHANNEL_ROWS+=("$DISCORD_ALERTS_CHANNEL_ID alerts true")
fi
if [[ -n "${DISCORD_OPS_CHANNEL_ID:-}" ]]; then
    CHANNEL_ROWS+=("$DISCORD_OPS_CHANNEL_ID ops true")
fi
if [[ -n "${DISCORD_BRIEFINGS_CHANNEL_ID:-}" ]]; then
    CHANNEL_ROWS+=("$DISCORD_BRIEFINGS_CHANNEL_ID briefings true")
fi
if [[ -n "${DISCORD_LEADS_CHANNEL_ID:-}" ]]; then
    CHANNEL_ROWS+=("$DISCORD_LEADS_CHANNEL_ID leads true")
fi
if [[ -n "${DISCORD_SOCIAL_CHANNEL_ID:-}" ]]; then
    CHANNEL_ROWS+=("$DISCORD_SOCIAL_CHANNEL_ID social true")
fi

if [[ ${#CHANNEL_ROWS[@]} -eq 0 ]]; then
    echo "WARN: no DISCORD_*_CHANNEL_ID env vars set - nothing to populate" >&2
    echo "  Set DISCORD_PRIMARY_CHANNEL_ID at minimum, plus any other channel" >&2
    echo "  keys you want allowlisted. See chassis.config.yaml.discord_channels." >&2
    exit 0
fi

# Ensure parent dir exists for the access file.
ACCESS_DIR="$(dirname "$ACCESS_FILE")"
if [[ "$DRY_RUN" != "true" ]]; then
    mkdir -p "$ACCESS_DIR"
fi

# Seed the file with a minimal allowlist policy if it doesn't already exist.
# Schema mirrors the discord@claude-plugins-official plugin's expected layout
# (channels keyed by id, allow array of user_ids, requireMention bool, policy
# field at the root level).
if [[ ! -f "$ACCESS_FILE" ]]; then
    INITIAL_JSON='{"policy": "allowlist", "channels": {}}'
else
    INITIAL_JSON="$(cat "$ACCESS_FILE")"
fi

# Validate that the existing file is JSON before we try to merge into it.
# Catches the "previous run wrote garbage" path early.
if ! echo "$INITIAL_JSON" | jq empty >/dev/null 2>&1; then
    echo "ERROR: $ACCESS_FILE exists but is not valid JSON. Refusing to overwrite." >&2
    echo "  Inspect, fix, or rm $ACCESS_FILE and re-run." >&2
    exit 3
fi

# Build the new state. For each declared channel:
#   - If absent, add it with allow=[$INSTALLER_DISCORD_USER_ID], requireMention per key
#   - If present and identical, leave alone
#   - If present and different and --force, overwrite
#   - If present and different and no --force, refuse with a clear message
CURRENT_JSON="$INITIAL_JSON"
for row in "${CHANNEL_ROWS[@]}"; do
    # shellcheck disable=SC2206
    parts=($row)
    chan_id="${parts[0]}"
    chan_key="${parts[1]}"
    require_mention="${parts[2]}"

    existing=$(echo "$CURRENT_JSON" | jq --arg id "$chan_id" '.channels[$id] // null')

    if [[ "$existing" != "null" ]]; then
        existing_require=$(echo "$existing" | jq -r '.requireMention // false')
        existing_has_user=$(echo "$existing" | jq --arg uid "$INSTALLER_DISCORD_USER_ID" \
            '(.allow // []) | index($uid) != null')
        if [[ "$existing_require" == "$require_mention" && "$existing_has_user" == "true" ]]; then
            echo "  unchanged: $chan_key ($chan_id) already allowlisted with requireMention=$require_mention"
            continue
        fi
        if [[ "$FORCE" != "true" ]]; then
            echo "REFUSE: $chan_key ($chan_id) already in $ACCESS_FILE with different config." >&2
            echo "  existing: $existing" >&2
            echo "  configured: requireMention=$require_mention, allow=[$INSTALLER_DISCORD_USER_ID]" >&2
            echo "  Pass --force to overwrite, or hand-edit $ACCESS_FILE." >&2
            exit 4
        fi
        echo "  overwriting (--force): $chan_key ($chan_id) requireMention=$require_mention"
    else
        echo "  adding: $chan_key ($chan_id) requireMention=$require_mention"
    fi

    CURRENT_JSON=$(echo "$CURRENT_JSON" | jq \
        --arg id "$chan_id" \
        --arg key "$chan_key" \
        --arg uid "$INSTALLER_DISCORD_USER_ID" \
        --argjson require "$require_mention" \
        '.channels[$id] = {
            key: $key,
            allow: [$uid],
            requireMention: $require
        }')
done

if [[ "$DRY_RUN" == "true" ]]; then
    echo ""
    echo "--- DRY-RUN: would write to $ACCESS_FILE ---"
    echo "$CURRENT_JSON" | jq .
    exit 0
fi

# Atomic write: stage to tmp then mv.
TMP="${ACCESS_FILE}.tmp.$$"
echo "$CURRENT_JSON" | jq . > "$TMP"
mv "$TMP" "$ACCESS_FILE"
chmod 600 "$ACCESS_FILE"

echo "✓ wrote $ACCESS_FILE (${#CHANNEL_ROWS[@]} channel entries, principal user_id=$INSTALLER_DISCORD_USER_ID)"
