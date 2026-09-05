#!/bin/zsh
# Behavioural coverage for the daily fire caps, alert dedup and suppression
# ledger added for new-jaxity#550.
#
# The incident: `repo-drift` fired 514 times over five days for $45.79 (66% of
# all scheduled spend) and posted 50+ near-identical alerts, because its
# condition - a dirty working tree - could not clear without a human, and
# `budget:` is a per-invocation cap rather than a daily one.
#
# The load-bearing assertion in this file is fingerprint stability. The real
# repo-drift gather emits `condition_age_hours`, `dirty_oldest_age_hours` and
# `ahead_newest_age_hours`, and its alerts read "unresolved for 4 days", then
# "5 days", then "131h", then "180 hours". A fingerprint computed over the raw
# gather output, or over the rendered alert prose, never matches itself, so the
# cooldown silently never fires and the feature looks installed while doing
# nothing. Every dedup case below feeds two payloads that differ ONLY in those
# volatile counters and asserts one fingerprint - with a control assertion that
# a naive hash of the same two payloads does differ, so the test proves it
# discriminates rather than merely passing.
#
# Sources heartbeat-dispatcher.sh with DISPATCHER_TEST_SOURCE=1 (see the guard
# at the bottom of that file) to reach the functions in isolation, against a
# scratch CUSTOMER_HOME.
#
# Run from repo root:
#   zsh chassis/scheduled-tasks/tests/test_fire_caps.sh

set -uo pipefail

SCRIPT_DIR="${0:A:h}"
DISPATCHER="${SCRIPT_DIR}/../heartbeat-dispatcher.sh"
LEDGER_GATHER="${SCRIPT_DIR}/../../scripts/gather-heartbeat-suppressions.sh"

TMP_HOME=$(mktemp -d)
cleanup() { rm -r -f "$TMP_HOME"; }
trap cleanup EXIT

export CUSTOMER_HOME="$TMP_HOME"
export CHASSIS_HOME="$TMP_HOME"
mkdir -p "$TMP_HOME/scheduled-tasks" "$TMP_HOME/logs/scheduled" "$TMP_HOME/logs/telemetry"

# The dispatcher reads HEARTBEATS.md at $CUSTOMER_HOME, and resolves the path
# at source time, so it has to exist before the source below.
cat > "$TMP_HOME/HEARTBEATS.md" <<'HBEOF'
## drift-default

```yaml
schedule: every 30m
gather: scripts/gather-drift.sh
condition: threshold count > 0
prompt: prompts/drift.md
model: haiku
budget: 0.10
criticality: normal
realert_after: 24h
```

## drift-keyed

```yaml
schedule: every 30m
gather: scripts/gather-drift.sh
condition: threshold count > 0
prompt: prompts/drift.md
model: haiku
budget: 0.10
criticality: normal
realert_after: 24h
dedupe_key: .needs_sean | map({repo, reason})
```

## asks-the-model

```yaml
schedule: every 30m
gather: scripts/gather-drift.sh
condition: ask_model
prompt: prompts/drift.md
model: haiku
budget: 0.10
criticality: normal
realert_after: 24h
```

## bad-window

```yaml
schedule: every 30m
gather: scripts/gather-drift.sh
condition: threshold count > 0
prompt: prompts/drift.md
model: haiku
budget: 0.10
criticality: normal
realert_after: soonish
```

## capped-fires

```yaml
schedule: every 30m
gather: scripts/gather-drift.sh
condition: threshold count > 0
prompt: prompts/drift.md
model: haiku
budget: 0.10
criticality: normal
max_fires_per_day: 3
```

## capped-usd

```yaml
schedule: every 30m
gather: scripts/gather-drift.sh
condition: threshold count > 0
prompt: prompts/drift.md
model: haiku
budget: 0.10
criticality: normal
max_usd_per_day: 0.50
```

## bad-cap

```yaml
schedule: every 30m
gather: scripts/gather-drift.sh
condition: threshold count > 0
prompt: prompts/drift.md
model: haiku
budget: 0.10
criticality: normal
max_fires_per_day: lots
max_usd_per_day: cheap
```

## legacy

```yaml
schedule: every 30m
gather: scripts/gather-drift.sh
condition: threshold count > 0
prompt: prompts/drift.md
model: haiku
budget: 0.10
criticality: normal
```
HBEOF

