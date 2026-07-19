#!/usr/bin/env bash
# pacman.sh - Run the Pacman 4-gate URL self-improvement pipeline.
#
# Pacman is a chassis-core capability that turns the installer's "interesting
# but uncategorized" inbound URLs into either (a) one-line drops, (b) reviewed
# proposals filed in the installer's second brain, or (c) GitHub issues after
# approval. It runs the same 4-gate filter (Relevant? Beneficial? Feasible?
# Plan?) regardless of installer.
#
# Usage:
#   pacman.sh <url> [<url2> <url3> ...]   # process one or more URLs
#   pacman.sh --queue                     # drain the Pacman queue
#   pacman.sh --stdin                     # read URLs from stdin
#
# All gate logic lives in the chassis pacman skill (chassis/skills/pacman.md).
# This script is a thin invocation wrapper around `claude -p`.
#
# What changed (2026-07-19, docs/pacman-queue-storage.md): the queue moved from
# SiYuan blocks to Postgres, so PACMAN_SIYUAN_QUEUE_BLOCK_ID and
# PACMAN_SIYUAN_DROPPED_BLOCK_ID are no longer required or referenced. Proposals
# and drop records are written through the second-brain adapter
# (PACMAN_PROPOSALS_PARENT / PACMAN_DROPPED_DOC_ID), which is what makes this
# work unchanged on SiYuan, Obsidian, and Notion.
#
# Required env (chassis bootstrap hydrates these from chassis.config.yaml or
# the installer's .env):
#   PACMAN_DISCORD_CHAT_ID         Discord channel for one-line drop notes + proposal summaries
#   PACMAN_GITHUB_REPO             owner/repo for `gh issue create` on approval
#   CHASSIS_PG_DSN                 Postgres DSN holding the queue (or
#                                  BEHALFBOT_PG_DSN / JAX_PG_DSN)
#
# Optional env:
#   PACMAN_PROPOSALS_PARENT        Adapter doc id/path proposals are filed under
#   PACMAN_DROPPED_DOC_ID          Adapter doc id/path for the drop audit trail
#   PACMAN_DISCORD_CHANNEL_LABEL   Human-readable channel label for prompts (default: derived from chat_id)
#   PACMAN_GITHUB_LABELS           Comma-separated GH labels (default: pacman)
#   PACMAN_MAX_BATCH_URLS          Per-invocation URL cap (default: 10)
#   PACMAN_HARD_PAUSE              File path; if exists, script exits 0 without invoking claude
#
# Chassis root resolution: $CHASSIS_HOME (set by container entrypoint) >
# $CUSTOMER_HOME > the directory two levels up from this script.

set -euo pipefail

CHASSIS_ROOT="${CHASSIS_HOME:-${CUSTOMER_HOME:-$(cd "$(dirname "$0")/../.." && pwd)}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$CHASSIS_ROOT/logs/pacman"
mkdir -p "$LOG_DIR"

