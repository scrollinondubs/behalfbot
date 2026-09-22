#!/usr/bin/env bash
# test-claude-oauth-bridge-alert.sh - behavioural tests for the edge-triggered
# alerting added to sync-claude-oauth-bridge.sh.
#
# The defect (new-jaxity#550): the bridge DETECTED the macOS keychain losing
# its Claude token and told nobody. Between 2026-09-01 and 2026-09-05 it wrote
# `WARN: keychain JSON missing claudeAiOauth.accessToken` 288 times into a log
# file while the host interactive session was dead and the operator was
# off-grid. 48 warnings a day is the ceiling for a job running every 30
# minutes, and three consecutive days hit it.
#
# The fix has to clear two bars at once, and a test suite that only checks one
# of them is worthless:
#   - it must alert AT ALL on the first tick of a fault, and
#   - it must NOT alert 287 more times after that.
# Swapping a silent log for 288 webhook messages would be the same failure
# wearing a different hat.
#
# Silence on the common path is a property too: a bridge that has never
# faulted must post nothing, ever.
#
# No network, no real keychain. `security` is stubbed on PATH and delivery is
# redirected through CHASSIS_ALERT_CMD to a recorder.
#
# Exit 0 all pass, 1 on failure, 2 on harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="${SCRIPT_DIR}/sync-claude-oauth-bridge.sh"

if [[ ! -f "$BRIDGE" ]]; then
    echo "test-claude-oauth-bridge-alert: bridge not found at $BRIDGE" >&2
    exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "test-claude-oauth-bridge-alert: jq required" >&2
    exit 2
fi

fail=0
pass=0

TMP="$(mktemp -d)"
wipe_dir() {
    [[ -n "${1:-}" && -d "$1" ]] || return 0
    find "$1" -mindepth 1 -delete 2>/dev/null || true
}
cleanup() { wipe_dir "$TMP"; rmdir "$TMP" 2>/dev/null || true; }
trap cleanup EXIT

STUB_BIN="$TMP/bin"
STUB_STATE="$TMP/stubstate"
mkdir -p "$STUB_BIN" "$STUB_STATE" "$TMP/.claude"

JQ_DIR="$(dirname "$(command -v jq)")"
SAFE_PATH="$STUB_BIN:$JQ_DIR:/usr/bin:/bin:/usr/sbin:/sbin"

cat > "$STUB_BIN/security" <<'STUB'
#!/bin/bash
state="${STUB_STATE:?}"
case "$1" in
    add-generic-password) exit 0 ;;
esac
if [[ -f "$state/keychain.unreadable" ]]; then
    echo "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain." >&2
    exit 44
fi
cat "$state/keychain.json"
exit 0
STUB

cat > "$STUB_BIN/recorder" <<'STUB'
#!/bin/bash
printf '%s\n===ALERT===\n' "$1" >> "${RECORD_FILE:?}"
exit 0
STUB

cat > "$STUB_BIN/failing-recorder" <<'STUB'
#!/bin/bash
printf '%s\n===ALERT===\n' "$1" >> "${RECORD_FILE:?}"
exit 1
STUB
chmod +x "$STUB_BIN/security" "$STUB_BIN/recorder" "$STUB_BIN/failing-recorder"

NOW="$(date -u +%s)"

reset() {
    wipe_dir "$STUB_STATE"
    mkdir -p "$STUB_STATE"
    rm -f "$TMP/record.txt" "$TMP/alert.state" "$TMP/.claude/.credentials.json"
    wipe_dir "$TMP/logs"
    RECORDER="$STUB_BIN/recorder"
}

plant_keychain() {
    # $1 accessToken. An empty string is the exact 2026-09-01 state: the entry
    # is there, the token is not.
    cat > "$STUB_STATE/keychain.json" <<JSON
{"claudeAiOauth": {"accessToken": "$1", "refreshToken": "sk-refresh-REDACTED",
                   "expiresAt": $(( (NOW + 3600) * 1000 )), "scopes": ["user:inference"]}}
JSON
}

