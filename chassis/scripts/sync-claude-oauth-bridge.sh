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
# schedule (every 30 min — access tokens last ~1h, so we refresh well before
# expiry).

set -euo pipefail

CRED_FILE="${HOME}/.claude/.credentials.json"
TMP="${CRED_FILE}.tmp"
LOG_DIR="${LOG_DIR:-${HOME}/work/new-jaxity/logs/scheduled}"
LOG="${LOG_DIR}/claude-oauth-bridge-sync.log"

mkdir -p "$LOG_DIR" "$(dirname "$CRED_FILE")"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

# Read Keychain entry. -w prints the password (which is JSON for this entry).
# Account = host user (the login account Claude Code's OAuth flow saves under).
KEYCHAIN_ACCOUNT="${KEYCHAIN_ACCOUNT:-${USER}}"
if ! KC_JSON=$(security find-generic-password -s "Claude Code-credentials" -a "$KEYCHAIN_ACCOUNT" -w 2>/dev/null); then
    log "WARN: keychain read failed (entry missing or locked) — leaving file untouched"
    exit 0
fi

# Validate Keychain JSON has claudeAiOauth.accessToken (else useless).
KC_ACCESS=$(jq -r '.claudeAiOauth.accessToken // empty' <<<"$KC_JSON" 2>/dev/null || true)
if [[ -z "$KC_ACCESS" ]]; then
    log "WARN: keychain JSON missing claudeAiOauth.accessToken — leaving file untouched"
    exit 0
fi

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
