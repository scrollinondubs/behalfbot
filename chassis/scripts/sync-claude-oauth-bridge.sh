#!/usr/bin/env bash
# sync-claude-oauth-bridge.sh — push macOS Keychain Claude Code OAuth
# state into ~/.claude/.credentials.json so the chassis container (which has
# no macOS Keychain access) can authenticate.
#
# Background:
# ============
# On macOS, Claude Code stores OAuth tokens (accessToken, refreshToken,
# expiresAt, scopes) in the login Keychain under the service
# `Claude Code-credentials`, account `${USER}` (the host login account name).
# The on-disk ~/.claude/.credentials.json file is only populated with `mcpOAuth` entries
# (MCP server tokens), not the Anthropic auth itself. Host `claude` invocations
# work because the binary shells out to `security find-generic-password` to
# pull the access token from Keychain.
#
# The chassis container is Linux and has no Keychain — `security` doesn't
# exist. The container previously had NO working path to authenticate, which
# meant every `claude -p` from the in-container dispatcher failed with
# "Not logged in" and every heartbeat that invoked claude (morning-briefing,
# github-issue-triage, pg-backup, daily-log, strava-ingest, etc.) tripped
# the circuit breaker. Cutover regression: nothing fired post-cutover.
#
# This script is the bridge: it reads the Keychain JSON and writes it to
# ~/.claude/.credentials.json. The chassis container bind-mounts ~/.claude/
# at /home/chassis/.claude/, so the moment this file lands on the host the
# container's claude can read it. No container restart required.
#
# Conflict handling:
# ==================
# Both sides can refresh the access token (host via Keychain, container via
# OAuth refresh against `.credentials.json`). The refresh_token is long-lived
# and the same on both sides, so either side's refresh produces a valid token.
# Race: if the container refreshes first and writes a NEWER expiresAt to the
# file, this script would clobber it on next sync. To avoid that, we only
# write when:
#   - the file doesn't exist or has no claudeAiOauth section, OR
#   - the keychain's expiresAt is >= the file's expiresAt
# This makes whichever side has the newer token authoritative.
#
# launchd-driven, see com.<assistant>.claude-credentials-bridge-sync.plist for the
# schedule (every 30 min - access tokens last ~1h, so we refresh well before
# expiry).
#
# Alerting (new-jaxity#550):
# ==========================
# This script has always DETECTED the Keychain losing its Claude token. It
# logged it and told nobody. Between 2026-09-01 and 2026-09-05 it wrote
# `WARN: keychain JSON missing claudeAiOauth.accessToken` 288 times into
# `claude-oauth-bridge-sync.log` while the operator was off-grid and the host
# interactive session was dead. 48 is the ceiling for a job running every 30
# minutes, and three consecutive days hit it. A monitor that fires into a log
# nobody reads is not a monitor.
#
# So the WARN paths now post once, on the FIRST tick after a healthy state,
# and then stay silent until the condition clears - at which point they post a
# recovery notice. Edge-triggered, not level-triggered: 288 identical webhook
# messages would be exactly as useless as 288 identical log lines.
#
# Delivery goes through `_alert.sh` (webhook, no model), because the credential
# in question is Claude's own. Routing this alert through `claude -p` would
# route it through the thing that just broke.
#
# State lives in one file holding the last-alerted condition:
#   CHASSIS_OAUTH_BRIDGE_ALERT_STATE   default
#     $CUSTOMER_HOME/state/claude-oauth-bridge-alert.state
# Delivery is configured by CHASSIS_ALERT_CHANNEL / CHASSIS_ALERT_CMD, see
# `_alert.sh`. An install with no webhook configured logs the delivery failure
# and carries on - alerting must never break syncing.

set -euo pipefail

CRED_FILE="${HOME}/.claude/.credentials.json"
TMP="${CRED_FILE}.tmp"
LOG_DIR="${LOG_DIR:-${CUSTOMER_HOME:-${HOME}/.behalfbot}/logs/scheduled}"
LOG="${LOG_DIR}/claude-oauth-bridge-sync.log"