export DISPATCHER_TEST_SOURCE=1
source "$DISPATCHER"
# The dispatcher sets `set -euo pipefail` for the shell that sources it. This
# suite deliberately calls predicates that return non-zero, so -e comes back
# off here; every real invocation still runs with it on.
set +e

init_state

FAIL=0
CASES=0

ok() {
    CASES=$((CASES + 1))
}

fail() {
    echo "FAIL: $1"
    FAIL=1
    CASES=$((CASES + 1))
}

assert_eq() {
    if [[ "$1" == "$2" ]]; then ok; else fail "$3 (expected '$2', got '$1')"; fi
}

assert_ne() {
    if [[ "$1" != "$2" ]]; then ok; else fail "$3 (both were '$1')"; fi
}

# ---------------------------------------------------------------------------
# Payloads: the real repo-drift gather shape, from
# scripts/gather-repo-drift.sh's documented contract.
# ---------------------------------------------------------------------------

# Four days into the condition.
DRIFT_4D='{"status":"ok","count":1,"auto_fixed":[],"needs_sean":[{"repo":".behalfbot","branch":"main","behind":0,"ahead":0,"dirty_files":5,"untracked":0,"dirty_oldest_age_hours":96,"ahead_newest_age_hours":-1,"condition_age_hours":96,"reason":"uncommitted_work","sample_drift":"HEARTBEATS.md"}],"errors":[],"skipped":[]}'

# Same five files, same repo, same reason. Only the durations moved - this is
# the payload three days later, and it is the pair the whole feature hinges on.
DRIFT_7D='{"status":"ok","count":1,"auto_fixed":[],"needs_sean":[{"repo":".behalfbot","branch":"main","behind":0,"ahead":0,"dirty_files":5,"untracked":0,"dirty_oldest_age_hours":180,"ahead_newest_age_hours":-1,"condition_age_hours":180,"reason":"uncommitted_work","sample_drift":"HEARTBEATS.md"}],"errors":[],"skipped":[]}'

# A genuinely different situation: two repos need attention, not one.
DRIFT_TWO='{"status":"ok","count":2,"auto_fixed":[],"needs_sean":[{"repo":".behalfbot","branch":"main","behind":0,"ahead":0,"dirty_files":5,"untracked":0,"dirty_oldest_age_hours":96,"ahead_newest_age_hours":-1,"condition_age_hours":96,"reason":"uncommitted_work","sample_drift":"HEARTBEATS.md"},{"repo":"vibecodelisboa","branch":"main","behind":9,"ahead":0,"dirty_files":0,"untracked":0,"dirty_oldest_age_hours":-1,"ahead_newest_age_hours":-1,"condition_age_hours":30,"reason":"behind","sample_drift":"scripts/deploy.sh"}],"errors":[],"skipped":[]}'

# Same count as DRIFT_4D, different repo in trouble. Distinguishes the default
# threshold identity from an explicit dedupe_key.
DRIFT_OTHER_REPO='{"status":"ok","count":1,"auto_fixed":[],"needs_sean":[{"repo":"vibecodelisboa","branch":"main","behind":9,"ahead":0,"dirty_files":0,"untracked":0,"dirty_oldest_age_hours":-1,"ahead_newest_age_hours":-1,"condition_age_hours":30,"reason":"behind","sample_drift":"scripts/deploy.sh"}],"errors":[],"skipped":[]}'

# ---------------------------------------------------------------------------
# 1. Control: the naive thing really does churn.
# ---------------------------------------------------------------------------
naive_a=$(printf '%s' "$DRIFT_4D" | _sha256_stdin)
naive_b=$(printf '%s' "$DRIFT_7D" | _sha256_stdin)
assert_ne "$naive_a" "$naive_b" \
    "control: a raw hash of the gather output should differ across the age fields (if it does not, the stability assertions below prove nothing)"

# ---------------------------------------------------------------------------
# 2. Fingerprint stability across a changing duration field.
# ---------------------------------------------------------------------------
fp_a=$(heartbeat_fingerprint "drift-default" "threshold count > 0" "$DRIFT_4D")
fp_b=$(heartbeat_fingerprint "drift-default" "threshold count > 0" "$DRIFT_7D")
assert_eq "$fp_a" "$fp_b" "default (threshold) fingerprint must not move when only the age counters move"
if [[ -n "$fp_a" ]]; then ok; else fail "default fingerprint came back empty"; fi

