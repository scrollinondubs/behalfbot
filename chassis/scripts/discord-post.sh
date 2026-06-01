#!/bin/bash
# discord-post.sh — POST a message + optional file attachments to a Discord
# channel via the REST API, using the bot token in $DISCORD_BOT_TOKEN.
#
# Designed to replace `mcp__plugin_discord_discord__reply` for delivery
# paths that run INSIDE the chassis container (heartbeat-fired claude -p
# invocations, scheduled tasks, anything launched from the dispatcher).
# Those contexts can't use `mcp__plugin_discord_discord__*` because the
# discord MCP is a Claude Code plugin installed on the HOST user's
# `~/.claude/plugins/`, not mirrored into the container's `.mcp.json`.
#
# Silent-failure history that motivated this: scrollinondubs/new-jaxity
# 2026-05-22..27 morning briefings — generated daily inside the container,
# claude -p couldn't find the discord MCP tool, fell back to the documented
# Gmail-draft path (which the recipient didn't check), six days of missing
# briefings before anyone noticed.
#
# Usage:
#   discord-post.sh <channel_id> <text-or-@file> [attach1] [attach2] ...
#
# Examples:
#   # Plain text message
#   discord-post.sh 1487067385159487588 "Briefing for $(date +%F) ready: $URL"
#
#   # Read text body from a file (handy for prose with newlines/markdown/$)
#   discord-post.sh 1487067385159487588 @/tmp/synopsis.txt
#
#   # With one or more file attachments
#   discord-post.sh 1487067385159487588 "Morning briefing $(date +%F)" \
#       /app/customer/briefings/$(date +%F)-morning-briefing.md
#
# Exit codes:
#   0  message posted successfully (HTTP 2xx, response has 'id' field)
#   1  invalid arguments
#   2  DISCORD_BOT_TOKEN env var missing
#   3  Discord API returned non-2xx OR no 'id' in response
#
# Quoting gotchas (learned in scrollinondubs/new-jaxity 2026-05-27, fixed
# in PR #101 + this helper):
#   - DON'T inline JSON via -F "payload_json={\"content\": ...}": shell
#     escapes mangle the JSON; Discord rejects with PAYLOAD_JSON_INVALID.
#     This helper writes payload to a temp file and uses `-F payload_json=
#     <file>` instead.
#   - DON'T append `;type=text/markdown` (or any type hint) to the file
#     part: curl 7.88 in the chassis container drops the file with
#     "(26) Failed to open/read local data" when type hints attach.
#   - DON'T use python urllib.request: default UA triggers Cloudflare
#     code 1010 (HTTP 403). Curl works fine.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: discord-post.sh <channel_id> <text-or-@file> [file_attachments...]

  <channel_id>     Discord channel ID (numeric snowflake)
  <text-or-@file>  Message body. Use `@/path/to/file` to read from a file
                   (handy for synopses with newlines + markdown + $ chars).
                   Plain string sent as-is otherwise. ~2000 char Discord
                   limit applies either way.
  [file_attach...] Optional file paths to attach (max 10 files, 25MB each).

Requires DISCORD_BOT_TOKEN in env. Bot must have View Channel + Send
Messages + Attach Files permissions in the target channel.
EOF
    exit 1
}

if [[ $# -lt 2 ]]; then
    usage
fi

if [[ -z "${DISCORD_BOT_TOKEN:-}" ]]; then
    echo "discord-post.sh: DISCORD_BOT_TOKEN env var is empty/unset" >&2
    exit 2
fi

CHANNEL_ID="$1"
BODY_ARG="$2"
shift 2
ATTACHMENTS=("$@")

# Resolve the message body — either the literal arg or contents of a file
# referenced via @prefix (mirrors curl's own @file convention so the
# interface stays familiar to anyone who's used curl multipart forms).
if [[ "$BODY_ARG" == @* ]]; then
    BODY_FILE="${BODY_ARG#@}"
    if [[ ! -f "$BODY_FILE" ]]; then
        echo "discord-post.sh: body file not found: $BODY_FILE" >&2
        exit 1
    fi
    MESSAGE_TEXT=$(<"$BODY_FILE")
else
    MESSAGE_TEXT="$BODY_ARG"
fi

# Build the payload JSON in a temp file so we don't fight shell quoting.
# `allowed_mentions: {parse: []}` suppresses @-mention pings, which is the
# right default for automated heartbeat posts — operators can opt back in
# by editing this helper or layering their own POST.
PAYLOAD_FILE=$(mktemp /tmp/discord-payload.XXXXXX.json)
trap 'rm -f "$PAYLOAD_FILE"' EXIT

python3 - "$MESSAGE_TEXT" > "$PAYLOAD_FILE" <<'PY'
import json, sys
print(json.dumps({"content": sys.argv[1], "allowed_mentions": {"parse": []}}))
PY

# Assemble curl args. -F "payload_json=<FILE" loads the field from disk
# (NOT @FILE — that would upload it as an attachment instead).
CURL_ARGS=(
    -sS -X POST
    -H "Authorization: Bot $DISCORD_BOT_TOKEN"
    -F "payload_json=<$PAYLOAD_FILE"
)

# Add file attachments — `files[N]=@/path/to/file` uploads each one. Do
# NOT append `;type=...` — curl 7.88 (the version shipped in the chassis
# image as of 2026-05-27) drops the field with exit 26 when type hints
# attach. Discord infers content type from the filename extension anyway.
for i in "${!ATTACHMENTS[@]}"; do
    if [[ ! -f "${ATTACHMENTS[$i]}" ]]; then
        echo "discord-post.sh: attachment file not found: ${ATTACHMENTS[$i]}" >&2
        exit 1
    fi
    CURL_ARGS+=(-F "files[$i]=@${ATTACHMENTS[$i]}")
done

CURL_ARGS+=("https://discord.com/api/v10/channels/$CHANNEL_ID/messages")

RESPONSE=$(curl "${CURL_ARGS[@]}")

# Verify success by checking the response has an `id` field. Discord
# returns the full Message object on 2xx; on 4xx/5xx it returns an error
# object with `code`/`message` and no `id`.
if echo "$RESPONSE" | python3 -c "import json, sys; d = json.load(sys.stdin); sys.exit(0 if d.get('id') else 1)" 2>/dev/null; then
    MSG_ID=$(echo "$RESPONSE" | python3 -c "import json, sys; print(json.load(sys.stdin)['id'])")
    echo "discord-post.sh: posted to channel $CHANNEL_ID, message id=$MSG_ID"
    exit 0
else
    echo "discord-post.sh: POST failed. Response:" >&2
    echo "$RESPONSE" | head -c 1000 >&2
    echo "" >&2
    exit 3
fi
