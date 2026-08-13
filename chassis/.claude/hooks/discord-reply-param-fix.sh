#!/bin/bash
# .claude/hooks/discord-reply-param-fix.sh — rename a `message` param to `text`
# on the Discord reply tool, in flight.
#
# The reply tool's body parameter is `text`. A call that passes `message`
# instead reaches the plugin with `text` undefined, and the plugin's chunk()
# splitter then raises:
#
#     reply failed: undefined is not an object (evaluating 'text.length')
#
# That error names a plugin internal rather than the parameter, so it reads as
# a plugin fault. The natural response is to go read plugin source instead of
# re-reading the tool schema, and the model keeps the wrong parameter name for
# the rest of the session.
#
# Why this is worth a hook rather than a line in CLAUDE.md: on 2026-08-13 the
# reference install sent its installer nothing for four hours while the
# terminal transcript looked healthy. Every reply failed on this, and the
# terminal auto-relay fallback truncates at Discord's 2000 chars, so the
# fallback path both lost content and went unnoticed. A CLAUDE.md line would be
# read on every turn to guard against a call the model has already decided how
# to make. See docs/security.md: enforcement lives at the hook layer.
#
# Scope: this is a correctness rewrite, not a safety block. It denies nothing.
#   - `text` already present       → emit nothing, exit 0 (the common path)
#   - `message` present, no `text` → emit updatedInput with the key renamed
#   - neither present              → emit nothing, let the tool raise its own error
#
# PreToolUse hooks may return hookSpecificOutput.updatedInput to rewrite the
# tool input before it runs. Emitting nothing leaves the call untouched.
#
# Register in the same PreToolUse block as guardrails.sh, scoped by matcher:
#   "matcher": "mcp__plugin_discord_discord__reply"

set -euo pipefail

INPUT=$(cat)

# Every jq here fails open. A non-zero exit from a PreToolUse hook can block the
# call, and a hook that mutes the Discord channel on malformed stdin defeats its
# own purpose - this one exists because that channel went silent for four hours.
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null) || exit 0
if [ "$TOOL" != "mcp__plugin_discord_discord__reply" ]; then
  exit 0
fi

TEXT=$(echo "$INPUT" | jq -r '.tool_input.text // empty' 2>/dev/null) || exit 0
MESSAGE=$(echo "$INPUT" | jq -r '.tool_input.message // empty' 2>/dev/null) || exit 0

# Correct call, or nothing we can repair. Stay silent either way.
if [ -n "$TEXT" ] || [ -z "$MESSAGE" ]; then
  exit 0
fi

echo "$INPUT" | jq -c '
  .tool_input
  | (.message) as $body
  | del(.message)
  | .text = $body
  | {
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        updatedInput: .
      },
      systemMessage: "discord reply: renamed `message` to `text` (the tool parameter is `text`)"
    }
' 2>/dev/null || exit 0
