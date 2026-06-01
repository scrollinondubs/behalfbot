#!/bin/bash
# .claude/hooks/guardrails.sh — chassis-side deterministic safety guardrails.
#
# Runs before every tool call. Blocks destructive operations that would
# violate the chassis's hard limits, enforced at the shell level so the
# LLM cannot bypass them.
#
# Lessons baked in:
#   #4  Discord-channel mode incompatible with auto-mode → guardrails ARE the
#       safety mechanism in --dangerously-skip-permissions setups
#   #6  Hard limits live at hook layer, not in CLAUDE.md alone
#   #13 Race-collapse pattern (relevant to lock semantics; not handled here)
#   #27 Substring-grep guardrails false-positive on heredocs → command-name
#       detection MUST be anchored to actual command boundaries, NOT bare
#       string-grep. The HTTP-mutation block below uses anchored regex.
#   #30 Privacy boundaries surface-specific → WhatsApp + Twitter/LinkedIn/
#       Slack DM blocks below
#
# Configuration (environment-driven; chassis bootstrap hydrates):
#   CHASSIS_HOME              — installer chassis root (REQUIRED)
#   CHASSIS_HTTP_ALLOWLIST    — pipe-separated regex of allowed hosts for
#                                mutating HTTP. Defaults to localhost only.
#                                Installers extend via chassis.config.yaml +
#                                bootstrap script writes the env into a
#                                source-able file the launchd / systemd
#                                unit pulls in.
#   CHASSIS_DIR_ALLOWLIST     — colon-separated list of directory prefixes
#                                Write/Edit may target. Defaults to
#                                ${CHASSIS_HOME}.
#   CHASSIS_E2E_FLAG_PATH     — path to the E2E flag-file. Defaults to
#                                ${CHASSIS_HOME}/.claude/<v1-reference-install>-e2e-mode.flag
#                                (kept for backward-compat with V1 reference;
#                                installers may rename).
#   CHASSIS_WHATSAPP_SAFE     — path to wacli-safe.sh wrapper. If set,
#                                wacli messages|send|media calls outside this
#                                wrapper are blocked. If unset (no whatsapp
#                                plugin), the block is inactive.

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')

deny() {
  jq -n --arg reason "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
}

CHASSIS_HOME="${CHASSIS_HOME:-}"
if [[ -z "$CHASSIS_HOME" ]]; then
  # Without CHASSIS_HOME we can't enforce the directory allowlist properly.
  # Fail-deny: better to block all writes than to allow writes to anywhere.
  if [[ "$TOOL" == "Write" || "$TOOL" == "Edit" ]]; then
    deny "Blocked: CHASSIS_HOME not set; cannot enforce directory allowlist. Set CHASSIS_HOME in your launchd/systemd unit env."
  fi
fi

# ─── Bash command guardrails ───────────────────────────────────────────

