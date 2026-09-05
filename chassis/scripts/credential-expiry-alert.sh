#!/usr/bin/env bash
# credential-expiry-alert.sh - run the credential-expiry gather and post the
# result straight to the ops channel. No model, no dispatcher.
#
# Why this exists alongside the heartbeat (new-jaxity#550)
# =======================================================
# The heartbeat runs inside the chassis container, where there is no macOS
# Keychain and no tailscaled, so the two checks that matter most on a
# containerized install are invisible to it. Worse, the heartbeat's delivery
# path is `claude -p`, and one of the credentials being watched is Claude's
# own: on 2026-09-01 the host Keychain lost `claudeAiOauth.accessToken` and
# any alert routed through the host interactive session would have been
# routed through the thing that had just broken.
#
# So this runner is the belt to the heartbeat's braces. It shells the same
# gather, and when the gather reports something firing it posts a plain
# webhook message. Nothing in that path needs Claude, ssh, Tailscale or a
# terminal.
#
# Wiring it (macOS)
# =================
# A LaunchAgent, NOT a LaunchDaemon. `security find-generic-password` cannot
# unlock the login keychain from launchd's Background session and fails with
# "User interaction is not allowed", which would turn the Keychain check into
# a permanent false alarm. See `docs/launchd-domains.md`.
#
#   ~/Library/LaunchAgents/com.behalfbot.<bot>-credential-expiry.plist
#     ProgramArguments: /bin/bash <chassis>/scripts/credential-expiry-alert.sh
#     StartInterval:    3600
#     EnvironmentVariables: CUSTOMER_HOME=<customer home>
#
# Wiring it (Linux)
# =================
# A user systemd timer, or a cron line:
#
#   17 * * * * CUSTOMER_HOME=$HOME/.behalfbot /bin/bash <chassis>/scripts/credential-expiry-alert.sh
#
# Flags
# =====
#   --dry-run   print the message that would be posted, post nothing, and
#               leave the gather's suppression state untouched.
#
# Exit codes
# ==========
#   0  nothing to say, or the alert went out
#   1  the gather could not run
#   2  there was something to say and delivery failed (check the log, and
#      check that the webhook is configured)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=chassis/scripts/_alert.sh
source "${SCRIPT_DIR}/_alert.sh"

: "${CUSTOMER_HOME:=${CHASSIS_HOME:-${HOME}/.behalfbot}}"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

GATHER="${SCRIPT_DIR}/gather-credential-expiry.sh"
if [[ ! -f "$GATHER" ]]; then
    echo "credential-expiry-alert: gather not found at $GATHER" >&2
    exit 1
fi

LOG_DIR="${LOG_DIR:-${CUSTOMER_HOME}/logs/scheduled}"
LOG="${LOG_DIR}/credential-expiry-alert.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true
log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG" 2>/dev/null || true; }

if (( DRY_RUN )); then
    # A dry run must not consume the transition it is previewing, so it grades
    # against a throwaway state file.
    DRY_STATE="$(mktemp)"
    if [[ -f "${CHASSIS_CREDENTIAL_STATE:-${CUSTOMER_HOME}/scheduled-tasks/credential-expiry-state.json}" ]]; then
        cp "${CHASSIS_CREDENTIAL_STATE:-${CUSTOMER_HOME}/scheduled-tasks/credential-expiry-state.json}" "$DRY_STATE" 2>/dev/null || true
    fi
    OUT="$(CHASSIS_CREDENTIAL_STATE="$DRY_STATE" bash "$GATHER")"
    GATHER_RC=$?
    rm -f "$DRY_STATE"
else
    OUT="$(bash "$GATHER")"
    GATHER_RC=$?
fi

if (( GATHER_RC != 0 )) || [[ -z "$OUT" ]]; then
    log "ERROR: gather exited ${GATHER_RC} with no usable output"
    echo "credential-expiry-alert: gather failed" >&2
    exit 1
fi

COUNT="$(jq -r '.count // 0' <<<"$OUT" 2>/dev/null)" || COUNT=0
[[ "$COUNT" =~ ^[0-9]+$ ]] || COUNT=0

if (( COUNT == 0 )); then
    if (( DRY_RUN )); then
        echo "Nothing firing. status=$(jq -r '.status' <<<"$OUT"), checked=$(jq -r '.checked' <<<"$OUT")"
        jq -r '.credentials[] | "  \(.stage)\t\(.id)\t\(.days_remaining // "-")d\t\(.label)"' <<<"$OUT"
    fi
    exit 0
fi

# One line per firing credential, ordered worst first. Kept short on purpose:
# Discord truncates at 2000 characters and this helper does not split.
MESSAGE="$(jq -r '
    def rank: {"expired": 0, "missing": 1, "unknown": 2, "t1": 3, "warn": 4, "t7": 5}[.stage] // 9;
    def line:
        (if .stage == "expired" then "EXPIRED"
         elif .stage == "missing" then "MISSING"
         elif .stage == "unknown" then "UNKNOWN - the check itself could not run"
         elif .stage == "t1" then "expires in under a day"
         elif .stage == "t7" then "expires in " + ((.days_remaining // 0) | tostring) + " days"
         else "needs attention" end) as $what
        | "- " + .label + " (" + .id + "): " + $what + "\n  " + .detail;
    . as $root
    | ($root.issues) as $issues
    | ($root.credentials
       | map(. as $c | select(($issues | index($c.id + "_" + $c.stage)) != null))
       | sort_by(rank)) as $firing
    | ($firing | length) as $n
    | "**Credential expiry alert** - " + ($n | tostring)
      + (if $n == 1 then " credential needs" else " credentials need" end)
      + " attention on this host.\n\n"
      + ($firing | map(line) | join("\n"))
      + "\n\nNo model ran to produce this. Fix the credential and the next run goes quiet on its own."
    ' <<<"$OUT" 2>/dev/null)"

if [[ -z "$MESSAGE" ]]; then
    MESSAGE="**Credential expiry alert** - $(jq -r '.issues | join(", ")' <<<"$OUT")"
fi

# Discord's own ceiling. Truncating here beats a silent server-side reject.
if (( ${#MESSAGE} > 1900 )); then
    MESSAGE="${MESSAGE:0:1880}"$'\n'"... (truncated)"
fi

if (( DRY_RUN )); then
    printf '%s\n' "$MESSAGE"
    exit 0
fi

if chassis_alert "$MESSAGE" >/dev/null 2>&1; then
    log "alerted: $(jq -c '.issues' <<<"$OUT")"
    exit 0
fi

log "ERROR: delivery failed for $(jq -c '.issues' <<<"$OUT") - is the ops webhook configured?"
echo "credential-expiry-alert: delivery failed" >&2
exit 2
