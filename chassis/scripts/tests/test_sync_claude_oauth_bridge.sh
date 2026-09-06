#!/usr/bin/env bash
# Tests for sync-claude-oauth-bridge.sh, and specifically for the rescue path
# added in new-jaxity#559.
#
# The script is the only thing keeping both the host keychain and the chassis
# container authenticated, so the cases that matter are the ones where one side
# is broken. Case 3 is the load-bearing one: when BOTH sides have lost their
# token, KC_EXPIRES and FILE_EXPIRES are both 0, `0 -lt 0` is false, and a
# naive "just delete the guard" fix would fall through to the forward-sync and
# write the broken keychain JSON over the container's credential file.
#
# `security` is stubbed by a shell script earlier on PATH that reads and writes
# a plain file, so no real keychain is touched. CHASSIS_ALERT_CMD is stubbed to
# `true` so a test run cannot post to the operator's alert channel - running
# this suite against the live install otherwise delivers real "the keychain
# lost its token" alerts, which is exactly the false alarm the alerting work in
# #550 was trying to eliminate.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${SCRIPT_DIR}/../sync-claude-oauth-bridge.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/home/.claude" "$WORK/logs" "$WORK/state"

cat > "$WORK/bin/security" <<'STUB'
#!/bin/bash
KC="$(dirname "$(dirname "$0")")/kc.json"
case "$1" in
  find-generic-password) [[ -s "$KC" ]] && cat "$KC" || exit 44 ;;
  add-generic-password)
    for ((i=1;i<=$#;i++)); do
      [[ "${!i}" == "-w" ]] && { j=$((i+1)); printf '%s' "${!j}" > "$KC"; exit 0; }
    done
    exit 1 ;;
esac
STUB
chmod +x "$WORK/bin/security"

NOW_S=$(date -u +%s)
FUTURE=$(( (NOW_S + 7200) * 1000 ))
PAST=$(( (NOW_S - 7200) * 1000 ))

FAILURES=0

# $1 name, $2 keychain json, $3 file json, $4 expected log substring,
# $5 expected keychain accessToken, $6 expected file accessToken
run_case() {
    local name="$1" kc="$2" file="$3" want_log="$4" want_kc="$5" want_file="$6"
    : > "$WORK/logs/claude-oauth-bridge-sync.log"
    rm -f "$WORK/state/alert.state"
    printf '%s' "$kc" > "$WORK/kc.json"
    printf '%s' "$file" > "$WORK/home/.claude/.credentials.json"

    PATH="$WORK/bin:$PATH" HOME="$WORK/home" LOG_DIR="$WORK/logs" \
        CHASSIS_OAUTH_BRIDGE_ALERT_STATE="$WORK/state/alert.state" \
        CHASSIS_ALERT_CMD="true" KEYCHAIN_ACCOUNT="tester" \
        bash "$TARGET"

    local got_log got_kc got_file
    got_log="$(cat "$WORK/logs/claude-oauth-bridge-sync.log")"
    got_kc="$(jq -r '.claudeAiOauth.accessToken // "NONE"' "$WORK/kc.json")"
    got_file="$(jq -r '.claudeAiOauth.accessToken // "NONE"' "$WORK/home/.claude/.credentials.json")"

    local ok=1
    [[ "$got_log" == *"$want_log"* ]] || { echo "  log: want *${want_log}*, got: ${got_log}"; ok=0; }
    [[ "$got_kc" == "$want_kc" ]] || { echo "  keychain accessToken: want ${want_kc}, got ${got_kc}"; ok=0; }
    [[ "$got_file" == "$want_file" ]] || { echo "  file accessToken: want ${want_file}, got ${got_file}"; ok=0; }

    if (( ok )); then
        echo "PASS  $name"
    else
        echo "FAIL  $name"
        FAILURES=$(( FAILURES + 1 ))
    fi
}

run_case "empty keychain, fresh file token: rescued from the container" \
    '{"claudeAiOauth":{"expiresAt":0},"mcpOAuth":{}}' \
    "{\"claudeAiOauth\":{\"accessToken\":\"GOOD\",\"refreshToken\":\"R1\",\"expiresAt\":${FUTURE}}}" \
    "rescued: keychain had no accessToken" "GOOD" "GOOD"

run_case "empty keychain, expired file token: alert, no rescue write" \
    '{"claudeAiOauth":{"expiresAt":0},"mcpOAuth":{}}' \
    "{\"claudeAiOauth\":{\"accessToken\":\"STALE\",\"refreshToken\":\"R1\",\"expiresAt\":${PAST}}}" \
    "keychain JSON missing claudeAiOauth.accessToken" "NONE" "STALE"

run_case "both sides empty: alert, and the file is NOT clobbered" \
    '{"claudeAiOauth":{"expiresAt":0},"mcpOAuth":{}}' \
    '{"claudeAiOauth":{"expiresAt":0},"mcpOAuth":{"keep":"me"}}' \
    "keychain JSON missing claudeAiOauth.accessToken" "NONE" "NONE"

run_case "healthy keychain newer than file: normal forward sync" \
    "{\"claudeAiOauth\":{\"accessToken\":\"KCNEW\",\"refreshToken\":\"R2\",\"expiresAt\":${FUTURE}}}" \
    "{\"claudeAiOauth\":{\"accessToken\":\"OLD\",\"refreshToken\":\"R1\",\"expiresAt\":${PAST}}}" \
    "synced: keychain expiresAt" "KCNEW" "KCNEW"

run_case "file newer than keychain: reverse sync, container wins" \
    "{\"claudeAiOauth\":{\"accessToken\":\"KCOLD\",\"refreshToken\":\"R1\",\"expiresAt\":${PAST}}}" \
    "{\"claudeAiOauth\":{\"accessToken\":\"FILENEW\",\"refreshToken\":\"R2\",\"expiresAt\":${FUTURE}}}" \
    "reverse-synced" "FILENEW" "FILENEW"

echo
if (( FAILURES )); then
    echo "${FAILURES} failure(s)"
    exit 1
fi
echo "all cases passed"
