#!/bin/bash
# test-daily-log-gather.sh - Smoke test for daily-log-gather.py.
#
# Verifies:
#   1. Runs with no configuration env vars set and emits valid JSON with
#      all-empty buckets + populated warnings.
#   2. Individual surfaces can be independently disabled via env var absence.
#   3. Output is parseable JSON in every scenario.
#   4. The `warnings` array reports each skipped surface.
#
# No real API calls: every scenario runs with the required auth env vars
# unset, so each surface short-circuits into the "skipped, warning added"
# path. Chassis test infra intentionally avoids network + credentials.
#
# Exit codes:
#   0 - all scenarios passed
#   1 - one or more scenarios failed
#   2 - test harness itself broke (missing python3 / jq)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATHER="${SCRIPT_DIR}/daily-log-gather.py"

fail=0
pass=0

need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "test-daily-log-gather: required dep missing: $1" >&2
        exit 2
    fi
}
need python3
need jq

if [[ ! -x "$GATHER" ]]; then
    echo "test-daily-log-gather: gather script not executable at $GATHER" >&2
    exit 2
fi

# --- Isolate env: strip every DAILY_LOG_*, SIYUAN_*, DISCORD_* var so each
# scenario starts from a known baseline. Set CHASSIS_HOME to a tmp dir so
# there's no accidental customer-repo scan.
TMPHOME="$(mktemp -d)"
trap 'rm -rf "$TMPHOME"' EXIT

# Deliberately no `.git` in TMPHOME so metrics.commits_customer_repo = 0.

run_scenario() {
    local name="$1"
    shift
    # env -i wipes the inherited environment, then re-establishes only the
    # vars we pass explicitly.
    local out
    out=$(env -i \
        PATH="$PATH" \
        HOME="$HOME" \
        CHASSIS_HOME="$TMPHOME" \
        "$@" \
        python3 "$GATHER" 2>/dev/null)
    if [[ -z "$out" ]]; then
        echo "FAIL [$name] gather produced no output"
        fail=$((fail + 1))
        return
    fi
    if ! echo "$out" | jq . >/dev/null 2>&1; then
        echo "FAIL [$name] output is not valid JSON"
        echo "-- output --"
        echo "$out"
        fail=$((fail + 1))
        return
    fi
    # Save for scenario-specific assertions.
    echo "$out" > "$TMPHOME/last-output.json"
    pass=$((pass + 1))
}

assert_key() {
    local name="$1"
    local key="$2"
    if ! jq -e "$key" "$TMPHOME/last-output.json" >/dev/null 2>&1; then
        echo "FAIL [$name] missing key: $key"
        fail=$((fail + 1))
        return 1
    fi
    return 0
}

assert_empty_array() {
    local name="$1"
    local key="$2"
    local len
    len=$(jq -r "$key | length" "$TMPHOME/last-output.json")
    if [[ "$len" != "0" ]]; then
        echo "FAIL [$name] expected empty $key, got length $len"
        fail=$((fail + 1))
        return 1
    fi
    return 0
}

assert_warning_contains() {
    local name="$1"
    local needle="$2"
    if ! jq -e --arg n "$needle" \
        '.warnings | map(select(contains($n))) | length > 0' \
        "$TMPHOME/last-output.json" >/dev/null 2>&1; then
        echo "FAIL [$name] warnings missing '$needle'"
        echo "-- warnings --"
        jq -r '.warnings[]' "$TMPHOME/last-output.json"
        fail=$((fail + 1))
        return 1
    fi
    return 0
}

# ------------------------------------------------------------------------
# Scenario 1: bare invocation, zero env config. Every surface skips with a
# warning. All buckets empty. Metrics all zeros.
# ------------------------------------------------------------------------
run_scenario "bare"
if [[ -f "$TMPHOME/last-output.json" ]]; then
    assert_key "bare" ".date"
    assert_key "bare" ".prs_by_repo"
    assert_key "bare" ".open_issues_awaiting_input"
    assert_key "bare" ".operational_email"
    assert_key "bare" ".siyuan_activity"
    assert_key "bare" ".postmortems"
    assert_key "bare" ".metrics"
    assert_key "bare" ".warnings"
    assert_empty_array "bare" ".open_issues_awaiting_input"
    assert_empty_array "bare" ".operational_email"
    assert_empty_array "bare" ".siyuan_activity"
    assert_empty_array "bare" ".postmortems"
    # prs_by_repo is an object; assert it's empty.
    if [[ "$(jq -r '.prs_by_repo | length' "$TMPHOME/last-output.json")" != "0" ]]; then
        echo "FAIL [bare] expected empty prs_by_repo"
        fail=$((fail + 1))
    fi
    assert_warning_contains "bare" "DAILY_LOG_GH_USER"
    assert_warning_contains "bare" "DAILY_LOG_GMAIL_IDENTITY"
    assert_warning_contains "bare" "DAILY_LOG_SIYUAN_URL"
    assert_warning_contains "bare" "DAILY_LOG_DISCORD_CHANNEL_ID"
