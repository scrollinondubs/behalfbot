#!/bin/bash
# test-gather-local-health.sh - Unit tests for gather-local-health.sh's
# dispatcher-silence check.
#
# The bug these lock down
# =======================
# The silence check read the newest `last_checked` with:
#
#   last_any=$(jq -r '[.[] | .last_checked // empty] | sort | last // empty' \
#              "$state_file" 2>/dev/null || echo "")
#
# heartbeat-state.json's top level is not uniformly objects - a scalar sibling
# key (schema_version, a bare timestamp) makes `.[] | .last_checked` abort with
# "Cannot index number with last_checked". The blanket `2>/dev/null || echo ""`
# swallowed that, `last_any` came back empty, and the `if [[ -n "$last_any" ]]`
# guard skipped the entire silence check. dispatcher_silent_<n>s could never
# fire: the alarm that reports a dead dispatcher was itself dead, silently, on
# every install whose state file carried a scalar key.
#
# Scenario 1 is the regression: a state file with a scalar sibling AND a
# last_checked six years stale must produce dispatcher_silent_<n>s. Against the
# unfixed script it produces count=0.
#
# No network, no daemon - the tests build state files in a temp CHASSIS_HOME.
#
# Exit codes:
#   0 - all scenarios passed
#   1 - one or more scenarios failed
#   2 - test harness itself broke (missing jq)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATHER="${SCRIPT_DIR}/gather-local-health.sh"

if ! command -v jq >/dev/null 2>&1; then
    echo "test-gather-local-health: required dep missing: jq" >&2
    exit 2
fi
if [[ ! -f "$GATHER" ]]; then
    echo "test-gather-local-health: script not found at $GATHER" >&2
    exit 2
fi

fail=0
pass=0

TMPHOME="$(mktemp -d)"
trap 'rm -rf "$TMPHOME"' EXIT
mkdir -p "$TMPHOME/scheduled-tasks"
STATE="$TMPHOME/scheduled-tasks/heartbeat-state.json"

# Run the gather against the current STATE file, capturing stdout only.
# DISK_PCT_THRESHOLD / MEM_MB_FLOOR are pinned out of the way so the host's
# real disk and memory can never inject an unrelated issue tag and make an
# assertion about `issues` pass or fail for the wrong reason.
run_gather() {
    CHASSIS_HOME="$TMPHOME" \
    DISK_PCT_THRESHOLD=100 \
    MEM_MB_FLOOR=0 \
    bash "$GATHER" 2>/dev/null
}

assert_has_issue() {
    local name="$1" prefix="$2" out="$3"
    if echo "$out" | jq -e --arg p "$prefix" \
        '.issues | map(select(startswith($p))) | length > 0' >/dev/null 2>&1; then
        pass=$((pass + 1))
    else
        echo "FAIL [$name] expected an issue starting with '$prefix'"
        echo "-- output --"
        echo "$out"
        fail=$((fail + 1))
    fi
}

assert_no_issues() {
    local name="$1" out="$2"
    local count
    count=$(echo "$out" | jq -r '.count')
    if [[ "$count" == "0" ]]; then
        pass=$((pass + 1))
    else
        echo "FAIL [$name] expected count=0, got $count"
        echo "-- output --"
        echo "$out"
        fail=$((fail + 1))
    fi
}

# ------------------------------------------------------------------------
# Scenario 1 (REGRESSION): scalar sibling key at top level, stale timestamp.
# The scalar is what broke jq. The stale timestamp is what must still be
# found despite it.
# ------------------------------------------------------------------------
cat >"$STATE" <<'EOF'
{
  "schema_version": 2,
  "daily-log": { "last_checked": "2020-01-01T00:00:00" },
  "morning-briefing": { "last_checked": "2020-01-02T00:00:00" }
}
EOF
out=$(run_gather)
assert_has_issue "scalar-sibling-stale" "dispatcher_silent_" "$out"

# ------------------------------------------------------------------------
# Scenario 2: same scalar sibling, but the newest timestamp is current. The
# select() must not swallow live heartbeats into a false alarm.
# ------------------------------------------------------------------------
now_iso=$(date -u +%Y-%m-%dT%H:%M:%S)
cat >"$STATE" <<EOF
{
  "schema_version": 2,
  "daily-log": { "last_checked": "2020-01-01T00:00:00" },
  "morning-briefing": { "last_checked": "$now_iso" }
}
EOF
out=$(run_gather)
assert_no_issues "scalar-sibling-fresh" "$out"

# ------------------------------------------------------------------------
# Scenario 3: objects only, stale. The pre-existing happy path - the fix must
# not have changed it.
# ------------------------------------------------------------------------
cat >"$STATE" <<'EOF'
{
  "daily-log": { "last_checked": "2020-01-01T00:00:00" }
}
EOF
out=$(run_gather)
assert_has_issue "objects-only-stale" "dispatcher_silent_" "$out"

# ------------------------------------------------------------------------
# Scenario 4: unparseable state file. Must report heartbeat_state_unreadable
# rather than degrade into an all-clear, and must still emit the gather JSON
# contract so the dispatcher does not choke.
# ------------------------------------------------------------------------
echo 'not json at all {{{' >"$STATE"
out=$(run_gather)
if ! echo "$out" | jq . >/dev/null 2>&1; then
    echo "FAIL [malformed-state] gather did not emit valid JSON"
    fail=$((fail + 1))
else
    pass=$((pass + 1))
fi
assert_has_issue "malformed-state" "heartbeat_state_unreadable" "$out"

# ------------------------------------------------------------------------
# Scenario 5: top level is a flat scalar. The literal shape named in the jq
# error ("Cannot index string with last_checked").
# ------------------------------------------------------------------------
echo '"just-a-string"' >"$STATE"
out=$(run_gather)
assert_has_issue "flat-scalar-state" "heartbeat_state_unreadable" "$out"

# ------------------------------------------------------------------------
# Scenario 6: no state file at all. Genuinely nothing to check - stays quiet.
# ------------------------------------------------------------------------
rm -f "$STATE"
out=$(run_gather)
assert_no_issues "no-state-file" "$out"

echo
echo "test-gather-local-health: $pass passed, $fail failed"
if [[ $fail -gt 0 ]]; then
    exit 1
fi
exit 0
