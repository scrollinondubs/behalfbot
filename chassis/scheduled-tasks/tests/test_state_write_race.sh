#!/bin/zsh
# Regression test for new-jaxity#412 / #409: set_state and increment_fire_count
# used to share a single "${STATE_FILE}.tmp" staging path across every
# concurrent writer. Two heartbeats writing at once could race on that one
# temp file - the second jq to finish clobbers the first's still-being-staged
# output before its mv runs, and the loser's write vanishes with no error.
# Observed live on 2026-07-30: the dispatcher fired pulse-triage twice 4s
# apart, the signature of a lost state write.
#
# This sources heartbeat-dispatcher.sh with DISPATCHER_TEST_SOURCE=1 (see the
# guard at the bottom of that file) to reach set_state/increment_fire_count in
# isolation, points CUSTOMER_HOME at a scratch temp dir, then fires N
# concurrent writers at DISTINCT keys and asserts every single one survived.
# Against the pre-fix shared-tmp-path code this fails intermittently but
# reliably across repeated runs; against the per-writer-tmp-path fix it
# passes every time.
#
# Run from repo root:
#   zsh chassis/scheduled-tasks/tests/test_state_write_race.sh

set -uo pipefail

SCRIPT_DIR="${0:A:h}"
DISPATCHER="${SCRIPT_DIR}/../heartbeat-dispatcher.sh"

TMP_HOME=$(mktemp -d)
cleanup() { rm -rf "$TMP_HOME"; }
trap cleanup EXIT

export CUSTOMER_HOME="$TMP_HOME"
export CHASSIS_HOME="$TMP_HOME"
mkdir -p "$TMP_HOME/scheduled-tasks" "$TMP_HOME/logs/scheduled"

export DISPATCHER_TEST_SOURCE=1
source "$DISPATCHER"

init_state

WRITER_COUNT=20
FAIL=0

# Fire WRITER_COUNT concurrent set_state calls, each at its own key, from
# separate background jobs (separate PIDs) - the shape that raced on the old
# shared "${STATE_FILE}.tmp" path. Distinct keys, same instant, is exactly
# the scenario the issue calls for: two heartbeats never touch each other's
# state entry, so any loss here is purely the temp-file collision, not a
# legitimate same-key conflict.
for i in $(seq 1 $WRITER_COUNT); do
    set_state "writer-$i" "marker" "value-$i" &
done
wait

for i in $(seq 1 $WRITER_COUNT); do
    got=$(get_state "writer-$i" "marker")
    if [[ "$got" != "value-$i" ]]; then
        echo "FAIL: writer-$i lost its write - expected 'value-$i', got '${got:-<empty>}'"
        FAIL=1
    fi
done

# Same race, same assertion, for increment_fire_count.
for i in $(seq 1 $WRITER_COUNT); do
    increment_fire_count "counter-$i" &
done
wait

for i in $(seq 1 $WRITER_COUNT); do
    got=$(jq -r --arg n "counter-$i" '.[$n].fire_count // ""' "$STATE_FILE")
    if [[ "$got" != "1" ]]; then
        echo "FAIL: counter-$i lost its increment - expected fire_count '1', got '${got:-<empty>}'"
        FAIL=1
    fi
done

# No leftover staging files - every "${STATE_FILE}.$$.<key>.tmp" must have
# been mv'd away.
leftover=("$TMP_HOME"/scheduled-tasks/*.tmp(N))
if [[ ${#leftover[@]} -gt 0 ]]; then
    echo "FAIL: leftover tmp files after all writers completed: ${leftover[*]}"
    FAIL=1
fi

if [[ $FAIL -eq 0 ]]; then
    echo "PASS: $((WRITER_COUNT * 2)) concurrent distinct-key writers all survived, no leftover tmp files"
    exit 0
else
    exit 1
fi