if [[ "$TOOL" == "Bash" ]]; then
  CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

  # Block destructive filesystem operations
  if echo "$CMD" | grep -qiE '^\s*rm\s+-rf\s+/|^\s*sudo\s+rm|^\s*rm\s+-rf\s+~'; then
    deny "Blocked: destructive filesystem deletion. Use 'trash' instead of 'rm -rf', or ask the installer for approval."
  fi

  # Block force-push
  if echo "$CMD" | grep -qiE 'git\s+push\s+.*--force|git\s+push\s+-f'; then
    deny "Blocked: force-push is prohibited. Use 'git revert' to undo commits safely."
  fi

  # Block push to main/master
  if echo "$CMD" | grep -qiE 'git\s+push\s+.*(main|master)\b'; then
    deny "Blocked: pushing directly to main/master is prohibited. Create a PR instead."
  fi

  # Block hard reset
  if echo "$CMD" | grep -qiE 'git\s+reset\s+--hard'; then
    deny "Blocked: git reset --hard can destroy work. Use 'git stash' or 'git revert' instead."
  fi

  # Block git clean -f (deletes untracked files)
  if echo "$CMD" | grep -qiE 'git\s+clean\s+-f'; then
    deny "Blocked: git clean -f deletes untracked files permanently. Review files manually first."
  fi

  # Block SQL destructive operations via any CLI (psql, mysql, sqlite3, turso, etc.)
  # Exception: allow schema changes explicitly targeting -preview databases.
  if echo "$CMD" | grep -qiE 'DROP\s+(TABLE|DATABASE|INDEX|SCHEMA)|TRUNCATE\s+TABLE|DELETE\s+FROM\s+\S+\s*(;|$)'; then
    if ! echo "$CMD" | grep -qE '\-preview([^a-zA-Z0-9]|$)'; then
      deny "Blocked: destructive SQL operation detected. Database schema changes require installer approval. (Exception: -preview DBs are allowed.)"
    fi
  fi

  # Block lat/lng coordinate strings in commits (Angel Protocol rule, per
  # plugins/angel-protocol). Coords are local-only at ~/.angel-vault/ —
  # never in repo / commits / PRs / issues. Doxxing risk.
  if echo "$CMD" | grep -qE '(git commit|git add|gh issue create|gh pr create|gh issue comment|gh pr comment).*-?[0-9]{1,3}\.[0-9]{4,}\s*,\s*-?[0-9]{1,3}\.[0-9]{4,}'; then
    deny "Blocked: lat/lng coordinate string detected in a commit/issue/PR command. Coords are local-only (~/.angel-vault/). Never commit them. (Angel Protocol rule.)"
  fi

  # Block sending email via CLI tools
  if echo "$CMD" | grep -qiE '^\s*(mail|sendmail|mutt|msmtp)\s+'; then
    deny "Blocked: sending email via CLI requires installer approval."
  fi

  # Mutating HTTP to external APIs is blocked by default. Two escape hatches:
  #
  # 1. CHASSIS_HTTP_ALLOWLIST — installer-driven regex of allowed hosts
  #    (chassis bootstrap writes this from chassis.config.yaml).
  # 2. CHASSIS_E2E_FLAG_PATH — when this flag-file exists AND is younger
  #    than 2h, the allowlist extends to cover Discord webhooks + Stripe
  #    test mode for end-to-end dogfooding runs. Toggled by:
  #      touch <flag-path>     # ON
  #      rm -f <flag-path>     # OFF
  #
  # Per lesson #27: the curl/wget detection below is anchored to actual
  # command boundaries (start-of-string, after ;, &, |, (, backtick, $())
  # so text inside string literals or heredocs doesn't false-positive.

  E2E_FLAG="${CHASSIS_E2E_FLAG_PATH:-${CHASSIS_HOME}/.claude/<v1-reference-install>-e2e-mode.flag}"
  E2E_ON="false"
  if [[ -f "$E2E_FLAG" ]]; then
    FLAG_AGE_SEC=$(( $(date +%s) - $(stat -f %m "$E2E_FLAG" 2>/dev/null || stat -c %Y "$E2E_FLAG" 2>/dev/null || echo 0) ))
    if [[ $FLAG_AGE_SEC -lt 7200 ]]; then
      E2E_ON="true"
    fi
  fi

  ALLOW_PATTERN="${CHASSIS_HTTP_ALLOWLIST:-(localhost|127\.0\.0\.1)}"
  if [[ "$E2E_ON" == "true" ]]; then
    ALLOW_PATTERN="${ALLOW_PATTERN}|discord\.com/api/webhooks|api\.stripe\.com|api\.telnyx\.com"
  fi

  # Anchored curl/wget detection — at start-of-string OR after a command-
  # boundary character. Prevents heredoc-body false positives (lesson #27).
  CURL_BOUNDARY='(^|[;&|`( ]|\$\()(curl|wget)\s'
  if echo "$CMD" | grep -qE "$CURL_BOUNDARY" && \
     echo "$CMD" | grep -qiE '(curl|wget)\s+.*-X\s*(POST|PUT|DELETE|PATCH)' && \
     ! echo "$CMD" | grep -qE "$ALLOW_PATTERN"; then
    deny "Blocked: mutating HTTP request to external API. Add the host to CHASSIS_HTTP_ALLOWLIST or toggle the E2E flag-file. (E2E_MODE=$E2E_ON)"
  fi
  if echo "$CMD" | grep -qE "$CURL_BOUNDARY" && \
     echo "$CMD" | grep -qiE '(curl|wget)\s+.*(-d\s|--data)' && \
     ! echo "$CMD" | grep -qE "$ALLOW_PATTERN"; then
    deny "Blocked: mutating HTTP request to external API. Add the host to CHASSIS_HTTP_ALLOWLIST or toggle the E2E flag-file. (E2E_MODE=$E2E_ON)"
  fi

  # ─── WhatsApp privacy guardrail (lesson #30 + WhatsApp plugin) ─────
  # Force all wacli reads through the chassis-installed wacli-safe.sh
  # wrapper, which enforces a groups-only allowlist. Direct DM access is
  # blocked even via the wrapper. This block is conditional on the
  # WhatsApp plugin being enabled (CHASSIS_WHATSAPP_SAFE set by chassis
  # bootstrap when the plugin is on).
  if [[ -n "${CHASSIS_WHATSAPP_SAFE:-}" ]]; then
    if echo "$CMD" | grep -qE '(^|[;&|`( ])wacli\s+(messages|send|media)\b' && \
       ! echo "$CMD" | grep -qE "$(printf '%s' "$CHASSIS_WHATSAPP_SAFE" | sed 's/[.[\\*^$()+?{|]/\\&/g')"; then
      deny "Blocked: raw 'wacli messages/send/media' is prohibited (WhatsApp privacy rule). Use ${CHASSIS_WHATSAPP_SAFE} which enforces the groups-only allowlist. DMs are off-limits."
    fi
  fi

  # ─── Twitter / LinkedIn / Slack DM blocks (lesson #30) ─────────────
  # Playwright commands targeting DM URLs are off-limits regardless of
  # whether the dating / social plugins are enabled.
  if echo "$CMD" | grep -qiE '(twitter|x)\.com/messages|linkedin\.com/messaging/|/messages/D[A-Z0-9]+'; then
    deny "Blocked: Twitter / LinkedIn / Slack DM access via Playwright is prohibited (privacy boundary). DMs carry an expectation of privacy from senders that the installer cannot unilaterally grant."
  fi
