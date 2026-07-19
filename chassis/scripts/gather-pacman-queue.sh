#!/usr/bin/env bash
# gather-pacman-queue.sh - Count claimable URLs in the Pacman queue.
#
# Called by the heartbeat dispatcher (cadence per the installer's
# `pacman-drain` heartbeat entry, typically every 4h). Outputs JSON
# `{"count": N}` where N is the number of queue rows a drain could pick up
# right now.
#
# Output contract: JSON object with a `count` field, per the dispatcher's
# threshold-condition logic. The dispatcher only fires Claude when N > 0, so
# the steady-state cost is zero Claude tokens.
#
# What changed (2026-07-19, docs/pacman-queue-storage.md): this used to run a
# SiYuan SQL query counting blocks under PACMAN_SIYUAN_QUEUE_BLOCK_ID, which
# meant the count was always zero on Obsidian, Notion, and adapter-mode
# installs. It now reads chassis_pacman_queue via chassis/scripts/pacman-queue.py.
#
# The count predicate is NOT reimplemented here. It lives in
# chassis/pacman/queue.py (PENDING_PREDICATE) and is shared with the claim
# path, because a count that disagrees with what a drain actually picks up
# either burns Claude tokens on an empty queue or leaves a queue stuck.
#
# Failure behaviour changed too, deliberately. The old script printed
# `{"count": 0, "reason": "..."}` and exited 0 on every failure, so an
# unreachable backend looked exactly like an empty queue - the silent-drain
# failure mode PR #78 exists to remove. A DB error now exits non-zero, which
# the dispatcher records as `gather_failed` and alerts on.
#
# Required env:
#   CHASSIS_PG_DSN (or BEHALFBOT_PG_DSN / JAX_PG_DSN)  Postgres DSN
#
# Optional env:
#   CHASSIS_HOME / CUSTOMER_HOME  Install root (for .env source + pause file)
#   PACMAN_HARD_PAUSE             Pause sentinel path; if the file exists the
#                                 script returns count=0 without querying.
#                                 Default: $CHASSIS_HOME/PACMAN_HARD_PAUSE
#   PACMAN_CLAIM_TIMEOUT_MINUTES  Minutes before a claimed-but-unprocessed row
#                                 becomes claimable again (default 60)

set -euo pipefail

CHASSIS_ROOT="${CHASSIS_HOME:-${CUSTOMER_HOME:-$(cd "$(dirname "$0")/../.." && pwd)}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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

if ! OUTPUT=$(python3 "$SCRIPT_DIR/pacman-queue.py" count 2>&1); then
  echo "gather-pacman-queue: $OUTPUT" >&2
  exit 1
fi

echo "$OUTPUT"
