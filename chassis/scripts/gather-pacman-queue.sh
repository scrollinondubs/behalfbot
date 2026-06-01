#!/usr/bin/env bash
# gather-pacman-queue.sh — Count unprocessed URL blocks in the SiYuan
# /To Investigate queue (Pacman's inbound). Called by the heartbeat
# dispatcher (cadence per the installer's `pacman-drain` heartbeat entry,
# typically every 4h). Outputs JSON `{"count": N}` where N is the number
# of blocks under the queue parent that contain http/https URLs.
#
# Output contract: JSON object with `count` field, per dispatcher
# threshold-condition logic. The dispatcher only fires Claude when N > 0,
# so the steady-state cost is zero Claude tokens.
#
# Required env (chassis bootstrap hydrates from chassis.config.yaml or
# the installer's .env; see chassis/skills/pacman.md for the contract):
#   SIYUAN_TOKEN                    SiYuan API token
#   PACMAN_SIYUAN_QUEUE_BLOCK_ID    /To Investigate queue parent block ID
#                                   (format: YYYYMMDDHHMMSS-XXXXXXX)
#
# Optional env:
#   SIYUAN_URL                      SiYuan API endpoint (default: http://localhost:6806)
#   CHASSIS_HOME / CHASSIS_HOME         Install root (for .env source + pause file)
#   PACMAN_HARD_PAUSE               File path to a pause sentinel; if it
#                                   exists, the script returns count=0
#                                   without querying. Default:
#                                   $CHASSIS_HOME/PACMAN_HARD_PAUSE

set -euo pipefail

CHASSIS_ROOT="${CHASSIS_HOME:-${CHASSIS_HOME:-$(cd "$(dirname "$0")/../.." && pwd)}}"

PAUSE_FILE="${PACMAN_HARD_PAUSE:-$CHASSIS_ROOT/PACMAN_HARD_PAUSE}"
if [[ -f "$PAUSE_FILE" ]]; then
  echo '{"count": 0, "reason": "PACMAN_HARD_PAUSE flag set"}'
  exit 0
fi

# Source .env if present (literal-only; container installs get env via
# compose's env_file directive, so this source is a no-op there).
set -f
[[ -f "$CHASSIS_ROOT/.env" ]] && source "$CHASSIS_ROOT/.env" 2>/dev/null || true
set +f

if [[ -z "${SIYUAN_TOKEN:-}" ]]; then
  echo '{"count": 0, "reason": "SIYUAN_TOKEN not set"}'
  exit 0
fi

if [[ -z "${PACMAN_SIYUAN_QUEUE_BLOCK_ID:-}" ]]; then
  echo '{"count": 0, "reason": "PACMAN_SIYUAN_QUEUE_BLOCK_ID not set"}'
  exit 0
fi

SIYUAN_URL="${SIYUAN_URL:-http://localhost:6806}"
TO_INVESTIGATE_ID="$PACMAN_SIYUAN_QUEUE_BLOCK_ID"

# Query: count blocks under the queue parent that contain a URL (http/https)
# and are not the parent doc itself.
SQL='SELECT COUNT(*) AS n FROM blocks WHERE root_id = '"'"'${TO_INVESTIGATE_ID}'"'"' AND id != '"'"'${TO_INVESTIGATE_ID}'"'"' AND type IN ('"'"'h'"'"', '"'"'p'"'"', '"'"'l'"'"', '"'"'i'"'"') AND content LIKE '"'"'%http%'"'"''
SQL_EXPANDED="${SQL//\$\{TO_INVESTIGATE_ID\}/$TO_INVESTIGATE_ID}"

PAYLOAD=$(jq -nc --arg stmt "$SQL_EXPANDED" '{stmt: $stmt}')

RESP=$(curl -sf -X POST \
  -H "Authorization: Token ${SIYUAN_TOKEN}" \
  -H "Content-Type: application/json" \
  --data "$PAYLOAD" \
  "${SIYUAN_URL}/api/query/sql" 2>/dev/null || echo '{"code":-1}')

CODE=$(echo "$RESP" | jq -r '.code // -1')

if [[ "$CODE" != "0" ]]; then
  echo '{"count": 0, "reason": "siyuan query failed"}'
  exit 0
fi

COUNT=$(echo "$RESP" | jq -r '.data[0].n // 0')

echo "{\"count\": $COUNT}"