fi

# ─── Playwright MCP tool-level DM blocks (lesson #30 — defense-in-depth) ───
# The Bash-level check above catches `playwright cli ... <DM-URL>` invocations
# but MISSES direct MCP tool calls (`mcp__playwright__browser_navigate {url: ...}`).
# An adversary / prompt injection / agent error that invokes the MCP tool
# directly with a DM URL bypasses the bash-layer guard. This block intercepts
# the MCP tool call at the URL-arg level.
#
# Brings <v1-reference-install> PR #521 (merged 2026-05-09) into chassis. See chassis#94.

if [[ "$TOOL" == "mcp__playwright__browser_navigate" ]]; then
  NAV_URL=$(echo "$INPUT" | jq -r '.tool_input.url // empty')

  if [[ -n "$NAV_URL" ]]; then
    # Twitter/X: block DM inbox (/messages path)
    if echo "$NAV_URL" | grep -qiE '(twitter\.com|x\.com)/messages'; then
      deny "Blocked: Twitter/X DM inbox is off-limits per the chassis privacy boundary. If the installer forwarded a DM via their chat channel, act on the forwarded content — do not navigate to the DM directly."
    fi

    # LinkedIn: block messaging inbox
    if echo "$NAV_URL" | grep -qiE 'linkedin\.com/messaging/'; then
      deny "Blocked: LinkedIn DM/messaging is off-limits per the chassis privacy boundary. Forward DM content via the installer's chat channel if they want chassis to see it."
    fi

    # Slack: block DMs and enforce channel allowlist (if configured).
    # DM channel IDs start with D; non-DM channels must appear in
    # ${CHASSIS_HOME}/config/slack-channels.json (workspaces[].channels[].channel_id).
    if echo "$NAV_URL" | grep -qiE 'app\.slack\.com'; then
      SLACK_CHANNEL_ID=$(echo "$NAV_URL" | grep -oE '/client/[A-Z0-9]+/([A-Z0-9]+)' | sed 's|.*/||' || true)

      if [[ -n "$SLACK_CHANNEL_ID" ]]; then
        # DM channel IDs start with D — block regardless of workspace
        if echo "$SLACK_CHANNEL_ID" | grep -qE '^D[A-Z0-9]+$'; then
          deny "Blocked: Slack DM navigation is off-limits per the chassis privacy boundary. Forward content via the installer's chat channel if they want chassis to see it."
        fi

        # Non-DM channels: must appear in the install's allowlist
        ALLOWLIST_FILE="${CHASSIS_HOME:-/app/customer}/config/slack-channels.json"
        if [[ -f "$ALLOWLIST_FILE" ]]; then
          if ! jq -r '[.workspaces[].channels[].channel_id] | .[]' "$ALLOWLIST_FILE" 2>/dev/null | grep -qF "$SLACK_CHANNEL_ID"; then
            deny "Blocked: Slack channel $SLACK_CHANNEL_ID is not in the allowlist at config/slack-channels.json. Ask the installer to add it before chassis can access it."
          fi
        fi
      fi
    fi
  fi