kp_a=$(heartbeat_fingerprint "drift-keyed" "threshold count > 0" "$DRIFT_4D")
kp_b=$(heartbeat_fingerprint "drift-keyed" "threshold count > 0" "$DRIFT_7D")
assert_eq "$kp_a" "$kp_b" "dedupe_key fingerprint must not move when only the age counters move"
assert_ne "$kp_a" "$fp_a" "dedupe_key and threshold identities are different projections and should not collide"

# Key order in the payload must not matter either.
DRIFT_4D_REORDERED=$(printf '%s' "$DRIFT_4D" | jq -c 'to_entries | sort_by(.key) | from_entries')
kp_reordered=$(heartbeat_fingerprint "drift-keyed" "threshold count > 0" "$DRIFT_4D_REORDERED")
assert_eq "$kp_reordered" "$kp_a" "dedupe_key fingerprint must survive a re-ordering of the gather payload's keys"

# ---------------------------------------------------------------------------
# 3. It still discriminates.
# ---------------------------------------------------------------------------
fp_two=$(heartbeat_fingerprint "drift-default" "threshold count > 0" "$DRIFT_TWO")
assert_ne "$fp_two" "$fp_a" "threshold fingerprint must change when the tested count changes"

kp_other=$(heartbeat_fingerprint "drift-keyed" "threshold count > 0" "$DRIFT_OTHER_REPO")
assert_ne "$kp_other" "$kp_a" "dedupe_key fingerprint must change when a different repo is the problem"

# ...and the documented coarseness of the default is real, not accidental: same
# count, different repo, same identity. This is why dedupe_key exists.
fp_other=$(heartbeat_fingerprint "drift-default" "threshold count > 0" "$DRIFT_OTHER_REPO")
assert_eq "$fp_other" "$fp_a" "documented tradeoff: the default identity is the tested count, so a different problem with the same count reads as the same condition"

# ---------------------------------------------------------------------------
# 4. No identity is refused, never guessed.
# ---------------------------------------------------------------------------
if heartbeat_fingerprint "asks-the-model" "ask_model" "$DRIFT_4D" >/dev/null; then
    fail "ask_model with no dedupe_key must not produce a fingerprint"
else
    ok
fi

if heartbeat_fingerprint "drift-default" "threshold count > 0" "not json at all" >/dev/null; then
    fail "a non-JSON gather payload must not produce a threshold fingerprint"
else
    ok
fi

# ---------------------------------------------------------------------------
# 5. dedupe_check: opt-in, window, expiry.
# ---------------------------------------------------------------------------

# Backwards compatibility: a heartbeat with none of the new keys is never
# deduped, whatever is sitting in its state.
set_state "legacy" "dedupe_fingerprint" "$fp_a"
set_state "legacy" "dedupe_fired_at" "$(date +%s)"
if dedupe_check "legacy" "threshold count > 0" "$DRIFT_4D"; then
    fail "a heartbeat with no realert_after must never be deduped"
else
    ok
fi
assert_eq "$DEDUPE_FINGERPRINT" "" "no realert_after means no fingerprint is carried to the caller"

# First sighting fires and hands the caller a fingerprint to store.
if dedupe_check "drift-default" "threshold count > 0" "$DRIFT_4D"; then
    fail "the first alert for a condition must fire"
else
    ok
fi
assert_eq "$DEDUPE_FINGERPRINT" "$fp_a" "dedupe_check exposes the fingerprint for the caller to store on a successful fire"

# Simulate the fire having gone out.
set_state "drift-default" "dedupe_fingerprint" "$DEDUPE_FINGERPRINT"
set_state "drift-default" "dedupe_fired_at" "$(date +%s)"

# The same condition, three days later, with both age fields moved.
if dedupe_check "drift-default" "threshold count > 0" "$DRIFT_7D"; then
    ok
else
    fail "the identical condition inside the window must be suppressed even though its age fields moved"
fi

# Outside the window it alerts again.
set_state "drift-default" "dedupe_fired_at" "$(( $(date +%s) - 90000 ))"
if dedupe_check "drift-default" "threshold count > 0" "$DRIFT_7D"; then
    fail "a condition older than realert_after must alert again"
else
    ok
fi

