#!/usr/bin/env bash
# test-control-flags.sh - behavioural tests for _control-flags.sh.
#
# The library exists because the auto-lift arithmetic it replaces was WRONG on
# Linux and could not be seen from the outside. The old inline version in
# heartbeat-dispatcher.sh was:
#
#     lift_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$auto_lift" +%s 2>/dev/null || echo 0)
#     [[ $now_epoch -ge $lift_epoch ]] && disable
#
# `date -j` is BSD only, so inside the chassis container (GNU coreutils) every
# `auto_lift_after` parsed as epoch 0 and the flag lifted on the very next
# tick. The log line for that path reads "auto-lift triggered", exactly what a
# WORKING auto-lift prints, so four weeks of it would look healthy.
#
# So the suite runs the whole matrix three times, once per date dialect, with
# `date` stubbed on PATH to be GNU-only, BSD-only, or neither. Case 3 (GNU
# only, future expiry) is the discriminating one: it passes here and fails
# against the pre-fix idiom, which the suite asserts directly at the end.
#
# Exit 0 all pass, 1 on failure, 2 on harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="${SCRIPT_DIR}/_control-flags.sh"

if [[ ! -f "$LIB" ]]; then
    echo "test-control-flags: library not found at $LIB" >&2
    exit 2
fi

pass=0
fail=0

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
STUB_BIN="$TMP/bin"
mkdir -p "$STUB_BIN"
REAL_DATE="$(command -v date)"
ORIGINAL_PATH="$PATH"

# A `date` stub that answers as one dialect only. Everything it does not
# intercept is forwarded to the real binary, so `date +%s` keeps working.
cat > "$STUB_BIN/date" <<'STUB'
#!/bin/bash
# `date` that answers as exactly one dialect. Both parsing dialects are
# EMULATED with python rather than forwarded to the real binary: the suite has
# to be able to force the BSD answer on a GNU-only CI runner and the GNU answer
# on a Mac, which passthrough cannot do. Everything else (notably `date +%s`)
# forwards to the real binary untouched.
real="${STUB_REAL_DATE:?}"
dialect="${STUB_DIALECT:?}"

emulate_parse() {
    # $1 = strftime format ("" means ISO-ish free parse), $2 = timestamp
    python3 -c '
import sys
from datetime import datetime, timezone
fmt, raw = sys.argv[1], sys.argv[2].strip()
try:
    if fmt:
        dt = datetime.strptime(raw, fmt)
    else:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
except ValueError:
    raise SystemExit(1)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
print(int(dt.timestamp()))
' "$1" "$2"
}

case "${1:-}" in
    -j)
        # BSD: date -j [-u] -f <fmt> <timestamp> +%s
        [[ "$dialect" == "bsd" ]] || { echo "date: illegal option -- j" >&2; exit 1; }
        fmt=""; ts=""; want_fmt=0
        for arg in "$@"; do
            if [[ "$want_fmt" -eq 1 ]]; then fmt="$arg"; want_fmt=0; continue; fi
            case "$arg" in
                -f) want_fmt=1 ;;
                -j|-u) ;;
                +*) ;;
                *) ts="$arg" ;;
            esac
        done
        emulate_parse "$fmt" "$ts"
        exit $?
        ;;
    -u)
        if [[ "${2:-}" == "-d" ]]; then
            # GNU: date -u -d <timestamp> +%s
            [[ "$dialect" == "gnu" ]] || { echo "date: illegal option -- d" >&2; exit 1; }
            emulate_parse "" "$3"
            exit $?
        fi
        exec "$real" "$@"
        ;;
esac
exec "$real" "$@"
STUB
chmod +x "$STUB_BIN/date"
export STUB_REAL_DATE="$REAL_DATE"

check() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        printf '  FAIL %s: expected %s, got %s\n' "$label" "$expected" "$actual" >&2
    fi
}

write_flag() {
    printf '%s\n' "$2" > "$TMP/$1.json"
}

past="2020-01-01T00:00:00Z"
future="$("$REAL_DATE" -u -v+2d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || "$REAL_DATE" -u -d '+2 days' +%Y-%m-%dT%H:%M:%SZ)"

