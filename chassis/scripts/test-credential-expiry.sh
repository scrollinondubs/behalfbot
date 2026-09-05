#!/usr/bin/env bash
# test-credential-expiry.sh - behavioural tests for gather-credential-expiry.sh.
#
# Per the "checks that cannot fail" rule: prove the check CAN fire before
# trusting it. These tests stub `tailscale` and `security` on PATH and force
# every failure the 2026-09-01 outage actually produced (new-jaxity#550),
# then assert the gather stays SILENT on a healthy host.
#
# The load-bearing cases are the suppression ones. A monitor that fires on
# every tick gets muted by its operator inside a week, and the specific way
# this codebase has broken before is a fingerprint built over a volatile
# counter: include `days_remaining` in the signature and it never matches its
# own previous value, so the cooldown never applies. Cases 12 to 16 pin that
# shut.
#
# No network, no docker, no real keychain, no tailnet.
#
# Exit 0 all pass, 1 on failure, 2 on harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATHER="${SCRIPT_DIR}/gather-credential-expiry.sh"

if [[ ! -f "$GATHER" ]]; then
    echo "test-credential-expiry: gather not found at $GATHER" >&2
    exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "test-credential-expiry: jq required" >&2
    exit 2
fi

fail=0
pass=0

TMP="$(mktemp -d)"

# Emptying a directory with `find -delete` rather than a recursive remove of a
# variable, so a mis-set variable cannot take anything with it.
wipe_dir() {
    [[ -n "${1:-}" && -d "$1" ]] || return 0
    find "$1" -mindepth 1 -delete 2>/dev/null || true
}
cleanup() { wipe_dir "$TMP"; rmdir "$TMP" 2>/dev/null || true; }
trap cleanup EXIT

STUB_BIN="$TMP/bin"
STUB_STATE="$TMP/stubstate"
DROPIN="$TMP/credential-checks.d"
mkdir -p "$STUB_BIN" "$STUB_STATE" "$DROPIN"

# The PATH the gather runs under. The stub dir comes first so it shadows any
# real `tailscale` / `security`; jq's own directory has to stay reachable.
JQ_DIR="$(dirname "$(command -v jq)")"
SAFE_PATH="$STUB_BIN:$JQ_DIR:/usr/bin:/bin:/usr/sbin:/sbin"

cat > "$STUB_BIN/tailscale" <<'STUB'
#!/bin/bash
state="${STUB_STATE:?}"
if [[ "$1" == "status" && -f "$state/tailscale.json" ]]; then
    cat "$state/tailscale.json"
    exit 0
fi
echo "failed to connect to local tailscaled" >&2
exit 1
STUB

cat > "$STUB_BIN/security" <<'STUB'
#!/bin/bash
state="${STUB_STATE:?}"
if [[ -f "$state/keychain.locked" ]]; then
    echo "security: SecKeychainSearchCopyNext: User interaction is not allowed." >&2
    exit 36
fi
if [[ -f "$state/keychain.json" ]]; then
    cat "$state/keychain.json"
    exit 0
fi
echo "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain." >&2
exit 44
STUB
chmod +x "$STUB_BIN/tailscale" "$STUB_BIN/security"

NOW="$(date -u +%s)"
in_days() { echo $(( NOW + ${1} * 86400 )); }
in_days_ms() { echo $(( (NOW + ${1} * 86400) * 1000 )); }
iso_in_days() {
    # BSD then GNU. Only used to build fixtures, never by the gather. BSD date
    # wants an explicit sign and rejects "+-2d", so a negative offset carries
    # its own sign and a positive one gets a "+".
    local n="$1" bsd="$1"
    [[ "$n" == -* ]] || bsd="+$n"
    date -u -v "${bsd}d" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -d "${n} days" +%Y-%m-%dT%H:%M:%SZ
}

reset() {
    wipe_dir "$STUB_STATE"
    wipe_dir "$DROPIN"
    rm -f "$TMP/state.json" "$TMP/credentials.json"
    CHECKS_ENABLED="tailscale,claude"
    TS_BIN_OVERRIDE=""
    REALERT_DAYS="3"
}

