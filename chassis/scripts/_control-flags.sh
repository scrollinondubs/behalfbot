#!/usr/bin/env bash
# _control-flags.sh - shared reader for the dispatcher's two control flags.
#
# Sourced by chassis/scheduled-tasks/heartbeat-dispatcher.sh (zsh) and by the
# bash test suite. Written to POSIX-ish shell so both interpreters agree.
#
# Two flags, one schema:
#
#   scheduled-tasks/conservation-mode.json   skip `normal` + `background`
#   scheduled-tasks/halt.json                skip EVERYTHING, `critical` too
#
# Both files carry the same five keys, which predate this file:
#
#   {"enabled": bool, "enabled_at": ts, "enabled_by": str,
#    "auto_lift_after": ts, "reason": str}
#
# Timestamps are "%Y-%m-%dT%H:%M:%SZ", the format
# `scripts/conservation-mode.sh` and `discord-control-listener.py` write.
#
# WHY THIS IS A SEPARATE FILE
# ---------------------------
# The auto-lift arithmetic it holds was wrong on Linux, and the dispatcher is
# not directly testable. The old inline version was:
#
#     lift_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$auto_lift" +%s 2>/dev/null || echo 0)
#     if [[ $now_epoch -ge $lift_epoch ]]; then ... disable ...
#
# `date -j` is BSD only. Inside the chassis container (Debian, GNU coreutils)
# that command always fails, `|| echo 0` yields epoch 0, and `now >= 0` is
# always true - so ANY conservation-mode.json carrying a non-null
# `auto_lift_after` was silently disabled on the very next tick. The throttle
# with an expiry was the one that could not survive one tick. Nothing surfaced
# it: the log line reads "auto-lift triggered", which is what a working
# auto-lift also prints.
#
# The parse here tries BSD, then GNU, then python3, and an UNPARSEABLE
# timestamp now keeps the flag ON rather than lifting it. For a kill switch the
# direction of the failure matters more than the failure: a halt that quietly
# lifts itself because a date string was malformed is the outage this whole
# feature exists to end.

# Detect the date dialect once. GNU date rejects -j outright, so a successful
# probe means BSD.
if date -j -f "%Y-%m-%dT%H:%M:%SZ" "1970-01-01T00:00:00Z" +%s >/dev/null 2>&1; then
    CHASSIS_DATE_DIALECT="bsd"
elif date -u -d "1970-01-01T00:00:00Z" +%s >/dev/null 2>&1; then
    CHASSIS_DATE_DIALECT="gnu"
else
    CHASSIS_DATE_DIALECT="none"
fi

# Every command substitution below is guarded with `|| _cf_x=""`. This file is
# sourced into a `set -euo pipefail` dispatcher, where a bare `x=$(false)`
# takes the whole tick down. Locals are `_cf_`-prefixed for the same reason:
# sourcing must not collide with the caller's variables.

# chassis_parse_iso8601 <timestamp> - print epoch seconds, or fail (return 1).
chassis_parse_iso8601() {
    local _cf_ts="$1" _cf_out=""
    [ -n "$_cf_ts" ] || return 1
    case "$CHASSIS_DATE_DIALECT" in
        bsd) _cf_out=$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$_cf_ts" +%s 2>/dev/null) || _cf_out="" ;;
        gnu) _cf_out=$(date -u -d "$_cf_ts" +%s 2>/dev/null) || _cf_out="" ;;
    esac
    if [ -n "$_cf_out" ]; then
        printf '%s\n' "$_cf_out"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        _cf_out=$(python3 -c '
import sys
from datetime import datetime, timezone
raw = sys.argv[1].strip().replace("Z", "+00:00")
try:
    dt = datetime.fromisoformat(raw)
except ValueError:
    raise SystemExit(1)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
print(int(dt.timestamp()))
' "$_cf_ts" 2>/dev/null) || _cf_out=""
    fi
    [ -n "$_cf_out" ] || return 1
    printf '%s\n' "$_cf_out"
}

# chassis_json_field <file> <.key> - print a scalar field, or fail.
# jq is the house tool; python3 covers an image where it is missing, so one
# broken parser cannot on its own make a flag unreadable.
chassis_json_field() {
    local _cf_file="$1" _cf_key="$2"
    if command -v jq >/dev/null 2>&1; then
        jq -r "$_cf_key // \"\"" "$_cf_file" 2>/dev/null && return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 -c '
import json, sys
key = sys.argv[2].lstrip(".")
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(1)
value = data.get(key)
if value is None or value is False:
    print("")
elif value is True:
    print("true")
else:
    print(value)
' "$_cf_file" "$_cf_key" 2>/dev/null && return 0
    fi
    return 1
}

# chassis_control_flag_lift <file> - write the five-key "off" document.
chassis_control_flag_lift() {
    cat > "$1" << 'EOJSON'
{
  "enabled": false,
  "enabled_at": null,
  "enabled_by": null,
  "auto_lift_after": null,
  "reason": null
}
EOJSON
}

# chassis_control_flag_state <file> - print "on" or "off".
#
# Side effect: when the flag is on and `auto_lift_after` has passed, the file
# is rewritten to the "off" document and "off" is printed. That write is how
# the flag self-clears; the caller logs it.
chassis_control_flag_state() {
    local _cf_flag="$1" _cf_enabled="" _cf_lift="" _cf_lift_epoch="" _cf_now=""

    if [ ! -f "$_cf_flag" ]; then
        printf 'off\n'
        return 0
    fi

    _cf_enabled=$(chassis_json_field "$_cf_flag" ".enabled") || {
        # The file exists and could not be read. Assume on: an unreadable halt
        # flag must not be treated as permission to spend.
        printf 'on\n'
        return 0
    }
    if [ "$_cf_enabled" != "true" ]; then
        printf 'off\n'
        return 0
    fi

    _cf_lift=$(chassis_json_field "$_cf_flag" ".auto_lift_after") || _cf_lift=""
    if [ -z "$_cf_lift" ] || [ "$_cf_lift" = "null" ]; then
        printf 'on\n'
        return 0
    fi

    _cf_lift_epoch=$(chassis_parse_iso8601 "$_cf_lift") || {
        # Unparseable expiry - stay on. See the header.
        printf 'on\n'
        return 0
    }
    _cf_now=$(date +%s)
    if [ "$_cf_now" -ge "$_cf_lift_epoch" ]; then
        chassis_control_flag_lift "$_cf_flag"
        printf 'off\n'
        return 0
    fi
    printf 'on\n'
}