run_matrix() {
    local dialect="$1"

    # Re-source with the stub in front so the dialect probe re-runs.
    export STUB_DIALECT="$dialect"
    PATH="$STUB_BIN:$ORIGINAL_PATH"
    # shellcheck disable=SC1090
    source "$LIB"
    PATH="$ORIGINAL_PATH"

    local expect_dialect="$dialect"
    [[ "$dialect" == "none" ]] && expect_dialect="none"
    check "[$dialect] dialect probe" "$expect_dialect" "$CHASSIS_DATE_DIALECT"

    write_flag on '{"enabled": true, "auto_lift_after": null, "reason": "manual"}'
    write_flag off '{"enabled": false, "auto_lift_after": null}'
    write_flag expired "{\"enabled\": true, \"auto_lift_after\": \"$past\"}"
    write_flag future "{\"enabled\": true, \"auto_lift_after\": \"$future\"}"
    write_flag garbage '{"enabled": true, "auto_lift_after": "next tuesday"}'
    printf '%s' '{not json' > "$TMP/corrupt.json"

    PATH="$STUB_BIN:$ORIGINAL_PATH"
    check "[$dialect] enabled, no expiry"      on  "$(chassis_control_flag_state "$TMP/on.json")"
    check "[$dialect] disabled"                off "$(chassis_control_flag_state "$TMP/off.json")"
    check "[$dialect] absent file"             off "$(chassis_control_flag_state "$TMP/nope.json")"
    # THE case the old code got wrong on Linux.
    check "[$dialect] expiry in the future"    on  "$(chassis_control_flag_state "$TMP/future.json")"
    check "[$dialect] expiry in the past"      off "$(chassis_control_flag_state "$TMP/expired.json")"
    # Fail closed: an unparseable expiry keeps the flag on. A halt that lifts
    # itself because of a malformed timestamp is the outage this exists to end.
    check "[$dialect] unparseable expiry"      on  "$(chassis_control_flag_state "$TMP/garbage.json")"
    check "[$dialect] unreadable file"         on  "$(chassis_control_flag_state "$TMP/corrupt.json")"
    PATH="$ORIGINAL_PATH"

    # The lift is a real write, not just a return value.
    local lifted
    lifted="$(tr -d ' \n' < "$TMP/expired.json")"
    check "[$dialect] lift rewrites the file" \
        '{"enabled":false,"enabled_at":null,"enabled_by":null,"auto_lift_after":null,"reason":null}' \
        "$lifted"
}

for dialect in bsd gnu none; do
    run_matrix "$dialect"
done

# Discrimination: the pre-fix idiom, run against the same fixture, must get the
# future expiry WRONG under GNU date. If this ever starts passing, the suite is
# no longer proving anything about the bug it was written for.
export STUB_DIALECT="gnu"
PATH="$STUB_BIN:$ORIGINAL_PATH"
old_lift_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$future" +%s 2>/dev/null || echo 0)  # portable-ok: the pre-fix BSD-only idiom on purpose, asserted below to misparse under GNU date
old_now=$(date +%s)
PATH="$ORIGINAL_PATH"
if [[ "$old_now" -ge "$old_lift_epoch" ]]; then
    pass=$((pass + 1))
else
    fail=$((fail + 1))
    echo "  FAIL discrimination: the pre-fix idiom no longer misparses under GNU date" >&2
fi

# Sourcing under `set -e` must not kill the caller. The dispatcher runs
# `set -euo pipefail`, and a bare `x=$(false)` inside the library would take
# the whole tick down - which is how a kill switch becomes an outage.
export STUB_DIALECT="none"
if PATH="$STUB_BIN:$ORIGINAL_PATH" bash -c "
set -euo pipefail
source '$LIB'
chassis_control_flag_state '$TMP/garbage.json' >/dev/null
chassis_control_flag_state '$TMP/nope.json' >/dev/null
chassis_parse_iso8601 'not-a-date' >/dev/null 2>&1 || true
echo survived
" | grep -q survived; then
    pass=$((pass + 1))
else
    fail=$((fail + 1))
    echo "  FAIL: library does not survive being sourced under set -euo pipefail" >&2
fi

# Same, under zsh - the dispatcher's actual interpreter.
if command -v zsh >/dev/null 2>&1; then
    if PATH="$STUB_BIN:$ORIGINAL_PATH" zsh -c "
set -euo pipefail
source '$LIB'
chassis_control_flag_state '$TMP/garbage.json' >/dev/null
echo survived
" | grep -q survived; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        echo "  FAIL: library does not survive zsh set -euo pipefail" >&2
    fi
fi

printf '\ntest-control-flags: %d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]] || exit 1