# A tailnet whose self key expires at $1 (empty = expiry disabled) and one
# peer expiring in $2 days (empty = no peers).
plant_tailscale() {
    local self_expiry="$1" peer_days="${2:-}" backend="${3:-Running}"
    local self_line="" peer_block="{}"
    if [[ -n "$self_expiry" ]]; then
        self_line="\"KeyExpiry\": \"$self_expiry\","
    fi
    if [[ -n "$peer_days" ]]; then
        peer_block="{\"nodekey:aaa\": {\"HostName\": \"Sean's MacBook Pro\", \"DNSName\": \"seans-macbook-pro.tailnet.ts.net.\", \"OS\": \"macOS\", \"KeyExpiry\": \"$(iso_in_days "$peer_days")\"}}"
    fi
    cat > "$STUB_STATE/tailscale.json" <<JSON
{"BackendState": "$backend",
 "Self": {"HostName": "test-host", "DNSName": "test-host.tailnet.ts.net.", $self_line "OS": "macOS"},
 "Peer": $peer_block}
JSON
}

plant_keychain() {
    # $1 accessToken (empty = key present but blank), $2 refresh-token expiry
    # in days, $3 access-token expiry offset in hours (default +1)
    local token="$1" refresh_days="${2:-30}" access_hours="${3:-1}"
    cat > "$STUB_STATE/keychain.json" <<JSON
{"claudeAiOauth": {"accessToken": "$token",
                   "refreshToken": "sk-refresh-REDACTED",
                   "expiresAt": $(( (NOW + access_hours * 3600) * 1000 )),
                   "refreshTokenExpiresAt": $(in_days_ms "$refresh_days")}}
JSON
}

plant_credfile() {
    local token="$1" refresh_days="${2:-30}" access_hours="${3:-1}"
    cat > "$TMP/credentials.json" <<JSON
{"claudeAiOauth": {"accessToken": "$token",
                   "refreshToken": "sk-refresh-REDACTED",
                   "expiresAt": $(( (NOW + access_hours * 3600) * 1000 )),
                   "refreshTokenExpiresAt": $(in_days_ms "$refresh_days")}}
JSON
}

run_gather() {
    env -i \
        PATH="$SAFE_PATH" \
        HOME="$TMP" \
        USER="tester" \
        STUB_STATE="$STUB_STATE" \
        CUSTOMER_HOME="$TMP" \
        CHASSIS_HOME="$TMP" \
        CHASSIS_CREDENTIAL_CHECKS="$CHECKS_ENABLED" \
        CHASSIS_CREDENTIAL_CHECKS_DIR="$DROPIN" \
        CHASSIS_CREDENTIAL_STATE="$TMP/state.json" \
        CHASSIS_CREDENTIAL_REALERT_DAYS="$REALERT_DAYS" \
        CHASSIS_CREDENTIAL_TAILSCALE_BIN="$TS_BIN_OVERRIDE" \
        CLAUDE_CREDENTIALS_FILE="$TMP/credentials.json" \
        KEYCHAIN_ACCOUNT="tester" \
        bash "$GATHER" 2>/dev/null
}

ok() { pass=$((pass + 1)); }
bad() { echo "FAIL [$1] $2"; fail=$((fail + 1)); }

assert_count() {
    local name="$1" out="$2" want="$3" got
    got="$(jq -r '.count' <<<"$out" 2>/dev/null)"
    if [[ "$got" == "$want" ]]; then ok
    else bad "$name" "expected count=$want, got count=$got :: $(jq -c '{count,issues,status,skipped}' <<<"$out" 2>/dev/null)"; fi
}

assert_issue() {
    local name="$1" out="$2" want="$3"
    if [[ "$(jq -r --arg t "$want" '.issues | index($t) // "MISSING"' <<<"$out" 2>/dev/null)" != "MISSING" ]]; then ok
    else bad "$name" "expected issue '$want', got $(jq -c '.issues' <<<"$out" 2>/dev/null)"; fi
}

assert_no_issue() {
    local name="$1" out="$2" unwanted="$3"
    if [[ "$(jq -r --arg t "$unwanted" '.issues | index($t) // "MISSING"' <<<"$out" 2>/dev/null)" == "MISSING" ]]; then ok
    else bad "$name" "did not expect issue '$unwanted', got $(jq -c '.issues' <<<"$out" 2>/dev/null)"; fi
}