mkdir -p "$LOG_DIR" "$(dirname "$CRED_FILE")"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Guarded, and the guard is load-bearing. This script runs under
# `set -euo pipefail` every 30 minutes and is the ONLY thing keeping the
# container authenticated. An unguarded `source` of a sibling would kill the
# sync outright on any install running a copy of this file from somewhere
# else - making the most critical script in the install more fragile, in a
# change about resilience. Missing helper degrades to the old log-only
# behaviour instead.
if [[ -f "${SCRIPT_DIR}/_alert.sh" ]]; then
    # shellcheck source=chassis/scripts/_alert.sh
    source "${SCRIPT_DIR}/_alert.sh"
else
    chassis_alert() { return 1; }
fi

ALERT_STATE_FILE="${CHASSIS_OAUTH_BRIDGE_ALERT_STATE:-${CUSTOMER_HOME:-${HOME}/.behalfbot}/state/claude-oauth-bridge-alert.state}"

alert_state() {
    [[ -f "$ALERT_STATE_FILE" ]] || { printf 'ok'; return 0; }
    local v
    v="$(cat "$ALERT_STATE_FILE" 2>/dev/null)" || v=""
    printf '%s' "${v:-ok}"
}

set_alert_state() {
    mkdir -p "$(dirname "$ALERT_STATE_FILE")" 2>/dev/null || true
    printf '%s' "$1" > "$ALERT_STATE_FILE" 2>/dev/null || true
}

# Post once on the transition INTO a fault, then go quiet. The state is
# recorded whether or not delivery succeeded: an install with no webhook
# configured would otherwise retry every 30 minutes forever, which is the same
# noise this change exists to remove. The delivery failure is logged instead.
alert_fault() {
    local condition="$1" message="$2"
    [[ "$(alert_state)" == "$condition" ]] && return 0
    set_alert_state "$condition"
    if chassis_alert "$message" >/dev/null 2>&1; then
        log "ALERT sent: $condition"
    else
        log "WARN: could not deliver the '$condition' alert - is the ops webhook configured?"
    fi
    return 0
}

# Called on every healthy tick. Silent unless the previous tick was a fault,
# so a bridge that has never failed never posts anything.
clear_fault() {
    local prev
    prev="$(alert_state)"
    [[ "$prev" == "ok" ]] && return 0
    set_alert_state "ok"
    if chassis_alert "**Claude oauth bridge recovered.** The keychain entry \`Claude Code-credentials\` carries a valid \`claudeAiOauth.accessToken\` again, so the host interactive session can authenticate. Previous fault: \`${prev}\`." >/dev/null 2>&1; then
        log "ALERT sent: recovered from $prev"
    else
        log "WARN: could not deliver the recovery notice for '$prev'"
    fi
    return 0
}

# Read Keychain entry. -w prints the password (which is JSON for this entry).
# Account = host user (the login account Claude Code's OAuth flow saves under).
KEYCHAIN_ACCOUNT="${KEYCHAIN_ACCOUNT:-${USER}}"
if ! KC_JSON=$(security find-generic-password -s "Claude Code-credentials" -a "$KEYCHAIN_ACCOUNT" -w 2>/dev/null); then
    log "WARN: keychain read failed (entry missing or locked) - leaving file untouched"
    alert_fault "keychain_unreadable" "**Claude oauth bridge: the macOS keychain entry cannot be read.** \`security find-generic-password -s 'Claude Code-credentials'\` failed, so the entry is missing, the login keychain is locked, or this job is running in launchd's Background session (see \`docs/launchd-domains.md\`). Host interactive \`claude\` cannot authenticate. The chassis container keeps working while its own refresh token holds. You will hear from this again only when it recovers."
    exit 0
fi

