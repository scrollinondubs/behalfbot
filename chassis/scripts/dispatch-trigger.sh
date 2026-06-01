#!/usr/bin/env bash
# dispatch-trigger.sh — deterministic trigger-keyword dispatcher for inbound Discord messages.
#
# Reads $CHASSIS_HOME/chassis/triggers.yaml (registry; plugin-declared triggers
# merged in by bootstrap.sh). Pattern-matches the inbound message body against
# each trigger's keyword_regex; on first match, runs the trigger's parser to
# extract structured args, optionally reacts to the source message with the
# trigger's react_emoji, then invokes the trigger's handler with the parsed
# args.
#
# Two reasons this exists alongside the LLM-driven trigger-dispatch pattern
# documented in chassis/CLAUDE.md.template (Discord Triggers section):
#
#   1. Determinism — known-shape messages (Backfill:, Pacman <url>) don't burn
#      LLM tokens. Pattern-matching shell triggers run in milliseconds against
#      a deterministic regex registry.
#   2. Plugin distribution — plugins ship triggers in their openclaw.plugin.json
#      under contracts.triggers; bootstrap merges them into triggers.yaml at
#      install time. The chassis is aware of every plugin trigger without the
#      installer needing to hand-edit CLAUDE.md.
#
# Usage:
#   dispatch-trigger.sh <channel-id> <message-id> <message-body>
#
# Output (stdout, single JSON object):
#   {"matched": false, "reason": "no_trigger_matched"}
#   {"matched": true, "trigger": "<name>", "react_emoji": "<emoji>",
#    "react_status": "reacted|emit_only|react_failed|skipped",
#    "handler": "<path>", "args": {...}, "exit_code": <int>,
#    "handler_stdout": "<text>", "handler_stderr": "<text>"}
#
# Exit codes:
#   0 — matched, handler ran (handler success/failure both 0; check exit_code in JSON)
#   1 — no trigger matched
#   2 — registry parse error / config error
#
# Required env:
#   CHASSIS_HOME — absolute path to the chassis directory
#
# Optional env:
#   DISCORD_BOT_TOKEN — if set, dispatcher reacts to source message with the
#                       trigger's react_emoji via discord-react.py. Without it,
#                       react_status is "emit_only" and the calling heartbeat
#                       is responsible for reacting (e.g. via the discord MCP
#                       from a claude -p prompt).
#   TRIGGERS_YAML     — override registry path (default: $CHASSIS_HOME/chassis/triggers.yaml)
#
# Lessons baked in (LESSONS_FROM_V1.md):
#   #16 — UserPromptSubmit hooks don't fire on Discord-channel inbound; assume
#         this dispatcher is invoked from a Discord-poll heartbeat.
#   #20 — keep gather-side checks cheap. This script no-ops in <50ms when the
#         message doesn't match any registered trigger.

set -euo pipefail