assert_status() {
    local name="$1" out="$2" want="$3" got
    got="$(jq -r '.status' <<<"$out" 2>/dev/null)"
    if [[ "$got" == "$want" ]]; then ok; else bad "$name" "expected status=$want, got $got"; fi
}

assert_contains() {
    local name="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then ok; else bad "$name" "expected output to contain '$needle', got: $haystack"; fi
}

assert_not_contains() {
    local name="$1" haystack="$2" needle="$3"
    if [[ "$haystack" != *"$needle"* ]]; then ok; else bad "$name" "output must NOT contain '$needle'"; fi
}

# --- 1. healthy host: everything green, nothing said -------------------------
reset
plant_tailscale "" 90
plant_keychain "sk-ant-oat-SECRETVALUE" 30
plant_credfile "sk-ant-oat-SECRETVALUE" 30
OUT="$(run_gather)"
assert_count "healthy host is silent" "$OUT" 0
assert_status "healthy host reports ok" "$OUT" "ok"

# --- 2. no secrets ever reach stdout ----------------------------------------
# The dispatcher logs gather stdout verbatim, so this is a security property
# rather than a nicety.
assert_not_contains "access token never appears in output" "$OUT" "SECRETVALUE"
assert_not_contains "refresh token never appears in output" "$OUT" "sk-refresh-REDACTED"

# --- 3. the gather always exits 0 -------------------------------------------
# A non-zero gather exit reads to the dispatcher as count=0, which would go
# silent exactly when this check matters.
reset
TS_BIN_OVERRIDE="/nonexistent/tailscale"
OUT="$(run_gather)"; RC=$?
if [[ "$RC" == "0" ]]; then ok; else bad "exit code" "gather exited $RC on a host with nothing configured"; fi

# --- 4. a missing tailscale binary is a graceful skip, not an alarm ---------
assert_contains "missing tailscale is reported as skipped" "$(jq -c '.skipped' <<<"$OUT")" "not executable"
assert_no_issue "missing tailscale raises no tailscale issue" "$OUT" "tailscale-self_missing"

# --- 5. tailscaled down is a graceful skip ----------------------------------
reset
CHECKS_ENABLED="tailscale"
OUT="$(run_gather)"
assert_count "tailscaled down does not alarm" "$OUT" 0
assert_contains "tailscaled down is reported as skipped" "$(jq -c '.skipped' <<<"$OUT")" "daemon not running"

# --- 6. Self.KeyExpiry null is the HEALTHY state ----------------------------
# Key expiry disabled is what an always-on host should be in, so a check that
# read null as "unknown" would nag forever about the correct configuration.
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "" ""
OUT="$(run_gather)"
assert_count "null Self.KeyExpiry is healthy" "$OUT" 0

# --- 7. self key 3 days out fires t7 ----------------------------------------
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "$(iso_in_days 3)" ""
OUT="$(run_gather)"
assert_count "self key at T-3 fires" "$OUT" 1
assert_issue "self key at T-3 fires as t7" "$OUT" "tailscale-self_t7"

# --- 8. an already-expired self key fires immediately -----------------------
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "$(iso_in_days -2)" ""
OUT="$(run_gather)"
assert_issue "an already-expired self key fires as expired" "$OUT" "tailscale-self_expired"

# --- 9. BackendState NeedsLogin is the outage state, not a skip -------------
# This is what an expired node key actually looks like from the CLI. Treating
# it as "not running, skip" is how the 2026-09-01 Tailscale expiry would have
# stayed invisible.
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "" "" "NeedsLogin"
OUT="$(run_gather)"
assert_issue "NeedsLogin fires as expired" "$OUT" "tailscale-self_expired"
assert_contains "NeedsLogin names the fix" "$(jq -r '.credentials[] | select(.id == "tailscale-self") | .detail' <<<"$OUT")" "tailscale up"

# --- 10. peers are checked too, keyed on their DNS label --------------------
# The laptop or the phone whose key expires is the operator's way back in.
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "" 1
OUT="$(run_gather)"
assert_issue "an expiring peer fires" "$OUT" "tailscale-peer-seans-macbook-pro_t1"
assert_contains "the peer keeps its human label" "$(jq -r '.credentials[] | select(.id | startswith("tailscale-peer")) | .label' <<<"$OUT")" "Sean's MacBook Pro"