# The container's side of the bridge: $HOME/.claude/.credentials.json.
#
# This matters more than it looks. Since the #559 rescue path landed, an empty
# keychain is NOT automatically a fault - if this file holds an unexpired
# token, the bridge copies it back up and self-heals rather than alerting. So
# "keychain is empty" and "the operator needs to hear about it" are now two
# different states, and a test that wants the fault has to arrange both sides.
plant_container_credential() {
    # $1 accessToken, $2 seconds from now until expiry (negative for expired).
    local offset="${2:-3600}"
    mkdir -p "$TMP/.claude"
    cat > "$TMP/.claude/.credentials.json" <<JSON
{"claudeAiOauth": {"accessToken": "$1", "refreshToken": "sk-refresh-REDACTED",
                   "expiresAt": $(( (NOW + offset) * 1000 )), "scopes": ["user:inference"]}}
JSON
}

clear_container_credential() {
    rm -f "$TMP/.claude/.credentials.json"
}

run_bridge() {
    env -i \
        PATH="$SAFE_PATH" \
        HOME="$TMP" \
        USER="tester" \
        STUB_STATE="$STUB_STATE" \
        CUSTOMER_HOME="$TMP" \
        LOG_DIR="$TMP/logs" \
        RECORD_FILE="$TMP/record.txt" \
        CHASSIS_OAUTH_BRIDGE_ALERT_STATE="$TMP/alert.state" \
        CHASSIS_ALERT_CMD="$RECORDER" \
        KEYCHAIN_ACCOUNT="tester" \
        bash "$BRIDGE" >/dev/null 2>&1
}

alert_count() {
    [[ -f "$TMP/record.txt" ]] || { echo 0; return; }
    grep -c '^===ALERT===$' "$TMP/record.txt" 2>/dev/null || echo 0
}
alert_text() { cat "$TMP/record.txt" 2>/dev/null || true; }
log_text() { cat "$TMP/logs/claude-oauth-bridge-sync.log" 2>/dev/null || true; }

ok() { pass=$((pass + 1)); }
bad() { echo "FAIL [$1] $2"; fail=$((fail + 1)); }

assert_alerts() {
    local name="$1" want="$2" got
    got="$(alert_count)"
    if [[ "$got" == "$want" ]]; then ok
    else bad "$name" "expected $want alert(s), got $got"; fi
}

assert_state() {
    local name="$1" want="$2" got
    got="$(cat "$TMP/alert.state" 2>/dev/null || echo "<absent>")"
    if [[ "$got" == "$want" ]]; then ok; else bad "$name" "expected alert state '$want', got '$got'"; fi
}

assert_contains() {
    local name="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then ok; else bad "$name" "expected '$needle' in: $haystack"; fi
}

assert_not_contains() {
    local name="$1" haystack="$2" needle="$3"
    if [[ "$haystack" != *"$needle"* ]]; then ok; else bad "$name" "did NOT expect '$needle' in: $haystack"; fi
}

# --- 1. a healthy bridge that has never faulted posts nothing ---------------
# This is the common path. It runs every 30 minutes on every macOS install,
# so a helper that chats on a healthy sync would be worse than the bug.
reset
plant_keychain "sk-ant-oat-HEALTHY"
run_bridge
run_bridge
run_bridge
assert_alerts "a healthy bridge posts nothing" 0
assert_contains "a healthy bridge still syncs" "$(log_text)" "synced: keychain"