# A different condition is news immediately.
set_state "drift-default" "dedupe_fired_at" "$(date +%s)"
set_state "drift-default" "dedupe_fingerprint" "$fp_a"
if dedupe_check "drift-default" "threshold count > 0" "$DRIFT_TWO"; then
    fail "a changed condition must alert inside the window"
else
    ok
fi

# An unparseable window fires rather than silently muting.
if dedupe_check "bad-window" "threshold count > 0" "$DRIFT_4D"; then
    fail "an unparseable realert_after must fire, not suppress"
else
    ok
fi

# ask_model with no key warns and fires.
: > "$LOG_FILE"
if dedupe_check "asks-the-model" "ask_model" "$DRIFT_4D"; then
    fail "a heartbeat with no stable identity must fire"
else
    ok
fi
if grep -q "no stable condition identity" "$LOG_FILE"; then ok; else fail "refusing to dedupe must be logged, not silent"; fi

# ---------------------------------------------------------------------------
# 6. Churn detector: the fingerprint that never matches itself.
# ---------------------------------------------------------------------------
: > "$LOG_FILE"
set_state "drift-default" "dedupe_churn" "0"
set_state "drift-default" "dedupe_fingerprint" "seed"
i=1
while [[ $i -le 3 ]]; do
    # Every call sees a different count, so the identity moves every time -
    # the signature of a dedupe_key wired to something volatile.
    payload=$(printf '%s' "$DRIFT_TWO" | jq -c --argjson c "$i" '.count = $c')
    dedupe_check "drift-default" "threshold count > 0" "$payload"
    set_state "drift-default" "dedupe_fingerprint" "$DEDUPE_FINGERPRINT"
    i=$((i + 1))
done
if grep -q "changed on 3 consecutive fires" "$LOG_FILE"; then
    ok
else
    fail "a fingerprint that moves every fire must warn - that is the misconfiguration this feature dies of"
fi

# A match clears the churn counter.
set_state "drift-default" "dedupe_fired_at" "$(date +%s)"
dedupe_check "drift-default" "threshold count > 0" "$payload"
assert_eq "$(get_state 'drift-default' 'dedupe_churn')" "0" "a matching fingerprint resets the churn counter"

# ---------------------------------------------------------------------------
# 7. Daily fire cap.
# ---------------------------------------------------------------------------
TODAY="$DATE"
YESTERDAY="2000-01-01"

# Backwards compatibility: no cap keys, no cap, ever.
set_state "legacy" "fires_date" "$TODAY"
set_state "legacy" "fires_today" "999"
if fire_cap_check "legacy"; then
    fail "a heartbeat with no cap keys must never be capped"
else
    ok
fi

set_state "capped-fires" "fires_date" "$TODAY"
set_state "capped-fires" "fires_today" "2"
if fire_cap_check "capped-fires"; then
    fail "2 of 3 fires must still be allowed"
else
    ok
fi

set_state "capped-fires" "fires_today" "3"
if fire_cap_check "capped-fires"; then
    ok
else
    fail "the third of three fires must cap the fourth"
fi
if [[ "$CAP_REASON" == max_fires_per_day* ]]; then ok; else fail "CAP_REASON should name the cap that tripped, got '$CAP_REASON'"; fi

# The date rolling lifts it.
set_state "capped-fires" "fires_date" "$YESTERDAY"
set_state "capped-fires" "fires_today" "99"
if fire_cap_check "capped-fires"; then
    fail "yesterday's fire count must not cap today"
else
    ok
fi
assert_eq "$(fires_today 'capped-fires')" "0" "fires_today reads 0 once the date has rolled"

note_fire "capped-fires"
assert_eq "$(get_state 'capped-fires' 'fires_today')" "1" "note_fire restarts the count on a new day"
assert_eq "$(get_state 'capped-fires' 'fires_date')" "$TODAY" "note_fire stamps today's date"
note_fire "capped-fires"
assert_eq "$(get_state 'capped-fires' 'fires_today')" "2" "note_fire increments within the same day"

# A malformed cap is ignored loudly rather than capping everything.
: > "$LOG_FILE"
set_state "bad-cap" "fires_date" "$TODAY"
set_state "bad-cap" "fires_today" "500"
if fire_cap_check "bad-cap"; then
    fail "an unparseable cap value must not cap the heartbeat"
else
    ok