# --- 11. a far-off peer key is silent ---------------------------------------
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "" 90
OUT="$(run_gather)"
assert_count "a peer 90 days out is silent" "$OUT" 0

# --- 12. suppression: the same stage does not re-fire on the next tick ------
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "$(iso_in_days 3)" ""
FIRST="$(run_gather)"
SECOND="$(run_gather)"
THIRD="$(run_gather)"
assert_count "first tick at T-3 fires" "$FIRST" 1
assert_count "second tick at T-3 is silent" "$SECOND" 0
assert_count "third tick at T-3 is silent" "$THIRD" 0
assert_status "a suppressed run says so" "$SECOND" "suppressed"
assert_contains "a suppressed run still reports the condition" "$(jq -c '.unhealthy' <<<"$SECOND")" "tailscale-self_t7"

# --- 13. THE regression this suite exists for: no volatile counter in the key
# `days_remaining` changes on every single run. If it reaches the state record
# or the signature, the cooldown never matches and the alert fires every tick.
STATE_BLOB="$(cat "$TMP/state.json")"
assert_not_contains "days_remaining never reaches the state file" "$STATE_BLOB" "days_remaining"
SIG_T3="$(jq -r '.alert_signature' <<<"$FIRST")"
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "$(iso_in_days 4)" ""
SIG_T4="$(jq -r '.alert_signature' <<<"$(run_gather)")"
if [[ "$SIG_T3" == "$SIG_T4" ]]; then ok
else bad "signature stability" "the signature moved when only days_remaining did (T-3 '$SIG_T3' vs T-4 '$SIG_T4', same t7 stage)"; fi

# --- 14. a stage TRANSITION does re-fire ------------------------------------
# T-7 fires once, silence, then T-1 fires once. Silence between the two is the
# design; silence THROUGH the second threshold would be the bug.
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "$(iso_in_days 5)" ""
OUT="$(run_gather)"
assert_issue "T-5 fires as t7" "$OUT" "tailscale-self_t7"
OUT="$(run_gather)"
assert_count "T-5 goes quiet on the next tick" "$OUT" 0
plant_tailscale "$(iso_in_days 1)" ""
OUT="$(run_gather)"
assert_issue "crossing into T-1 fires again" "$OUT" "tailscale-self_t1"

# --- 15. `expired` re-nags on the cooldown, `t7` never does -----------------
# An expired credential does not self-resolve, and an unanswered notification
# is not a dismissal. A 3-day re-nag on t7, though, would fire again at T-4,
# which is the daily nagging this design rules out.
reset
CHECKS_ENABLED="tailscale"
REALERT_DAYS="0"
plant_tailscale "$(iso_in_days -1)" ""
OUT="$(run_gather)"
assert_issue "an expired key fires" "$OUT" "tailscale-self_expired"
OUT="$(run_gather)"
assert_issue "an expired key re-nags once the cooldown clears" "$OUT" "tailscale-self_expired"

reset
CHECKS_ENABLED="tailscale"
REALERT_DAYS="0"
plant_tailscale "$(iso_in_days 3)" ""
OUT="$(run_gather)"
assert_count "t7 fires once" "$OUT" 1
OUT="$(run_gather)"
assert_count "t7 does NOT re-nag even with the cooldown at zero" "$OUT" 0

# --- 16. recovery clears the record, so a later re-degrade fires again ------
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "$(iso_in_days 3)" ""
run_gather >/dev/null
plant_tailscale "" ""
OUT="$(run_gather)"
assert_count "a recovered credential is silent" "$OUT" 0
if [[ "$(jq -r '.checks | keys | length' "$TMP/state.json")" == "0" ]]; then ok
else bad "recovery" "the state record was not cleared: $(cat "$TMP/state.json")"; fi
plant_tailscale "$(iso_in_days 3)" ""
OUT="$(run_gather)"
assert_count "a re-degraded credential fires again" "$OUT" 1