fi

# ------------------------------------------------------------------------
# Scenario 2: SIYUAN_URL set but unreachable. Should emit a warning about
# a failed SQL query, not crash. (Local port that isn't listening.)
# ------------------------------------------------------------------------
run_scenario "siyuan-unreachable" \
    DAILY_LOG_SIYUAN_URL="http://127.0.0.1:1"
if [[ -f "$TMPHOME/last-output.json" ]]; then
    # Warning specifically about SiYuan failing (not the "unset" warning).
    if ! jq -e '.warnings | map(select(contains("siyuan"))) | length > 0' \
        "$TMPHOME/last-output.json" >/dev/null; then
        echo "FAIL [siyuan-unreachable] expected a siyuan-related warning"
        fail=$((fail + 1))
    fi
    assert_empty_array "siyuan-unreachable" ".siyuan_activity"
fi

# ------------------------------------------------------------------------
# Scenario 3: Discord channel set but token unset. Skips with a "token
# unset" warning, not a network attempt.
# ------------------------------------------------------------------------
run_scenario "discord-no-token" \
    DAILY_LOG_DISCORD_CHANNEL_ID="1234567890123456789"
if [[ -f "$TMPHOME/last-output.json" ]]; then
    assert_warning_contains "discord-no-token" "DISCORD_TOKEN"
    assert_empty_array "discord-no-token" ".postmortems"
fi

# ------------------------------------------------------------------------
# Scenario 4: EXTRA_METRICS_SCRIPT points at a non-existent path. Should
# treat it as "no custom metrics" and not crash.
# ------------------------------------------------------------------------
run_scenario "missing-extra-script" \
    DAILY_LOG_EXTRA_METRICS_SCRIPT="/nonexistent/path/emit.sh"
if [[ -f "$TMPHOME/last-output.json" ]]; then
    # custom should be an empty object (script not invoked).
    if [[ "$(jq -r '.metrics.custom | length' "$TMPHOME/last-output.json")" != "0" ]]; then
        echo "FAIL [missing-extra-script] expected empty metrics.custom"
        fail=$((fail + 1))
    fi
fi

# ------------------------------------------------------------------------
# Scenario 5: valid EXTRA_METRICS_SCRIPT. Chassis calls it and merges output.
# ------------------------------------------------------------------------
cat >"$TMPHOME/emit-metrics.sh" <<'EMIT'
#!/bin/bash
echo '{"dating_swipes": 12, "outreach_sent": 3}'
EMIT
chmod +x "$TMPHOME/emit-metrics.sh"
run_scenario "valid-extra-script" \
    DAILY_LOG_EXTRA_METRICS_SCRIPT="$TMPHOME/emit-metrics.sh"
if [[ -f "$TMPHOME/last-output.json" ]]; then
    if [[ "$(jq -r '.metrics.custom.dating_swipes' "$TMPHOME/last-output.json")" != "12" ]]; then
        echo "FAIL [valid-extra-script] expected metrics.custom.dating_swipes=12"
        fail=$((fail + 1))
    fi
    if [[ "$(jq -r '.metrics.custom.outreach_sent' "$TMPHOME/last-output.json")" != "3" ]]; then
        echo "FAIL [valid-extra-script] expected metrics.custom.outreach_sent=3"
        fail=$((fail + 1))
    fi
fi

# ------------------------------------------------------------------------
# Scenario 6: --now override produces a stable date.
# ------------------------------------------------------------------------
out=$(env -i \
    PATH="$PATH" \
    HOME="$HOME" \
    CHASSIS_HOME="$TMPHOME" \
    python3 "$GATHER" --now 2026-07-02T02:00:00Z 2>/dev/null)
if ! echo "$out" | jq . >/dev/null 2>&1; then
    echo "FAIL [now-override] output not valid JSON"
    fail=$((fail + 1))
else
    date_field=$(echo "$out" | jq -r '.date')
    if [[ "$date_field" != "2026-07-01" ]]; then
        echo "FAIL [now-override] expected date=2026-07-01 (yesterday of 2026-07-02), got $date_field"
        fail=$((fail + 1))
    else
        pass=$((pass + 1))
    fi
fi

# ------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------
echo
echo "test-daily-log-gather: $pass passed, $fail failed"
if [[ $fail -gt 0 ]]; then
    exit 1
fi
exit 0