fi

# ─── GitHub MCP guardrails ────────────────────────────────────────────

if [[ "$TOOL" == "mcp__github__merge_pull_request" ]]; then
  deny "Blocked: chassis cannot merge pull requests. PRs must be reviewed and merged by the installer."
fi

if [[ "$TOOL" == "mcp__github__delete_repository" ]]; then
  deny "Blocked: repository deletion is prohibited."
fi

# ─── Turso (production DB) guardrails ─────────────────────────────────

if [[ "$TOOL" == "mcp__turso__delete_database" ]]; then
  deny "Blocked: deleting a production database is prohibited. Ask the installer for approval."
fi

# Block destructive SQL via Turso execute_query, except on -preview DBs
if [[ "$TOOL" == "mcp__turso__execute_query" ]]; then
  QUERY=$(echo "$INPUT" | jq -r '.tool_input.query // .tool_input.sql // empty')
  DB=$(echo "$INPUT" | jq -r '.tool_input.database // empty')
  if echo "$QUERY" | grep -qiE 'DROP\s+(TABLE|DATABASE|INDEX)|TRUNCATE\s+TABLE|DELETE\s+FROM\s+\S+\s*(WHERE\s+1|;|$)'; then
    if [[ ! "$DB" =~ -preview$ ]]; then
      deny "Blocked: destructive SQL on production database ($DB). Schema changes and bulk deletes require installer approval. (Exception: -preview DBs are allowed.)"
    fi
  fi
fi

# ─── File write guardrails ────────────────────────────────────────────

if [[ "$TOOL" == "Write" || "$TOOL" == "Edit" ]]; then
  FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

  if [[ -z "$FILE_PATH" ]]; then
    exit 0
  fi

  # Default allowlist: just CHASSIS_HOME. Installers extend via
  # CHASSIS_DIR_ALLOWLIST (colon-separated absolute paths) — chassis
  # bootstrap writes this from chassis.config.yaml.guardrails.directory_allowlist.
  ALLOWED="${CHASSIS_DIR_ALLOWLIST:-${CHASSIS_HOME}}"

  IFS=':' read -ra ALLOWED_DIRS <<< "$ALLOWED"
  ALLOWED_MATCH=false
  for dir in "${ALLOWED_DIRS[@]}"; do
    [[ -z "$dir" ]] && continue
    if [[ "$FILE_PATH" == "$dir"/* || "$FILE_PATH" == "$dir" ]]; then
      ALLOWED_MATCH=true
      break
    fi
  done

  # Always allow harness config under ~/.claude/
  if [[ "$FILE_PATH" == "$HOME/.claude/"* ]]; then
    ALLOWED_MATCH=true
  fi

  if [[ "$ALLOWED_MATCH" == "false" ]]; then
    deny "Blocked: writing outside allowed directories ($FILE_PATH). Allowed: $ALLOWED, plus $HOME/.claude/. Extend via chassis.config.yaml.guardrails.directory_allowlist."
  fi
fi

# ─── Gmail guardrails ────────────────────────────────────────────────

# Gmail MCP only exposes create_draft, list_drafts, etc. — no send tool.
# Drafts require explicit installer action to send. No additional block needed.

# ─── All clear ────────────────────────────────────────────────────────
exit 0