# Validate Keychain JSON has claudeAiOauth.accessToken (else useless).
KC_ACCESS=$(jq -r '.claudeAiOauth.accessToken // empty' <<<"$KC_JSON" 2>/dev/null || true)
if [[ -z "$KC_ACCESS" ]]; then
    log "WARN: keychain JSON missing claudeAiOauth.accessToken - leaving file untouched"
    alert_fault "keychain_missing_token" "**Claude oauth bridge: the macOS keychain lost its Claude access token.** The entry \`Claude Code-credentials\` is present but carries no \`claudeAiOauth.accessToken\`, so host interactive \`claude\` cannot authenticate until someone runs \`/login\`. The chassis container is unaffected while its own refresh token holds. This is the exact state that went unreported for four days in new-jaxity#550. You will hear from this again only when it recovers."
    exit 0
fi

# The healthy boundary. This must sit ABOVE the reverse-sync and the
# identical-hash early exits: both of them `exit 0` on a perfectly healthy
# keychain, and a recovery notice placed below them would never post on the
# common path.
clear_fault

KC_EXPIRES=$(jq -r '.claudeAiOauth.expiresAt // 0' <<<"$KC_JSON")

# Compare against current file state.
FILE_EXPIRES=0
if [[ -f "$CRED_FILE" ]]; then
    FILE_EXPIRES=$(jq -r '.claudeAiOauth.expiresAt // 0' "$CRED_FILE" 2>/dev/null || echo 0)
fi

# When the FILE is newer (container refreshed via the bind-mounted file),
# push file → Keychain so host claude doesn't keep trying with a stale
# refresh_token that Anthropic already rotated out. Without this reverse
# direction, the daily 5am discord-restart cron's `claude --print` probe on
# host hits 401 every morning because the refresh_token in Keychain was
# invalidated by the container's prior overnight refresh. (<v1-reference-install>#86,
# new-jaxity#86 root cause analysis 2026-05-26.)
if [[ "$KC_EXPIRES" -lt "$FILE_EXPIRES" ]]; then
    # Read the file's full JSON (must include claudeAiOauth or we'd be writing
    # garbage to Keychain).
    if [[ ! -f "$CRED_FILE" ]]; then
        log "skip: file expiresAt > keychain but file vanished mid-run"
        exit 0
    fi
    # Compact the JSON before writing to Keychain. `security
    # add-generic-password -w` takes the password as a single argument; pretty-
    # printed JSON with embedded newlines round-trips badly via the security
    # CLI (`find-generic-password -w` reads back as truncated/corrupt). Claude
    # Code itself writes compact JSON, so this matches the canonical format.
    FILE_JSON=$(jq -c '.' "$CRED_FILE" 2>/dev/null)
    if [[ -z "$FILE_JSON" ]]; then
        log "skip: file expiresAt > keychain but file is not valid JSON"
        exit 0
    fi
    FILE_ACCESS=$(jq -r '.claudeAiOauth.accessToken // empty' <<<"$FILE_JSON" 2>/dev/null || true)
    if [[ -z "$FILE_ACCESS" ]]; then
        log "skip: file expiresAt > keychain but file has no claudeAiOauth.accessToken"
        exit 0
    fi
    # security add-generic-password -U updates the existing item in place,
    # preserving the service/account labels.
    if security add-generic-password -U -s "Claude Code-credentials" -a "$KEYCHAIN_ACCOUNT" -w "$FILE_JSON" 2>/dev/null; then
        log "reverse-synced: file expiresAt=$FILE_EXPIRES → keychain (was $KC_EXPIRES)"
        exit 0
    else
        log "WARN: reverse-sync failed (security add-generic-password -U exit non-zero) — leaving keychain untouched"
        exit 0
    fi
fi

# Skip identical writes to avoid log churn.
if [[ -f "$CRED_FILE" ]]; then
    KC_HASH=$(echo -n "$KC_JSON" | shasum -a 256 | awk '{print $1}')
    FILE_HASH=$(shasum -a 256 "$CRED_FILE" | awk '{print $1}')
    if [[ "$KC_HASH" == "$FILE_HASH" ]]; then
        exit 0
    fi
fi

# Atomic write with 0600 perms.
umask 077
printf '%s' "$KC_JSON" > "$TMP"
mv "$TMP" "$CRED_FILE"
chmod 600 "$CRED_FILE"
log "synced: keychain expiresAt=$KC_EXPIRES → file (was $FILE_EXPIRES)"