# --- 17. THE keychain case: token gone, first occurrence, no threshold ------
# This is the state the reference install sat in for four days while the
# bridge logged 288 identical warnings and told nobody.
reset
CHECKS_ENABLED="claude"
plant_keychain "" 30
plant_credfile "sk-ant-oat-SECRETVALUE" 30
OUT="$(run_gather)"
assert_issue "a keychain entry with no accessToken fires on the FIRST run" "$OUT" "claude-keychain_missing"
assert_contains "the keychain alert names the fix" "$(jq -r '.credentials[] | select(.id == "claude-keychain") | .detail' <<<"$OUT")" "/login"
assert_no_issue "the credentials file stays green while the keychain is broken" "$OUT" "claude-credentials-file_missing"

# --- 18. keychain entry absent entirely -------------------------------------
reset
CHECKS_ENABLED="claude"
plant_credfile "sk-ant-oat-SECRETVALUE" 30
OUT="$(run_gather)"
assert_issue "an absent keychain entry fires" "$OUT" "claude-keychain_missing"

# --- 19. keychain unreadable because of the launchd domain ------------------
# "User interaction is not allowed" is a LaunchDaemon in the Background
# session, not a lost token. Telling the operator to run /login would send
# them after the wrong thing.
reset
CHECKS_ENABLED="claude"
touch "$STUB_STATE/keychain.locked"
plant_credfile "sk-ant-oat-SECRETVALUE" 30
OUT="$(run_gather)"
assert_issue "a refused keychain read fires as unknown" "$OUT" "claude-keychain_unknown"
assert_contains "a refused read points at the launchd domain" "$(jq -r '.credentials[] | select(.id == "claude-keychain") | .detail' <<<"$OUT")" "launchd-domains.md"

# --- 20. credentials file missing or tokenless ------------------------------
reset
CHECKS_ENABLED="claude"
plant_keychain "sk-ant-oat-SECRETVALUE" 30
OUT="$(run_gather)"
assert_issue "an absent credentials file fires" "$OUT" "claude-credentials-file_missing"

reset
CHECKS_ENABLED="claude"
plant_keychain "sk-ant-oat-SECRETVALUE" 30
echo '{"mcpOAuth": {}}' > "$TMP/credentials.json"
OUT="$(run_gather)"
assert_issue "a credentials file with no accessToken fires" "$OUT" "claude-credentials-file_missing"

# --- 21. the refresh token gets the T-7 / T-1 ladder ------------------------
reset
CHECKS_ENABLED="claude"
plant_keychain "sk-ant-oat-SECRETVALUE" 4
plant_credfile "sk-ant-oat-SECRETVALUE" 40
OUT="$(run_gather)"
assert_issue "a refresh token 4 days out fires as t7" "$OUT" "claude-keychain_t7"
assert_no_issue "the file's own refresh token stays green" "$OUT" "claude-credentials-file_t7"

# --- 22. the ACCESS token does not get the ladder ---------------------------
# It lives about an hour and refreshes itself. Putting T-7 / T-1 on it would
# alert permanently, which is how a real monitor gets muted.
reset
CHECKS_ENABLED="claude"
plant_keychain "sk-ant-oat-SECRETVALUE" 40 1
plant_credfile "sk-ant-oat-SECRETVALUE" 40 1
OUT="$(run_gather)"
assert_count "an hour-long access token does not alarm" "$OUT" 0

reset
CHECKS_ENABLED="claude"
plant_keychain "sk-ant-oat-SECRETVALUE" 40 -2
plant_credfile "sk-ant-oat-SECRETVALUE" 40 -2
OUT="$(run_gather)"
assert_count "an access token 2h stale is inside the grace window" "$OUT" 0

reset
CHECKS_ENABLED="claude"
plant_keychain "sk-ant-oat-SECRETVALUE" 40 -48
plant_credfile "sk-ant-oat-SECRETVALUE" 40 -48
OUT="$(run_gather)"
assert_issue "a two-day-stale access token fires as a dead refresh loop" "$OUT" "claude-token-refresh_expired"