# --- 2. the fault posts once, on the FIRST tick ----------------------------
# `reset` clears the container credential, so this is the genuine both-sides-
# dead state: the keychain has no token and there is nothing to rescue from.
reset
plant_keychain ""
run_bridge
assert_alerts "the missing-token fault alerts on the first tick" 1
assert_state "the fault condition is recorded" "keychain_missing_token"
assert_contains "the alert names the credential" "$(alert_text)" "claudeAiOauth.accessToken"
assert_contains "the alert names the fix" "$(alert_text)" "/login"
# This assertion used to look for "container is unaffected", which was true
# before the #559 rescue path existed: back then an empty keychain said
# nothing about the container, so the alert reassured the operator that the
# scheduled side kept running. It is now the opposite. Reaching alert_fault at
# all means the rescue was tried and found nothing usable, so the container
# has no good token either. Asserting the old wording would be asserting a
# claim the bridge can no longer make.
assert_contains "the alert says the container could not rescue it" "$(alert_text)" "no usable token to restore from"

# --- 3. THE 288 regression: it does not keep posting ------------------------
run_bridge; run_bridge; run_bridge; run_bridge; run_bridge
assert_alerts "five more ticks in the same fault post nothing further" 1

# --- 4. the WARN is still logged every tick --------------------------------
# The log line is the forensic record that dated this outage to the minute.
# Alerting once must not cost the per-tick log entry.
LOG_WARNS="$(log_text | grep -c 'WARN: keychain JSON missing' || true)"
if [[ "$LOG_WARNS" == "6" ]]; then ok
else bad "log retained" "expected 6 WARN log lines across 6 ticks, got $LOG_WARNS"; fi

# --- 5. recovery posts exactly once ----------------------------------------
plant_keychain "sk-ant-oat-RECOVERED"
run_bridge
assert_alerts "recovery posts one more message" 2
assert_state "the recovery clears the condition" "ok"
assert_contains "the recovery message says so" "$(alert_text)" "recovered"
run_bridge; run_bridge
assert_alerts "a recovered bridge goes quiet again" 2

# --- 6. a re-fault after a recovery alerts again ---------------------------
# The edge has to be re-armed. A latch that only ever fires once per install
# would be silent for the second outage.
#
# The container credential has to be cleared explicitly. Step 5's recovery ran
# a healthy forward-sync, which wrote the good keychain token down into
# .credentials.json. Emptying the keychain alone would now hit the #559 rescue
# path, restore from that file and correctly NOT alert - so without this line
# the test asserts a fault it never actually created. That is exactly how this
# assertion started failing.
clear_container_credential
plant_keychain ""
run_bridge
assert_alerts "a second fault alerts again" 3

# --- 6b. the #559 rescue path repairs instead of alerting ------------------
# An empty keychain plus a still-valid container token is not an outage, it is
# the routine aftermath of the container refreshing and rotating the host's
# refresh token out from under it. The bridge copies the container's copy back
# up and nobody needs to be told. Alerting here would be the 288-warning bug
# in a new costume: a message every 30 minutes about something already fixed.
#
# This path had no coverage. It is what quietly replaced the old
# "container is unaffected" behaviour, and its absence is why the change
# looked like a test regression rather than a deliberate one.
reset
plant_container_credential "sk-ant-oat-FROM-CONTAINER" 3600
plant_keychain ""
run_bridge
assert_alerts "a rescuable keychain posts nothing" 0
assert_contains "the rescue is logged" "$(log_text)" "rescued: keychain had no accessToken"
run_bridge
run_bridge
assert_alerts "repeated rescuable ticks stay quiet" 0

# A rescue is a recovery when a fault was already latched, and must clear it.
# Otherwise the next genuine outage would be swallowed by a stale latch.
reset
plant_keychain ""
run_bridge
assert_alerts "both sides dead alerts" 1
assert_state "the fault is latched" "keychain_missing_token"
plant_container_credential "sk-ant-oat-FROM-CONTAINER" 3600
run_bridge
assert_alerts "the rescue posts the recovery notice" 2
assert_state "the rescue clears the latched fault" "ok"
assert_contains "the recovery names it as such" "$(alert_text)" "recovered"

