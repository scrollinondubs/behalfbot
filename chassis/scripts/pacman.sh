#!/usr/bin/env bash
# pacman.sh — Run the Pacman 4-gate URL self-improvement pipeline.
#
# Pacman is a chassis-core capability that turns the installer's "interesting
# but uncategorized" inbound URLs into either (a) one-line drops, (b) reviewed
# proposals filed in SiYuan, or (c) GitHub issues after approval. It runs the
# same 4-gate filter (Relevant? Beneficial? Feasible? Plan?) regardless of
# installer.
#
# Usage:
#   pacman.sh <url> [<url2> <url3> ...]   # process one or more URLs
#   pacman.sh --queue                     # drain SiYuan queue
#   pacman.sh --stdin                     # read URLs from stdin
#
# All gate logic lives in the chassis pacman skill (chassis/skills/pacman.md).
# This script is a thin invocation wrapper around `claude -p`.
#
# Required env (chassis bootstrap hydrates these from chassis.config.yaml or
# the installer's .env):
#   PACMAN_DISCORD_CHAT_ID         Discord channel for one-line drop notes + proposal summaries
#   PACMAN_SIYUAN_QUEUE_BLOCK_ID   SiYuan parent block for the /To Investigate queue
#   PACMAN_SIYUAN_DROPPED_BLOCK_ID SiYuan parent block for the /Dropped archive
#   PACMAN_GITHUB_REPO             owner/repo for `gh issue create` on approval
#
# Optional env:
#   PACMAN_DISCORD_CHANNEL_LABEL   Human-readable channel label for prompts (default: derived from chat_id)
#   PACMAN_GITHUB_LABELS           Comma-separated GH labels (default: pacman)
#   PACMAN_MAX_BATCH_URLS          Per-invocation URL cap (default: 10)
#   PACMAN_HARD_PAUSE              File path; if exists, script exits 0 without invoking claude
#
# Chassis root resolution: $CHASSIS_HOME (set by container entrypoint) >
# $CHASSIS_HOME (legacy) > the directory two levels up from this script.

set -euo pipefail

CHASSIS_ROOT="${CHASSIS_HOME:-${CHASSIS_HOME:-$(cd "$(dirname "$0")/../.." && pwd)}}"
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
: "${PACMAN_SIYUAN_QUEUE_BLOCK_ID:?PACMAN_SIYUAN_QUEUE_BLOCK_ID must be set}"
: "${PACMAN_GITHUB_REPO:?PACMAN_GITHUB_REPO must be set}"

PACMAN_DISCORD_CHANNEL_LABEL="${PACMAN_DISCORD_CHANNEL_LABEL:-Discord channel ${PACMAN_DISCORD_CHAT_ID}}"
PACMAN_MAX_BATCH_URLS="${PACMAN_MAX_BATCH_URLS:-10}"

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DATE="$(date -u +%Y-%m-%d)"
LOG_FILE="$LOG_DIR/$DATE.jsonl"

if [[ "$1" == "--queue" ]]; then
  PROMPT="Read chassis/skills/pacman.md. Drain the SiYuan /To Investigate queue (block ${PACMAN_SIYUAN_QUEUE_BLOCK_ID}). Process up to ${PACMAN_MAX_BATCH_URLS} URLs that don't yet have a pacman_processed attribute. For each: run the 4-gate pipeline. Drops post one-line notes to ${PACMAN_DISCORD_CHANNEL_LABEL} (chat_id ${PACMAN_DISCORD_CHAT_ID}). Proposals get written as SiYuan sub-docs and posted to ${PACMAN_DISCORD_CHANNEL_LABEL} with approve/reject prompts. Cap batch at ${PACMAN_MAX_BATCH_URLS} URLs."
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

Source: manual CLI invocation (multi-URL batch). For each URL: run the 4 gates. Drops post one-line notes to ${PACMAN_DISCORD_CHANNEL_LABEL} (chat_id ${PACMAN_DISCORD_CHAT_ID}). Proposals get written as SiYuan sub-docs and posted to ${PACMAN_DISCORD_CHANNEL_LABEL} with approve/reject prompts. Process URLs sequentially. After all URLs are processed, post a single batch-summary line to ${PACMAN_DISCORD_CHANNEL_LABEL}: 'Pacman batch: processed ${COUNT} URL(s) (P proposals, D drops).'"

echo "{\"ts\":\"$TS\",\"mode\":\"batch\",\"url_count\":${COUNT},\"event\":\"invoke\"}" >> "$LOG_FILE"
claude -p "$PROMPT"
echo "{\"ts\":\"$TS\",\"mode\":\"batch\",\"url_count\":${COUNT},\"event\":\"complete\"}" >> "$LOG_FILE"
