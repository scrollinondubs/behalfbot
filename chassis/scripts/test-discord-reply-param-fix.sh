#!/bin/bash
# test-discord-reply-param-fix.sh - Unit tests for the PreToolUse hook that
# renames a `message` param to `text` on the Discord reply tool.
#
# The bug this locks down
# =======================
# The reply tool's body parameter is `text`. A call passing `message` reaches
# the plugin with `text` undefined, and the plugin's chunk() splitter raises
# "reply failed: undefined is not an object (evaluating 'text.length')". The
# error names a plugin internal rather than the parameter, so the failure reads
# as a plugin fault and the wrong parameter name survives the whole session.
#
# On 2026-08-13 the reference install sent its installer nothing for four hours
# on exactly this, while the terminal transcript looked healthy.
#
# Two properties matter and they pull against each other:
#
#   1. A `message`-only call MUST be rewritten, with every other key preserved
#      (chat_id, reply_to, files) - a rewrite that drops the attachment list
#      would turn a loud failure into a quiet one.
#   2. A correct call MUST produce NO output. This hook sits on a PreToolUse
#      path that fires on every reply, so a hook that emits an updatedInput
#      when none is needed is a hook that rewrites payloads it does not
#      understand. Silence on the common path is the safety property.
#
# No docker, no network, no MCP server. The hook is pure stdin/stdout.
#
# Exit codes:
#   0 - all scenarios passed
#   1 - one or more scenarios failed
#   2 - test harness itself broke

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="${SCRIPT_DIR}/../.claude/hooks/discord-reply-param-fix.sh"

if [[ ! -f "$HOOK" ]]; then
    echo "test-discord-reply-param-fix: missing $HOOK" >&2
    exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "test-discord-reply-param-fix: jq not on PATH" >&2
    exit 2
fi

fail=0
pass=0

check() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass=$((pass + 1))
    else
        echo "FAIL [$name] expected '$expected', got '$actual'"
        fail=$((fail + 1))
    fi
}

run_hook() { bash "$HOOK"; }

REPLY_TOOL='mcp__plugin_discord_discord__reply'

# ---------------------------------------------------------------------------
# 1. The rewrite fires, and carries every other key across.
# ---------------------------------------------------------------------------
OUT=$(echo "{\"tool_name\":\"${REPLY_TOOL}\",\"tool_input\":{\"chat_id\":\"148\",\"message\":\"hello\",\"reply_to\":\"9\"}}" | run_hook)

check "rewrite: text takes the message body" \
    "hello" "$(echo "$OUT" | jq -r '.hookSpecificOutput.updatedInput.text // empty')"
check "rewrite: message key is gone" \
    "true" "$(echo "$OUT" | jq -r '.hookSpecificOutput.updatedInput | has("message") | not')"
check "rewrite: chat_id survives" \
    "148" "$(echo "$OUT" | jq -r '.hookSpecificOutput.updatedInput.chat_id // empty')"
check "rewrite: reply_to survives" \
    "9" "$(echo "$OUT" | jq -r '.hookSpecificOutput.updatedInput.reply_to // empty')"
check "rewrite: declares the PreToolUse event" \
    "PreToolUse" "$(echo "$OUT" | jq -r '.hookSpecificOutput.hookEventName // empty')"
check "rewrite: never denies - a correctness fix must not block the call" \
    "true" "$(echo "$OUT" | jq -r 'has("permissionDecision") or has("hookSpecificOutput.permissionDecision") | not')"

# Attachments are the payload most expensive to lose silently.
OUT=$(echo "{\"tool_name\":\"${REPLY_TOOL}\",\"tool_input\":{\"chat_id\":\"1\",\"message\":\"m\",\"files\":[\"/a.png\",\"/b.png\"]}}" | run_hook)
check "rewrite: files array survives intact" \
    "/a.png /b.png" "$(echo "$OUT" | jq -r '.hookSpecificOutput.updatedInput.files | join(" ")')"

# A body with newlines and quotes must survive the jq round trip byte for byte.
BODY='line one
"quoted" and `backticked` and \ backslash'
OUT=$(jq -n --arg t "$REPLY_TOOL" --arg b "$BODY" \
    '{tool_name:$t, tool_input:{chat_id:"1", message:$b}}' | run_hook)
check "rewrite: multiline body with quotes survives unchanged" \
    "$BODY" "$(echo "$OUT" | jq -r '.hookSpecificOutput.updatedInput.text')"

# ---------------------------------------------------------------------------
# 2. Silence on every path that is not the bug. A hook that speaks here is a
#    hook rewriting calls it was not asked to touch.
# ---------------------------------------------------------------------------
silent_case() {
    local name="$1" payload="$2"
    local out rc
    out=$(echo "$payload" | run_hook)
    rc=$?
    check "silent: $name (no output)" "" "$out"
    check "silent: $name (exit 0)" "0" "$rc"
}

silent_case "correct call using text" \
    "{\"tool_name\":\"${REPLY_TOOL}\",\"tool_input\":{\"chat_id\":\"1\",\"text\":\"hi\"}}"
silent_case "both keys present - text wins, nothing to repair" \
    "{\"tool_name\":\"${REPLY_TOOL}\",\"tool_input\":{\"chat_id\":\"1\",\"text\":\"real\",\"message\":\"stale\"}}"
silent_case "neither key - let the tool raise its own error" \
    "{\"tool_name\":\"${REPLY_TOOL}\",\"tool_input\":{\"chat_id\":\"1\"}}"
silent_case "a different tool that happens to take a message param" \
    '{"tool_name":"Bash","tool_input":{"command":"ls","message":"x"}}'
silent_case "empty message string is not a body worth rewriting" \
    "{\"tool_name\":\"${REPLY_TOOL}\",\"tool_input\":{\"chat_id\":\"1\",\"message\":\"\"}}"
silent_case "no tool_name at all" \
    '{"tool_input":{"message":"x"}}'

# ---------------------------------------------------------------------------
# 3. Malformed stdin must not take the tool call down with it. This hook runs
#    ahead of every Discord reply; failing closed here would mute the channel
#    it exists to protect.
# ---------------------------------------------------------------------------
OUT=$(echo 'not json at all' | run_hook 2>/dev/null)
RC=$?
check "malformed stdin: exits 0 rather than blocking the reply" "0" "$RC"
check "malformed stdin: emits nothing" "" "$OUT"

OUT=$(printf '' | run_hook 2>/dev/null)
RC=$?
check "empty stdin: exits 0" "0" "$RC"
check "empty stdin: emits nothing" "" "$OUT"

# ---------------------------------------------------------------------------

echo
echo "test-discord-reply-param-fix: ${pass} passed, ${fail} failed"
[[ $fail -eq 0 ]] || exit 1
exit 0
