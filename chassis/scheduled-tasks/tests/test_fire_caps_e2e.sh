#!/bin/zsh
# End-to-end coverage for new-jaxity#550: run the real dispatcher, as a child
# process, over an unresolvable condition, and prove it stops paying for it.
#
# test_fire_caps.sh exercises the new functions in isolation. That is not
# enough on its own: the incident was not a broken function, it was a loop with
# no ceiling in it, and a unit test of a ceiling that is never consulted passes
# just as happily. So this suite stubs `claude` on PATH and drives six full
# dispatcher ticks against three heartbeats whose gather always reports work:
#
#   capped      max_fires_per_day: 2   - must fire twice, then stop
#   capped-usd  max_usd_per_day: 0.20  - must stop once telemetry says it has
#                                        spent that much, at $0.0891 a run
#   deduped     realert_after: 24h     - must fire once, then stay quiet while
#                                        the gather's age counter climbs
#   uncapped    no new keys            - must fire on every single tick
#
# The third is the backwards-compatibility contract, checked the only way that
# means anything: by running it.
#
# Run from repo root:
#   zsh chassis/scheduled-tasks/tests/test_fire_caps_e2e.sh

set -uo pipefail

SCRIPT_DIR="${0:A:h}"
DISPATCHER="${SCRIPT_DIR}/../heartbeat-dispatcher.sh"
LEDGER_GATHER="${SCRIPT_DIR}/../../scripts/gather-heartbeat-suppressions.sh"

TMP_HOME=$(mktemp -d)
cleanup() { rm -r -f "$TMP_HOME"; }
trap cleanup EXIT

mkdir -p "$TMP_HOME/scheduled-tasks" "$TMP_HOME/logs/scheduled" \
         "$TMP_HOME/logs/telemetry" "$TMP_HOME/bin" "$TMP_HOME/prompts"

# --- Stub claude. Emits the JSON shape invoke_claude parses, at a cost that
# --- matches what repo-drift actually billed per run.
cat > "$TMP_HOME/bin/claude" <<'STUB'
#!/bin/sh
cat > /dev/null
# printf with a single-quoted argument, not echo: /bin/sh's echo expands the
# \n inside the JSON string and hands the dispatcher unparseable telemetry.
printf '%s\n' '{"result":"notify: true\nsummary: still dirty\n","cost_usd":0.0891,"usage":{"input_tokens":33,"output_tokens":1944}}'
STUB
chmod +x "$TMP_HOME/bin/claude"