# --- 23. drop-in checks: array, NDJSON, epoch ms and ISO8601 ----------------
reset
CHECKS_ENABLED=""
cat > "$DROPIN/icloud.sh" <<DROPIN_EOF
#!/bin/bash
echo '[{"id": "icloud-trust-token", "label": "iCloud trust token", "expires_at": $(in_days 1), "detail": "renew with icloud-login"}]'
DROPIN_EOF
cat > "$DROPIN/vault.sh" <<DROPIN_EOF
#!/bin/bash
echo '{"id": "vault-session", "expires_at": $(in_days_ms 4), "detail": "epoch ms"}'
echo '{"id": "cert", "expires_at": "$(iso_in_days 5)", "detail": "iso8601"}'
DROPIN_EOF
chmod +x "$DROPIN/icloud.sh" "$DROPIN/vault.sh"
OUT="$(run_gather)"
assert_issue "a drop-in array record fires" "$OUT" "icloud-trust-token_t1"
assert_issue "a drop-in NDJSON record in epoch ms fires" "$OUT" "vault-session_t7"
assert_issue "a drop-in ISO8601 expiry fires" "$OUT" "cert_t7"

# --- 24. a drop-in that cannot run reports itself ---------------------------
# A credential check that silently fails is a credential nobody is watching.
reset
CHECKS_ENABLED=""
cat > "$DROPIN/broken.sh" <<'DROPIN_EOF'
#!/bin/bash
echo "this is not json"
exit 1
DROPIN_EOF
chmod +x "$DROPIN/broken.sh"
OUT="$(run_gather)"
assert_issue "a broken drop-in surfaces as unknown" "$OUT" "broken_unknown"

# --- 25. a drop-in state outside the vocabulary is clamped ------------------
reset
CHECKS_ENABLED=""
cat > "$DROPIN/weird.sh" <<'DROPIN_EOF'
#!/bin/bash
echo '{"id": "weird", "state": "catastrophe", "detail": "not a known state"}'
DROPIN_EOF
chmod +x "$DROPIN/weird.sh"
OUT="$(run_gather)"
assert_issue "an unrecognised state clamps to unknown" "$OUT" "weird_unknown"

# --- 26. a drop-in reporting ok is silent -----------------------------------
reset
CHECKS_ENABLED=""
cat > "$DROPIN/fine.sh" <<'DROPIN_EOF'
#!/bin/bash
echo '{"id": "fine", "state": "ok", "detail": "all good"}'
DROPIN_EOF
chmod +x "$DROPIN/fine.sh"
OUT="$(run_gather)"
assert_count "a drop-in reporting ok is silent" "$OUT" 0

# --- 27. per-check state, so two runners with different visibility coexist --
# The in-container heartbeat cannot see the keychain or the tailnet; the host
# runner can. With one whole-run fingerprint the two would alternate and alert
# on every tick. Per-check records mean a run that cannot see a check leaves
# that check's record alone.
reset
CHECKS_ENABLED="tailscale,claude"
plant_tailscale "$(iso_in_days 3)" ""
plant_keychain "sk-ant-oat-SECRETVALUE" 30
plant_credfile "sk-ant-oat-SECRETVALUE" 30
run_gather >/dev/null                      # host-shaped run: fires on tailscale
CHECKS_ENABLED="claude"                    # container-shaped run: no tailnet
TS_BIN_OVERRIDE="/nonexistent/tailscale"
OUT="$(run_gather)"
assert_count "a narrower run does not re-fire what it cannot see" "$OUT" 0
if [[ "$(jq -r '.checks["tailscale-self"].stage' "$TMP/state.json")" == "t7" ]]; then ok
else bad "cross-runner state" "the narrower run dropped the other runner's record: $(cat "$TMP/state.json")"; fi
CHECKS_ENABLED="tailscale,claude"
TS_BIN_OVERRIDE=""
OUT="$(run_gather)"
assert_count "the wider run still sees it as suppressed" "$OUT" 0

# --- 28. a corrupt state file fails open ------------------------------------
# Silence is the failure mode this gather exists to stop, so an unreadable
# state file must re-alert rather than mute.
reset
CHECKS_ENABLED="tailscale"
plant_tailscale "$(iso_in_days 3)" ""
run_gather >/dev/null
printf 'not json at all' > "$TMP/state.json"
OUT="$(run_gather)"
assert_count "a corrupt state file re-alerts rather than muting" "$OUT" 1

echo ""
echo "test-credential-expiry: $pass passed, $fail failed"
[[ $fail -eq 0 ]] || exit 1
exit 0