fi
if grep -q "is not a whole number" "$LOG_FILE"; then ok; else fail "an unparseable max_fires_per_day must be logged"; fi
if grep -q "is not a number" "$LOG_FILE"; then ok; else fail "an unparseable max_usd_per_day must be logged"; fi

# ---------------------------------------------------------------------------
# 8. Daily dollar cap, read from telemetry rather than a counter of our own.
# ---------------------------------------------------------------------------
TELEMETRY="$TMP_HOME/logs/telemetry/$DATE-usage.jsonl"

# No telemetry yet on the day's first tick.
rm -f "$TELEMETRY"
assert_eq "$(usd_today 'capped-usd')" "0" "a missing telemetry file reads as zero spend"
if fire_cap_check "capped-usd"; then
    fail "a heartbeat that has not spent anything must not be capped"
else
    ok
fi

{
    echo '{"ts":"2026-09-05T00:14:11","heartbeat":"capped-usd","model":"haiku","cost_usd":0.20}'
    echo '{"ts":"2026-09-05T00:30:11","heartbeat":"someone-else","model":"haiku","cost_usd":9.99}'
    echo 'this line is not json and must not take the cap down with it'
    echo '{"ts":"2026-09-05T00:45:11","heartbeat":"capped-usd","model":"haiku","cost_usd":null}'
} > "$TELEMETRY"
assert_eq "$(usd_today 'capped-usd')" "0.2" "usd_today sums only this heartbeat, tolerates a malformed line and a null cost"
if fire_cap_check "capped-usd"; then
    fail "\$0.20 of a \$0.50 cap must still fire"
else
    ok
fi

# The validator runs on this heartbeat's behalf, so its spend counts too.
echo '{"ts":"2026-09-05T01:00:11","heartbeat":"capped-usd-validator","model":"haiku","cost_usd":0.31}' >> "$TELEMETRY"
if fire_cap_check "capped-usd"; then
    ok
else
    fail "the heartbeat's own output-validator spend must count toward its daily cap"
fi
if [[ "$CAP_REASON" == max_usd_per_day* ]]; then ok; else fail "CAP_REASON should name the usd cap, got '$CAP_REASON'"; fi

# ---------------------------------------------------------------------------
# 9. The cap notice is itself capped.
# ---------------------------------------------------------------------------
ALERTS="$TMP_HOME/alerts.txt"
: > "$ALERTS"
alert_ops() { printf '%s\n---\n' "$1" >> "$ALERTS"; }

cap_alert_once "capped-fires" "max_fires_per_day reached: 3 of 3" "1" "YES - count=1"
cap_alert_once "capped-fires" "max_fires_per_day reached: 3 of 3" "2" "YES - count=1"
cap_alert_once "capped-fires" "max_fires_per_day reached: 3 of 3" "3" "YES - count=1"
assert_eq "$(grep -c -- '---' "$ALERTS")" "1" "a tripped cap posts exactly one notice per day, not one per tick"
if grep -q "heartbeat-state.json" "$ALERTS"; then ok; else fail "the notice must say how to lift the cap"; fi
if grep -q "heartbeat-suppressions.json" "$ALERTS"; then ok; else fail "the notice must point at the ledger"; fi

# A dollar cap points somewhere useful: clearing fires_today does nothing to a
# ceiling measured from telemetry, so the notice must not send anyone there.
: > "$ALERTS"
cap_alert_once "capped-usd" "max_usd_per_day reached: \$0.51 of \$0.50" "1" "YES - count=1"
if grep -q "raise .max_usd_per_day" "$ALERTS"; then ok; else fail "a usd-cap notice must name the field that actually lifts it"; fi
if grep -q "clear .fires_today" "$ALERTS"; then
    fail "a usd-cap notice must not tell an operator to clear fires_today, which does nothing to it"
else
    ok
fi
# Tomorrow it speaks again.
: > "$ALERTS"
cap_alert_once "capped-fires" "max_fires_per_day reached: 3 of 3" "40" "YES - count=1"
assert_eq "$(grep -c -- '---' "$ALERTS")" "0" "still the same day, still quiet"
set_state "capped-fires" "cap_alert_date" "$YESTERDAY"
cap_alert_once "capped-fires" "max_fires_per_day reached: 3 of 3" "50" "YES - count=1"
assert_eq "$(grep -c -- '---' "$ALERTS")" "1" "the notice repeats once the date rolls, so a muted heartbeat is not muted forever"