# --- 6c. an EXPIRED container token is not a rescue ------------------------
# Writing a dead token back into the keychain would clear the fault state and
# silence the alert while leaving the host just as unauthenticated. Worse than
# the outage, because it hides it.
reset
plant_container_credential "sk-ant-oat-STALE" -3600
plant_keychain ""
run_bridge
assert_alerts "an expired container token still alerts" 1
assert_state "the expired-rescue case records the missing-token fault" "keychain_missing_token"
assert_not_contains "no rescue is claimed in the log" "$(log_text)" "rescued: keychain had no accessToken"

# --- 7. an unreadable keychain is its own condition ------------------------
reset
touch "$STUB_STATE/keychain.unreadable"
run_bridge
assert_alerts "an unreadable keychain alerts" 1
assert_state "the unreadable condition is recorded separately" "keychain_unreadable"
assert_contains "the unreadable alert names the launchd trap" "$(alert_text)" "launchd-domains.md"

# --- 8. moving between fault conditions alerts again -----------------------
# `unreadable` and `missing_token` need different fixes, so collapsing them
# into one "something is wrong" latch would send the operator after the wrong
# thing.
rm -f "$STUB_STATE/keychain.unreadable"
plant_keychain ""
run_bridge
assert_alerts "a different fault condition alerts again" 2
assert_state "the new condition replaces the old" "keychain_missing_token"

# --- 9. a delivery failure never breaks the sync ---------------------------
# Alerting is a courtesy on top of the bridge's actual job. A webhook that is
# down, unset or misconfigured must not stop credentials propagating.
reset
RECORDER="$STUB_BIN/failing-recorder"
plant_keychain "sk-ant-oat-HEALTHY"
run_bridge
RC=$?
if [[ "$RC" == "0" ]]; then ok; else bad "delivery failure" "bridge exited $RC when delivery failed"; fi
assert_contains "the healthy sync still happened" "$(log_text)" "synced: keychain"

reset
RECORDER="$STUB_BIN/failing-recorder"
plant_keychain ""
run_bridge
RC=$?
if [[ "$RC" == "0" ]]; then ok; else bad "delivery failure in a fault" "bridge exited $RC"; fi
assert_contains "a failed delivery is logged" "$(log_text)" "could not deliver"
assert_state "a failed delivery still records the condition" "keychain_missing_token"
# Recording the condition despite the failure is deliberate: an install with
# no webhook configured would otherwise retry every 30 minutes forever, which
# is the noise this change exists to remove.
run_bridge
assert_alerts "an undeliverable alert is not retried every tick" 1

# --- 10. no secrets ever reach the alert or the log ------------------------
reset
plant_keychain "sk-ant-oat-SUPERSECRET"
run_bridge
assert_not_contains "the token never reaches the alert channel" "$(alert_text)" "SUPERSECRET"
assert_not_contains "the token never reaches the log" "$(log_text)" "SUPERSECRET"

# --- 11. the bridge survives _alert.sh not being there ---------------------
# This script runs under `set -euo pipefail` every 30 minutes and is the only
# thing keeping the container authenticated. An unguarded `source` of a
# sibling would kill the sync outright on any install running a copy of it
# from somewhere else, which would make the most critical script in the
# install more fragile in a change about resilience.
reset
ISOLATED="$TMP/isolated"
mkdir -p "$ISOLATED"
cp "$BRIDGE" "$ISOLATED/"
plant_keychain "sk-ant-oat-HEALTHY"
env -i PATH="$SAFE_PATH" HOME="$TMP" USER="tester" STUB_STATE="$STUB_STATE" \
    CUSTOMER_HOME="$TMP" LOG_DIR="$TMP/logs" KEYCHAIN_ACCOUNT="tester" \
    bash "$ISOLATED/sync-claude-oauth-bridge.sh" >/dev/null 2>&1
RC=$?
if [[ "$RC" == "0" ]]; then ok; else bad "orphaned bridge" "exited $RC with no _alert.sh sibling"; fi
assert_contains "an orphaned bridge still syncs" "$(log_text)" "synced: keychain"