if [[ $# -lt 3 ]]; then
    echo "usage: dispatch-trigger.sh <channel-id> <message-id> <message-body>" >&2
    exit 2
fi

CHANNEL_ID="$1"
MESSAGE_ID="$2"
MESSAGE_BODY="$3"

: "${CHASSIS_HOME:?CHASSIS_HOME must be set}"
TRIGGERS_YAML="${TRIGGERS_YAML:-$CHASSIS_HOME/chassis/triggers.yaml}"

if [[ ! -f "$TRIGGERS_YAML" ]]; then
    echo '{"matched": false, "reason": "no_registry"}'
    exit 1
fi

# Source .env so handlers + parsers see plugin-declared environment (BOT_TOKEN,
# channel-id placeholders, etc.).
if [[ -f "$CHASSIS_HOME/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "$CHASSIS_HOME/.env"
    set +a
fi

# Walk triggers.yaml with awk; emit one TSV line per trigger:
#   name<TAB>plugin<TAB>keyword_regex<TAB>channel_filter<TAB>parser<TAB>handler<TAB>react_emoji
#
# Same minimal-YAML approach as chassis/scheduled-tasks/heartbeat-dispatcher.sh
# (no yq dependency — the chassis intentionally stays close to POSIX shell +
# jq). Schema is one entry per `- name:` block under `triggers:`.
trigger_lines=$(awk '
    /^triggers:/ { in_triggers = 1; next }
    in_triggers && /^[^[:space:]-]/ { in_triggers = 0 }
    !in_triggers { next }

    function emit_entry() {
        printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n", \
            name, plugin, kw, ch, parser, handler, emoji
    }

    function strip(s) {
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
        gsub(/^["'\'']|["'\'']$/, "", s)
        return s
    }

    /^[[:space:]]*-[[:space:]]+name:[[:space:]]*/ {
        if (have_entry) emit_entry()
        have_entry = 1
        name = ""; plugin = ""; kw = ""; ch = "*"; parser = "passthrough"; handler = ""; emoji = ""
        sub(/^[[:space:]]*-[[:space:]]+name:[[:space:]]*/, "", $0)
        name = strip($0)
        next
    }

    /^[[:space:]]+plugin:[[:space:]]*/         { sub(/^[[:space:]]+plugin:[[:space:]]*/, "", $0);         plugin  = strip($0); next }
    /^[[:space:]]+keyword_regex:[[:space:]]*/  { sub(/^[[:space:]]+keyword_regex:[[:space:]]*/, "", $0);  kw      = strip($0); next }
    /^[[:space:]]+channel_filter:[[:space:]]*/ { sub(/^[[:space:]]+channel_filter:[[:space:]]*/, "", $0); ch      = strip($0); next }
    /^[[:space:]]+parser:[[:space:]]*/         { sub(/^[[:space:]]+parser:[[:space:]]*/, "", $0);         parser  = strip($0); next }
    /^[[:space:]]+handler:[[:space:]]*/        { sub(/^[[:space:]]+handler:[[:space:]]*/, "", $0);        handler = strip($0); next }
    /^[[:space:]]+react_emoji:[[:space:]]*/    { sub(/^[[:space:]]+react_emoji:[[:space:]]*/, "", $0);    emoji   = strip($0); next }

    END { if (have_entry) emit_entry() }
' "$TRIGGERS_YAML")

if [[ -z "$trigger_lines" ]]; then
    echo '{"matched": false, "reason": "registry_empty"}'
    exit 1
fi

# Expand $CHASSIS_HOME etc. in registry values via Python (envsubst-equivalent
# without depending on gettext being installed).
expand_env() {
    python3 -c '
import os, sys
print(os.path.expandvars(sys.stdin.read()), end="")
' <<< "$1"
}

matched_name=""
matched_plugin=""
matched_kw=""
matched_parser=""
matched_handler=""
matched_emoji=""

while IFS=$'\t' read -r name plugin kw ch parser handler emoji; do
    [[ -z "$name" ]] && continue

    ch_expanded=$(expand_env "$ch")
    if [[ "$ch_expanded" != "*" && "$ch_expanded" != "$CHANNEL_ID" ]]; then
        continue
    fi

    # Case-insensitive regex match against message body. Uses Python re so
    # `\s`, `\d`, `\w` etc. work as authors expect (ERE wouldn't honor them).
    if MESSAGE="$MESSAGE_BODY" PATTERN="$kw" python3 -c '
import os, re, sys
m = os.environ["MESSAGE"]
p = os.environ["PATTERN"]
sys.exit(0 if re.search(p, m, re.IGNORECASE) else 1)
'; then
        matched_name="$name"
        matched_plugin="$plugin"
        matched_kw="$kw"
        matched_parser=$(expand_env "$parser")
        matched_handler=$(expand_env "$handler")
        matched_emoji="$emoji"
        break
    fi
done <<< "$trigger_lines"

if [[ -z "$matched_name" ]]; then
    echo '{"matched": false, "reason": "no_trigger_matched"}'
    exit 1
fi

# Resolve parser: absolute path → use as-is; bare name → chassis-shipped lib
parser_path=""
case "$matched_parser" in
    /*) parser_path="$matched_parser" ;;
    *)  parser_path="$CHASSIS_HOME/chassis/scripts/parsers/${matched_parser}.sh" ;;
esac

if [[ ! -x "$parser_path" ]]; then
    jq -n --arg trigger "$matched_name" --arg parser "$matched_parser" --arg path "$parser_path" '
        {matched: false, reason: "parser_not_executable", trigger: $trigger, parser: $parser, resolved_path: $path}'
    exit 2
fi

# Parser receives the message body on stdin and emits a JSON object on stdout.
parsed_args=$(printf '%s' "$MESSAGE_BODY" | "$parser_path" 2>/dev/null || echo '{}')

# Validate parser output is JSON; fall back to {"raw": "<body>"} if not.
if ! echo "$parsed_args" | jq empty 2>/dev/null; then
    parsed_args=$(jq -n --arg raw "$MESSAGE_BODY" '{raw: $raw}')
fi

# React (optional)
react_status="skipped"
if [[ -n "$matched_emoji" ]]; then
    if [[ -n "${DISCORD_BOT_TOKEN:-}" && -x "$CHASSIS_HOME/chassis/scripts/discord-react.py" ]]; then
        if "$CHASSIS_HOME/chassis/scripts/discord-react.py" \
                "$CHANNEL_ID" "$MESSAGE_ID" "$matched_emoji" >/dev/null 2>&1; then
            react_status="reacted"
        else
            react_status="react_failed"
        fi
    else
        react_status="emit_only"
    fi
fi

# Invoke handler
if [[ ! -x "$matched_handler" ]]; then
    jq -n --arg trigger "$matched_name" --arg handler "$matched_handler" '
        {matched: true, trigger: $trigger, reason: "handler_not_executable", handler: $handler}'
    exit 2
fi

handler_stdout_file=$(mktemp)
handler_stderr_file=$(mktemp)
trap 'rm -f "$handler_stdout_file" "$handler_stderr_file"' EXIT

handler_exit=0
TRIGGER_NAME="$matched_name" \
TRIGGER_PLUGIN="$matched_plugin" \
TRIGGER_CHANNEL_ID="$CHANNEL_ID" \
TRIGGER_MESSAGE_ID="$MESSAGE_ID" \
TRIGGER_MESSAGE_BODY="$MESSAGE_BODY" \
TRIGGER_PARSED_ARGS_JSON="$parsed_args" \
    "$matched_handler" >"$handler_stdout_file" 2>"$handler_stderr_file" || handler_exit=$?

handler_stdout=$(cat "$handler_stdout_file")
handler_stderr=$(cat "$handler_stderr_file")

jq -n \
    --arg trigger "$matched_name" \
    --arg plugin "$matched_plugin" \
    --arg emoji "$matched_emoji" \
    --arg react_status "$react_status" \
    --arg handler "$matched_handler" \
    --argjson args "$parsed_args" \
    --argjson exit_code "$handler_exit" \
    --arg handler_stdout "$handler_stdout" \
    --arg handler_stderr "$handler_stderr" '
    {
        matched: true,
        trigger: $trigger,
        plugin: $plugin,
        react_emoji: $emoji,
        react_status: $react_status,
        handler: $handler,
        args: $args,
        exit_code: $exit_code,
        handler_stdout: $handler_stdout,
        handler_stderr: $handler_stderr
    }'