# ---------------------------------------------------------------------------
# 10. Suppression ledger.
# ---------------------------------------------------------------------------
ticks=$(ledger_note "repo-drift" "fire_cap" "max_fires_per_day reached: 3 of 3" "$fp_a" "YES - count=1")
assert_eq "$ticks" "1" "the first suppression records one tick"
first_at=$(jq -r '.["repo-drift"].first_suppressed_at' "$SUPPRESSION_LEDGER")

ticks=$(ledger_note "repo-drift" "fire_cap" "max_fires_per_day reached: 3 of 3" "$fp_a" "YES - count=1")
assert_eq "$ticks" "2" "repeat suppressions accumulate on one record rather than appending duplicates"
assert_eq "$(jq -r '.["repo-drift"].first_suppressed_at' "$SUPPRESSION_LEDGER")" "$first_at" \
    "first_suppressed_at is the start of the quiet period and must not be overwritten"
assert_eq "$(jq -r '.["repo-drift"].reason' "$SUPPRESSION_LEDGER")" "fire_cap" "the ledger records why it went quiet"
assert_eq "$(jq -r '.["repo-drift"].fingerprint' "$SUPPRESSION_LEDGER")" "$fp_a" "the ledger records the condition identity"
if [[ "$(jq -r '.["repo-drift"].last_suppressed_epoch' "$SUPPRESSION_LEDGER")" =~ ^[0-9]+$ ]]; then
    ok
else
    fail "the ledger must carry an epoch, since date -j -f is macOS-only and a consumer has to do arithmetic"
fi

ledger_note "telegram-groups" "dedup" "identical condition, last alerted 30min ago" "" "YES - count=4" >/dev/null
assert_eq "$(jq -r 'keys | length' "$SUPPRESSION_LEDGER")" "2" "the ledger holds one record per suppressed heartbeat"

# The gather renders it without a model call.
report=$(CUSTOMER_HOME="$TMP_HOME" bash "$LEDGER_GATHER")
assert_eq "$(echo "$report" | jq -r '.count')" "2" "the ledger gather reports both suppressed heartbeats"
assert_eq "$(echo "$report" | jq -r '.suppressed[] | select(.heartbeat == "repo-drift") | .suppressed_ticks')" "2" \
    "the ledger gather carries the tick count through"
if echo "$report" | jq -e '.suppressed[0] | has("quiet_hours")' >/dev/null; then
    ok
else
    fail "the ledger gather must expose how long each heartbeat has been quiet"
fi

# A fixed problem stops being reported.
ledger_clear "repo-drift"
assert_eq "$(jq -r 'keys | length' "$SUPPRESSION_LEDGER")" "1" "ledger_clear removes the entry when the condition clears"
report=$(CUSTOMER_HOME="$TMP_HOME" bash "$LEDGER_GATHER")
assert_eq "$(echo "$report" | jq -r '.count')" "1" "a cleared heartbeat disappears from the rollup"
ledger_clear "not-a-heartbeat"
assert_eq "$(jq -r 'keys | length' "$SUPPRESSION_LEDGER")" "1" "clearing an absent entry is a no-op, not an error"

# ---------------------------------------------------------------------------
# 11. Duration parsing.
# ---------------------------------------------------------------------------
assert_eq "$(parse_duration_seconds '24h')" "86400" "24h"
assert_eq "$(parse_duration_seconds '90m')" "5400" "90m"
assert_eq "$(parse_duration_seconds '2d')" "172800" "2d"
assert_eq "$(parse_duration_seconds '300s')" "300" "300s"
assert_eq "$(parse_duration_seconds '45')" "45" "a bare number is seconds"
if parse_duration_seconds 'soonish' >/dev/null; then
    fail "an unparseable duration must exit non-zero so the caller can warn"
else
    ok
fi

# ---------------------------------------------------------------------------
# 12. No leftover staging files from any of the above.
# ---------------------------------------------------------------------------
leftover=("$TMP_HOME"/scheduled-tasks/*.tmp(N) "$TMP_HOME"/scheduled-tasks/*.clr(N))
if [[ ${#leftover[@]} -gt 0 ]]; then
    fail "leftover staging files: ${leftover[*]}"
else
    ok
fi

if [[ $FAIL -eq 0 ]]; then
    echo "PASS: $CASES assertions"
    exit 0
else
    echo "FAILED ($CASES assertions run)"
    exit 1
fi