reset
plant_keychain ""
env -i PATH="$SAFE_PATH" HOME="$TMP" USER="tester" STUB_STATE="$STUB_STATE" \
    CUSTOMER_HOME="$TMP" LOG_DIR="$TMP/logs" KEYCHAIN_ACCOUNT="tester" \
    bash "$ISOLATED/sync-claude-oauth-bridge.sh" >/dev/null 2>&1
RC=$?
if [[ "$RC" == "0" ]]; then ok; else bad "orphaned bridge in a fault" "exited $RC with no _alert.sh sibling"; fi
assert_contains "an orphaned bridge still logs the WARN" "$(log_text)" "WARN: keychain JSON missing"

# --- 12. _alert.sh delivery fallbacks --------------------------------------
# An install can have a bot token and channel IDs and NO webhook URL - the
# reference install is exactly that shape, which is why the dispatcher's own
# `alert_ops()` has been dropping alerts there. A helper that stopped at the
# webhook would be one more monitor firing into a log.
LIBDIR="$TMP/lib"
mkdir -p "$LIBDIR"
cp "${SCRIPT_DIR}/_alert.sh" "$LIBDIR/"

cat > "$LIBDIR/post-to-channel.sh" <<'STUB'
#!/bin/bash
[[ -n "${WEBHOOK_CONFIGURED:-}" ]] || exit 1
printf 'webhook:%s:%s
' "$1" "$2" >> "${RECORD_FILE:?}"
STUB
cat > "$LIBDIR/discord-post.sh" <<'STUB'
#!/bin/bash
printf 'bot:%s:%s
' "$1" "$2" >> "${RECORD_FILE:?}"
STUB
chmod +x "$LIBDIR/post-to-channel.sh" "$LIBDIR/discord-post.sh"

try_alert() {
    # $1..$n are KEY=VALUE pairs for the alert environment.
    env -i PATH="$SAFE_PATH" HOME="$TMP" RECORD_FILE="$TMP/record.txt" \
        CUSTOMER_HOME="$TMP" "$@" \
        bash -c 'source "$0"; chassis_alert "hello"' "$LIBDIR/_alert.sh"
}

rm -f "$TMP/record.txt"
try_alert WEBHOOK_CONFIGURED=1 CHASSIS_ALERT_CHANNEL=ops >/dev/null 2>&1
assert_contains "a configured webhook is used first" "$(cat "$TMP/record.txt" 2>/dev/null)" "webhook:ops:hello"

rm -f "$TMP/record.txt"
printf 'DISCORD_BOT_TOKEN=bot-token-fixture
DISCORD_ALERTS_CHANNEL_ID=111222333
' > "$TMP/.env"
try_alert >/dev/null 2>&1
assert_contains "no webhook falls through to the bot token" "$(cat "$TMP/record.txt" 2>/dev/null)" "bot:111222333:hello"

rm -f "$TMP/record.txt"
printf 'DISCORD_BOT_TOKEN=bot-token-fixture
DISCORD_PRIMARY_CHANNEL_ID=999888777
' > "$TMP/.env"
try_alert >/dev/null 2>&1
assert_contains "the primary channel is the last resort" "$(cat "$TMP/record.txt" 2>/dev/null)" "bot:999888777:hello"

rm -f "$TMP/record.txt"
printf 'NOTHING_USEFUL=1
' > "$TMP/.env"
if try_alert >/dev/null 2>&1; then
    bad "no delivery path" "chassis_alert reported success with nothing configured"
else
    ok
fi
if [[ ! -f "$TMP/record.txt" ]]; then ok; else bad "no delivery path" "something was posted anyway: $(cat "$TMP/record.txt")"; fi
rm -f "$TMP/.env"

echo ""
echo "test-claude-oauth-bridge-alert: $pass passed, $fail failed"
[[ $fail -eq 0 ]] || exit 1
exit 0
