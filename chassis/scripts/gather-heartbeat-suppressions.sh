#!/usr/bin/env bash
# gather-heartbeat-suppressions.sh - what the dispatcher is currently NOT
# telling you, in JSON, for free.
#
# The heartbeat dispatcher can decline to invoke the model for a heartbeat
# whose condition is true: a daily cap has been hit (`max_fires_per_day`,
# `max_usd_per_day`) or the identical alert already went out inside the
# heartbeat's `realert_after` window. Both are deliberate. Both are also a
# problem that is still there and has stopped saying so.
#
# Every suppression is recorded in
# `$CUSTOMER_HOME/scheduled-tasks/heartbeat-suppressions.json`, one updatable
# record per heartbeat, cleared the moment the condition clears. This script
# renders that ledger. It costs a file read and a jq pass, so a weekly rollup
# can call it directly and render "here is what has been failing silently all
# week" with no model call in the loop.
#
# Usage:
#   gather-heartbeat-suppressions.sh                 # JSON on stdout
#   gather-heartbeat-suppressions.sh --min-hours 24  # only entries quiet >=24h
#
# Environment:
#   CUSTOMER_HOME  install root (falls back to CHASSIS_HOME, then $HOME)
#
# Output:
#   {
#     "count": 2,                       # entries after filtering
#     "generated_at": "2026-09-05T14:02:11",
#     "ledger_path": "/app/customer/scheduled-tasks/heartbeat-suppressions.json",
#     "suppressed": [
#       {
#         "heartbeat": "repo-drift",
#         "reason": "fire_cap",         # fire_cap | usd_cap | dedup
#         "detail": "max_fires_per_day reached: 3 of 3 ...",
#         "fingerprint": "9f2c...",
#         "first_suppressed_at": "2026-09-01T00:14:11",
#         "last_suppressed_at": "2026-09-05T13:44:02",
#         "suppressed_ticks": 471,
#         "quiet_hours": 133             # derived at read time, see note
#       }
#     ]
#   }
#
# `quiet_hours` is computed here, at read time, and is deliberately NOT part of
# the ledger record or of any fingerprint: a duration that grows every tick is
# exactly the value that must never reach a content hash. It is a reporting
# field only.
#
# Usable as a heartbeat gather (`condition: threshold count > 0`) as well as
# from a rollup prompt. It ALWAYS exits 0 and carries its verdict in `count` -
# a gather that exits non-zero is read by the dispatcher as count=0, so an
# erroring monitor would go silent exactly when it has something to say.
#
# Issue: new-jaxity#550.

set -uo pipefail

MIN_HOURS=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --min-hours)
            MIN_HOURS="${2:-0}"
            shift 2
            ;;
        --help|-h)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "unknown flag: $1" >&2
            exit 2
            ;;
    esac
done

if [[ ! "$MIN_HOURS" =~ ^[0-9]+$ ]]; then
    MIN_HOURS=0
fi

ROOT="${CUSTOMER_HOME:-${CHASSIS_HOME:-$HOME}}"
LEDGER="$ROOT/scheduled-tasks/heartbeat-suppressions.json"
NOW_EPOCH=$(date +%s)
NOW_ISO=$(date +%Y-%m-%dT%H:%M:%S)

emit_empty() {
    printf '{"count":0,"generated_at":"%s","ledger_path":"%s","suppressed":[]}\n' \
        "$NOW_ISO" "$LEDGER"
    exit 0
}

command -v jq >/dev/null 2>&1 || emit_empty
[[ -f "$LEDGER" ]] || emit_empty

jq -c \
    --arg now_iso "$NOW_ISO" \
    --argjson now "$NOW_EPOCH" \
    --argjson min_hours "$MIN_HOURS" \
    --arg path "$LEDGER" '
    [ to_entries[]
      | .value as $v
      | {
          heartbeat: .key,
          reason: ($v.reason // "unknown"),
          detail: ($v.detail // ""),
          fingerprint: ($v.fingerprint // ""),
          first_suppressed_at: ($v.first_suppressed_at // ""),
          last_suppressed_at: ($v.last_suppressed_at // ""),
          suppressed_ticks: ($v.suppressed_ticks // 0),
          quiet_hours: (
              (($v.first_suppressed_epoch // "0") | tonumber) as $first
              | if $first > 0 then (($now - $first) / 3600 | floor) else 0 end
          )
        }
    ]
    | map(select(.quiet_hours >= $min_hours))
    | sort_by(-.quiet_hours)
    | {count: length, generated_at: $now_iso, ledger_path: $path, suppressed: .}
' "$LEDGER" 2>/dev/null || emit_empty
