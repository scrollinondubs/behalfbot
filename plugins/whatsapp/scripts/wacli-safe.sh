#!/bin/bash
# wacli-safe.sh — privacy-enforced wrapper around wacli (#119, #384).
#
# Rationale (Sean voice memo 2026-04-30 #<primary> 1499334593999015966):
#   "WhatsApp 1-on-1 messages have an expectation of privacy that I'm violating
#    by virtue of having you with access to read everything... can you establish
#    some guardrails that prevent you from reading direct WhatsApp messages and
#    only read the groups?"
#
# Enforcement:
#   1. Only `wacli messages list` and `wacli messages search` are routed through;
#      all other subcommands are passed through unchanged (chats list, contacts,
#      groups, doctor, version, etc. — these are metadata, not message content).
#   2. Any --chat / --from filter MUST resolve to a JID present in the allowlist
#      groups[] array of data/whatsapp-allowlist.json.
#   3. Any unfiltered `messages list` or `messages search` is REJECTED — the
#      wrapper requires an explicit allowlisted --chat. (Otherwise it would
#      bleed DMs into output.)
#   4. Wildcards / regex / multi-JID filters are NOT supported — one allowlisted
#      JID per call.
#
# Usage (drop-in for read paths):
#   wacli-safe.sh messages list --chat 120363397436589829@g.us --limit 50
#   wacli-safe.sh messages search "vibecode" --chat 120363397436589829@g.us
#   wacli-safe.sh chats list --json                # passes through (metadata only)
#   wacli-safe.sh groups list                      # passes through
#
# Anything outside this wrapper (raw `wacli messages ...` calls) is blocked at
# the .claude/hooks/guardrails.sh layer — see that file for the second-line defense.

set -euo pipefail

ALLOWLIST_FILE="${WHATSAPP_ALLOWLIST_PATH:-${CHASSIS_HOME}/plugins/whatsapp/data/whatsapp-allowlist.json}"

if [[ ! -f "$ALLOWLIST_FILE" ]]; then
  echo "wacli-safe: allowlist file missing at $ALLOWLIST_FILE — refusing to run" >&2
  exit 2
fi

# Subcommand routing
SUBCMD="${1:-}"
SUB2="${2:-}"

# Pass-through subcommands (no message content; metadata only).
# Note: `messages context` and `media download` are intentionally NOT in this list —
# they expose message content and require allowlist enforcement, which we don't
# implement yet, so they're blocked entirely. If Sean needs them, lift them in
# this wrapper with the same JID check.
case "$SUBCMD" in
  chats|contacts|groups|doctor|version|auth|sync|history|completion|help|--help|-h|--version|-v|"")
    exec wacli "$@"
    ;;
  messages)
    case "$SUB2" in
      list|search)
        # Continue to enforcement below
        ;;
      context|"")
        echo "wacli-safe: 'wacli messages $SUB2' is not allowed via the safe wrapper. Reason: it can expose message content from chats ${ASSISTANT_NAME} is not allowlisted to read. If you need message context, ask Sean." >&2
        exit 3
        ;;
      *)
        # Unknown messages subcommand — block to be safe
        echo "wacli-safe: unknown 'wacli messages $SUB2' subcommand — blocked. Update wacli-safe.sh if this is a legitimate read path." >&2
        exit 3
        ;;
    esac
    ;;
  send|media)
    echo "wacli-safe: '$SUBCMD' is a write/media path — ${ASSISTANT_NAME} is not authorized for this without Sean's explicit approval." >&2
    exit 3
    ;;
  *)
    # Unknown subcommand — pass through (wacli will error or handle); prefer fail-open
    # for non-message paths so the wrapper doesn't break tooling we haven't seen yet.
    exec wacli "$@"
    ;;
esac

# Below this point: $SUBCMD == "messages" && $SUB2 in {list, search}.
# Extract --chat / --from value.

CHAT_JID=""
FROM_JID=""
HAS_FILTER="false"

# Re-scan args looking for --chat or --from and capture value.
ARGS=("$@")
for ((i=0; i<${#ARGS[@]}; i++)); do
  case "${ARGS[$i]}" in
    --chat)
      CHAT_JID="${ARGS[$((i+1))]:-}"
      HAS_FILTER="true"
      ;;
    --chat=*)
      CHAT_JID="${ARGS[$i]#--chat=}"
      HAS_FILTER="true"
      ;;
    --from)
      FROM_JID="${ARGS[$((i+1))]:-}"
      HAS_FILTER="true"
      ;;
    --from=*)
      FROM_JID="${ARGS[$i]#--from=}"
      HAS_FILTER="true"
      ;;
  esac
done

if [[ "$HAS_FILTER" != "true" ]]; then
  echo "wacli-safe: 'wacli messages $SUB2' must include --chat <JID> targeting an allowlisted group. Unfiltered queries would bleed DMs into output and are blocked." >&2
  echo "wacli-safe: allowlisted groups are listed in $ALLOWLIST_FILE — copy a JID from groups[].jid." >&2
  exit 3
fi

# --from is for individual senders — by definition not allowlistable as a group.
# (--from inside an allowlisted --chat is fine and would have set CHAT_JID; --from
# alone is not.)
if [[ -n "$FROM_JID" && -z "$CHAT_JID" ]]; then
  echo "wacli-safe: 'wacli messages $SUB2 --from <JID>' alone reads across all chats including DMs — blocked. Combine with --chat <allowlisted-group-JID> to scope." >&2
  exit 3
fi

# Validate the JID is in the allowlist.
TARGET_JID="$CHAT_JID"
if [[ ! "$TARGET_JID" == *@g.us ]]; then
  echo "wacli-safe: --chat $TARGET_JID is not a group JID (must end @g.us). DM JIDs (@s.whatsapp.net), individual JIDs (@lid), and newsletters (@newsletter) are blocked." >&2
  exit 3
fi

# Cross-check against allowlist file using jq.
if ! command -v jq >/dev/null 2>&1; then
  echo "wacli-safe: jq is required to validate allowlist — install with 'brew install jq'." >&2
  exit 2
fi

IN_ALLOWLIST=$(jq -r --arg j "$TARGET_JID" '.groups[]? | select(.jid == $j) | .name' "$ALLOWLIST_FILE" 2>/dev/null || echo "")

if [[ -z "$IN_ALLOWLIST" ]]; then
  echo "wacli-safe: group JID $TARGET_JID is NOT in the allowlist ($ALLOWLIST_FILE)." >&2
  echo "wacli-safe: to add it, edit the file and append a {jid, name} entry to groups[]. Then ask Sean to approve the change." >&2
  exit 3
fi

# All checks passed — execute.
exec wacli "$@"