# --- A gather that can never clear, with an age that climbs every tick. This
# --- is repo-drift: the tree is dirty, auto-heal correctly refuses to touch a
# --- dirty tree, and only a human can resolve it.
cat > "$TMP_HOME/scheduled-tasks/gather-stuck.sh" <<'GATHER'
#!/bin/sh
TICK_FILE="${CUSTOMER_HOME}/scheduled-tasks/.tick"
n=$(cat "$TICK_FILE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$TICK_FILE"
printf '{"count":1,"needs_sean":[{"repo":".behalfbot","reason":"uncommitted_work","condition_age_hours":%s}],"errors":[]}\n' "$((n * 96))"
GATHER
chmod +x "$TMP_HOME/scheduled-tasks/gather-stuck.sh"

echo "Report the drift." > "$TMP_HOME/prompts/stuck.md"

cat > "$TMP_HOME/HEARTBEATS.md" <<'HBEOF'
## capped

```yaml
schedule: every 0m
gather: scheduled-tasks/gather-stuck.sh
condition: threshold count > 0
prompt: prompts/stuck.md
model: haiku
budget: 0.10
criticality: normal
max_fires_per_day: 2
```

## capped-usd

```yaml
schedule: every 0m
gather: scheduled-tasks/gather-stuck.sh
condition: threshold count > 0
prompt: prompts/stuck.md
model: haiku
budget: 0.10
criticality: normal
max_usd_per_day: 0.20
```

## deduped

```yaml
schedule: every 0m
gather: scheduled-tasks/gather-stuck.sh
condition: threshold count > 0
prompt: prompts/stuck.md
model: haiku
budget: 0.10
criticality: normal
realert_after: 24h
```

## uncapped

```yaml
schedule: every 0m
gather: scheduled-tasks/gather-stuck.sh
condition: threshold count > 0
prompt: prompts/stuck.md
model: haiku
budget: 0.10
criticality: normal
```
HBEOF

FAIL=0
CASES=0
ok() { CASES=$((CASES + 1)); }
fail() { echo "FAIL: $1"; FAIL=1; CASES=$((CASES + 1)); }
assert_eq() {
    if [[ "$1" == "$2" ]]; then ok; else fail "$3 (expected '$2', got '$1')"; fi
}

TICKS=6
i=1
while [[ $i -le $TICKS ]]; do
    PATH="$TMP_HOME/bin:$PATH" \
    CHASSIS_HOME="$TMP_HOME" CUSTOMER_HOME="$TMP_HOME" \
        zsh "$DISPATCHER"
    i=$((i + 1))
done

DATE_TODAY=$(date +%Y-%m-%d)
LOG="$TMP_HOME/logs/scheduled/${DATE_TODAY}-dispatcher.log"
TELEMETRY="$TMP_HOME/logs/telemetry/${DATE_TODAY}-usage.jsonl"

if [[ ! -f "$LOG" ]]; then
    echo "FAIL: no dispatcher log at $LOG - the run never happened"
    exit 1
fi

count_lines() { grep -c "$1" "$LOG" || true; }
telemetry_runs() {
    if [[ -f "$TELEMETRY" ]]; then
        jq -R -s --arg n "$1" '[split("\n")[] | select(length>0) | (fromjson? // empty) | select(.heartbeat == $n)] | length' "$TELEMETRY"
    else
        echo 0
    fi
}

# --- capped: two fires, then nothing, for the rest of the day.
assert_eq "$(telemetry_runs capped)" "2" \
    "max_fires_per_day: 2 must produce exactly 2 model invocations across $TICKS ticks"
assert_eq "$(count_lines 'CAPPED capped -')" "4" \
    "every tick after the cap must log the suppression rather than vanish"

# The notice fires once per capped heartbeat per day. With no ops webhook
# configured, alert_ops logs the message it would have posted, which is what we
# count. Two heartbeats cap here, so two notices across 6 ticks - not the 10
# suppressed ticks, and not the 50+ alerts of the incident.
assert_eq "$(grep -c 'alert dropped: 🔇' "$LOG" || true)" "2" \
    "a tripped cap must post exactly one notice per heartbeat per day, not one per tick"

# --- capped-usd: the ceiling is read from the telemetry the dispatcher writes,
# --- so it counts what was actually billed rather than a tally of our own.
# --- $0.0891 a run: two runs is $0.1782 and still under, three is $0.2673.
assert_eq "$(telemetry_runs capped-usd)" "3" \
    "max_usd_per_day: 0.20 must stop the heartbeat on the tick after telemetry crosses it"
assert_eq "$(count_lines 'CAPPED capped-usd -')" "3" \
    "the dollar cap must log every suppressed tick too"

# --- deduped: one fire, then quiet, while the age counter climbs every tick.
assert_eq "$(telemetry_runs deduped)" "1" \
    "an unchanged condition must alert once inside realert_after, however much its age field moves"
assert_eq "$(count_lines 'DEDUPED deduped')" "5" \
    "every deduped tick must be logged"

# --- uncapped: unchanged behaviour, which is the backwards-compat contract.
assert_eq "$(telemetry_runs uncapped)" "$TICKS" \
    "a heartbeat with none of the new keys must fire on every tick exactly as before"
assert_eq "$(count_lines 'CAPPED uncapped')" "0" "an uncapped heartbeat is never capped"
assert_eq "$(count_lines 'DEDUPED uncapped')" "0" "a heartbeat without realert_after is never deduped"

# --- The spend that started all this.
total=$(jq -R -s '[split("\n")[] | select(length>0) | (fromjson? // empty) | .cost_usd // 0] | add' "$TELEMETRY")
would_have_been=$(awk -v t="$TICKS" 'BEGIN { printf "%.4f", t * 4 * 0.0891 }')
actual_runs=$(jq -R -s '[split("\n")[] | select(length>0)] | length' "$TELEMETRY")
assert_eq "$actual_runs" "$((TICKS + 6))" \
    "4 heartbeats x $TICKS ticks is $((TICKS * 4)) runs uncapped; caps and dedup must bring it to $((TICKS + 6))"
echo "  spend: \$$total across $actual_runs runs (uncapped would have been \$$would_have_been across $((TICKS * 4)))"

# --- The ledger, and the weekly-rollup read path.
LEDGER="$TMP_HOME/scheduled-tasks/heartbeat-suppressions.json"
if [[ -f "$LEDGER" ]]; then ok; else fail "no suppression ledger was written"; fi

report=$(CUSTOMER_HOME="$TMP_HOME" bash "$LEDGER_GATHER")
assert_eq "$(echo "$report" | jq -r '.count')" "3" \
    "the rollup must see every silenced heartbeat and none of the healthy ones"
assert_eq "$(echo "$report" | jq -r '.suppressed[] | select(.heartbeat=="capped-usd") | .reason')" "usd_cap" \
    "the ledger distinguishes a dollar cap from a fire-count cap"
assert_eq "$(echo "$report" | jq -r '.suppressed[] | select(.heartbeat=="capped") | .reason')" "fire_cap" \
    "the ledger names why capped went quiet"
assert_eq "$(echo "$report" | jq -r '.suppressed[] | select(.heartbeat=="deduped") | .reason')" "dedup" \
    "the ledger names why deduped went quiet"
assert_eq "$(echo "$report" | jq -r '.suppressed[] | select(.heartbeat=="capped") | .suppressed_ticks')" "4" \
    "the ledger counts the suppressed ticks"
if echo "$report" | jq -e '.suppressed[] | select(.heartbeat=="uncapped")' >/dev/null 2>&1; then
    fail "a heartbeat that is still alerting must not appear in the ledger"
else
    ok
fi

# --- A resolved condition must leave the ledger, so "quiet because it was
# --- fixed" is distinguishable from "quiet because it was muted".
cat > "$TMP_HOME/scheduled-tasks/gather-stuck.sh" <<'CLEAN'
#!/bin/sh
echo '{"count":0,"needs_sean":[],"errors":[]}'
CLEAN
chmod +x "$TMP_HOME/scheduled-tasks/gather-stuck.sh"

PATH="$TMP_HOME/bin:$PATH" \
CHASSIS_HOME="$TMP_HOME" CUSTOMER_HOME="$TMP_HOME" \
    zsh "$DISPATCHER"

report=$(CUSTOMER_HOME="$TMP_HOME" bash "$LEDGER_GATHER")
assert_eq "$(echo "$report" | jq -r '.count')" "0" \
    "once the condition clears, the ledger empties without anyone clearing it by hand"
assert_eq "$(jq -r '.deduped.dedupe_fingerprint' "$TMP_HOME/scheduled-tasks/heartbeat-state.json")" "" \
    "a cleared condition drops the dedup identity, so the same problem recurring next month is news again"

if [[ $FAIL -eq 0 ]]; then
    echo "PASS: $CASES assertions"
    exit 0
else
    echo "FAILED ($CASES assertions run)"
    exit 1
fi