if [[ $# -lt 1 ]]; then
  echo "Usage: pacman.sh <url> [<url2> ...] | --queue | --stdin" >&2
  exit 1
fi

PAUSE_FILE="${PACMAN_HARD_PAUSE:-$CHASSIS_ROOT/PACMAN_HARD_PAUSE}"
if [[ -f "$PAUSE_FILE" ]]; then
  echo "Pacman is paused ($PAUSE_FILE exists). Delete the flag to resume." >&2
  exit 0
fi

: "${PACMAN_DISCORD_CHAT_ID:?PACMAN_DISCORD_CHAT_ID must be set}"
: "${PACMAN_GITHUB_REPO:?PACMAN_GITHUB_REPO must be set}"

PACMAN_DISCORD_CHANNEL_LABEL="${PACMAN_DISCORD_CHANNEL_LABEL:-Discord channel ${PACMAN_DISCORD_CHAT_ID}}"
PACMAN_MAX_BATCH_URLS="${PACMAN_MAX_BATCH_URLS:-10}"

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DATE="$(date -u +%Y-%m-%d)"
LOG_FILE="$LOG_DIR/$DATE.jsonl"

if [[ "$1" == "--queue" ]]; then
  # Fail before spending a Claude invocation if the queue is unreachable. The
  # drain prompt would otherwise discover this partway through and report a
  # clean run over an empty result, which is the exact failure PR #78 removed.
  if ! python3 "$SCRIPT_DIR/pacman-queue.py" count >/dev/null; then
    echo "Pacman queue is unreachable - not invoking claude. See the error above." >&2
    exit 1
  fi
  PROMPT="Read chassis/skills/pacman.md. Drain the Pacman queue. Claim up to ${PACMAN_MAX_BATCH_URLS} URLs with 'python3 chassis/scripts/pacman-queue.py claim --limit ${PACMAN_MAX_BATCH_URLS}'. For each claimed row: run the 4-gate pipeline, then mark it done with 'python3 chassis/scripts/pacman-queue.py complete <token> --verdict <drop|proposal|fetch_failed>'. Drops post one-line notes to ${PACMAN_DISCORD_CHANNEL_LABEL} (chat_id ${PACMAN_DISCORD_CHAT_ID}). Proposals get written through mcp__secondbrain__create_doc and posted to ${PACMAN_DISCORD_CHANNEL_LABEL} with approve/reject prompts quoting the row's approval token."
  echo "{\"ts\":\"$TS\",\"mode\":\"queue\",\"event\":\"invoke\"}" >> "$LOG_FILE"
  claude -p "$PROMPT"
  echo "{\"ts\":\"$TS\",\"mode\":\"queue\",\"event\":\"complete\"}" >> "$LOG_FILE"
  exit 0
fi

URLS=()
if [[ "$1" == "--stdin" ]]; then
  while IFS= read -r line; do
    while IFS= read -r url; do
      [[ -n "$url" ]] && URLS+=("$url")
    done < <(echo "$line" | grep -oE 'https?://[^[:space:]<>"'"'"'`,]+' || true)
  done
else
  for arg in "$@"; do
    while IFS= read -r url; do
      [[ -n "$url" ]] && URLS+=("$url")
    done < <(echo "$arg" | grep -oE 'https?://[^[:space:]<>"'"'"'`,]+' || true)
  done
fi

if [[ ${#URLS[@]} -eq 0 ]]; then
  echo "No valid URLs found in input." >&2
  exit 1
fi

URL_LIST=$(printf -- "- %s\n" "${URLS[@]}")
COUNT=${#URLS[@]}
PROMPT="Read chassis/skills/pacman.md. Run the Pacman 4-gate pipeline on the following ${COUNT} URL(s):

${URL_LIST}

Source: manual CLI invocation (multi-URL batch). For each URL: run the 4 gates. Drops post one-line notes to ${PACMAN_DISCORD_CHANNEL_LABEL} (chat_id ${PACMAN_DISCORD_CHAT_ID}). Proposals get written through mcp__secondbrain__create_doc and posted to ${PACMAN_DISCORD_CHANNEL_LABEL} with approve/reject prompts. These URLs came from the CLI, not from the queue, so there is no queue row to complete unless you enqueued them yourself. Process URLs sequentially. After all URLs are processed, post a single batch-summary line to ${PACMAN_DISCORD_CHANNEL_LABEL}: 'Pacman batch: processed ${COUNT} URL(s) (P proposals, D drops).'"

echo "{\"ts\":\"$TS\",\"mode\":\"batch\",\"url_count\":${COUNT},\"event\":\"invoke\"}" >> "$LOG_FILE"
claude -p "$PROMPT"
echo "{\"ts\":\"$TS\",\"mode\":\"batch\",\"url_count\":${COUNT},\"event\":\"complete\"}" >> "$LOG_FILE"
