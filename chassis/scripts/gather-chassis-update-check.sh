#!/bin/bash
# gather-chassis-update-check.sh - weekly gate for the chassis-update-check heartbeat.
#
# Issue: scrollinondubs/behalfbot#33 (Apple-style chassis auto-updater).
#
# Compares local chassis/VERSION against upstream main and emits JSON with the
# version delta. Fires Claude only when:
#   1. Customer is behind by at least one version, AND
#   2. The latest available version is not in the dismissed list, AND
#   3. auto_update.check is true in chassis.config.yaml (default true), AND
#   4. the same version was not already offered inside the cooldown window
#
# Gate 4 (dismissed) is permanent: the operator typed `skip update` and meant
# it. Gate 5 (already offered) is a COOLDOWN, not a mute - an unanswered
# notification is not a dismissal (#146).
#
# Cheap by design: one HTTP GET against raw.githubusercontent.com for VERSION,
# plus one for CHANGELOG.md when behind. No paid API calls.

set -uo pipefail

CHASSIS_HOME="${CHASSIS_HOME:?CHASSIS_HOME must be set}"
CUSTOMER_HOME="${CUSTOMER_HOME:-${HOME}/.behalfbot}"

# Resolve VERSION + CHANGELOG paths RELATIVE TO THIS SCRIPT, not $CHASSIS_HOME.
# Survives both install layouts:
#   - vendored-subtree (chassis lives at ${CHASSIS_HOME}/chassis/)
#   - overlay-mount (Jax-style #136, chassis lives at ${CHASSIS_HOME}/chassis/chassis/)
# Either way, this script is at <chassis>/scripts/, so VERSION is at ../VERSION.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_VERSION_FILE="${SCRIPT_DIR}/../VERSION"
UPSTREAM_RAW_BASE="${CHASSIS_UPDATE_RAW_BASE:-https://raw.githubusercontent.com/scrollinondubs/behalfbot/main}"
UPSTREAM_VERSION_URL="${UPSTREAM_RAW_BASE}/chassis/VERSION"
UPSTREAM_CHANGELOG_URL="${UPSTREAM_RAW_BASE}/chassis/CHANGELOG.md"
CONFIG_FILE="${CHASSIS_HOME}/chassis.config.yaml"
STATE_DIR="${CUSTOMER_HOME}/state/chassis-update"
DISMISSED_FILE="${STATE_DIR}/dismissed.json"
LAST_OFFERED_FILE="${STATE_DIR}/last-offered.json"

# How long a single offer suppresses re-offers of the SAME version.
#
# Six days, not seven, and the difference matters. The heartbeat is
# `weekly monday 09:00` and the dispatcher fires at the first 15-minute tick
# at or after that time, once per day-of-week. Consecutive checks are
# therefore ~7 days apart give or take the tick alignment and any DST shift.
# A 7-day cooldown races that: a tick landing a few minutes "early" leaves
# the delta just under the window, the check stays silent, and the
# once-per-day-of-week guard pushes the next attempt out a full week. A
# weekly re-nag silently becomes fortnightly. Six days clears deterministically
# before every weekly tick.
#
# Override with CHASSIS_UPDATE_OFFER_COOLDOWN_DAYS (used by the test suite).
OFFER_COOLDOWN_DAYS="${CHASSIS_UPDATE_OFFER_COOLDOWN_DAYS:-6}"

mkdir -p "$STATE_DIR"

emit_skip() {
    local reason="$1"
    printf '{"count": 0, "reason": "%s"}\n' "$reason"
    exit 0
}

# --- Gate 1: config opt-out ---
# Default ON. Only skip when chassis.config.yaml explicitly sets
# auto_update.check to false. Simple grep is enough: yq isn't a chassis dep.
if [[ -f "$CONFIG_FILE" ]]; then
    # Match `check: false` (or `check:false`) under an auto_update block.
    # Awk walks the file with a flag set when we see `auto_update:` and clear
    # when a new top-level key appears.
    CHECK_DISABLED=$(awk '
        /^auto_update:/ { in_block = 1; next }
        /^[a-z_]+:/ && in_block { in_block = 0 }
        in_block && /^[[:space:]]+check:[[:space:]]*false/ { print "1"; exit }
    ' "$CONFIG_FILE")
    if [[ "$CHECK_DISABLED" == "1" ]]; then
        emit_skip "auto_update_check_disabled"
    fi
fi

# --- Gate 2: local VERSION file exists ---
if [[ ! -f "$LOCAL_VERSION_FILE" ]]; then
    # First install or pre-versioned chassis. Don't notify - the next chassis
    # update via subtree pull will install VERSION and unblock this.
    emit_skip "local_version_missing"
fi

LOCAL_VERSION=$(tr -d '[:space:]' < "$LOCAL_VERSION_FILE")
if [[ -z "$LOCAL_VERSION" ]]; then
    emit_skip "local_version_empty"
fi

# --- Gate 3: fetch upstream VERSION (cheap, no auth) ---
UPSTREAM_VERSION=$(curl --silent --fail --max-time 10 "$UPSTREAM_VERSION_URL" 2>/dev/null | tr -d '[:space:]')
if [[ -z "$UPSTREAM_VERSION" ]]; then
    # Network glitch / GitHub raw outage. Stay silent rather than nag.
    emit_skip "upstream_unreachable"
fi

# --- Compare semver ---
# Returns: -1 if a<b, 0 if a==b, 1 if a>b
semver_cmp() {
    local a="$1" b="$2"
    local IFS=.
    local -a a_parts=($a) b_parts=($b)
    for i in 0 1 2; do
        local av="${a_parts[$i]:-0}"
        local bv="${b_parts[$i]:-0}"
        if (( av < bv )); then echo -1; return; fi
        if (( av > bv )); then echo 1; return; fi
    done
    echo 0
}

CMP=$(semver_cmp "$LOCAL_VERSION" "$UPSTREAM_VERSION")
if [[ "$CMP" != "-1" ]]; then
    # Caught up or ahead of upstream. Silent.
    emit_skip "up_to_date"
fi

# --- Gate 4: not dismissed ---
if [[ -f "$DISMISSED_FILE" ]]; then
    DISMISSED=$(jq -r --arg v "$UPSTREAM_VERSION" 'index($v) // empty' "$DISMISSED_FILE" 2>/dev/null || echo "")
    if [[ -n "$DISMISSED" ]]; then
        emit_skip "dismissed"
    fi
fi

# --- Gate 5: not offered inside the cooldown window ---
# Reads the `offered_at` the emit step below has always written. Before #146
# this gate matched on version alone, which made a single unanswered
# notification a permanent mute for that version - the installer who was busy
# the week they were offered an update never heard about it again.
#
# Timestamp parsing is deliberately fail-open: an absent, corrupt or
# future-dated `offered_at` re-offers rather than staying silent. Silence is
# the failure mode this gate exists to stop.
iso_to_epoch() {
    local ts="$1" out=""
    # An empty string is not a timestamp, and GNU date happily reads it as
    # midnight today (exit 0), which would mute a state file that has no
    # offered_at at all. Reject it before date sees it.
    [[ -z "$ts" ]] && return
    # GNU date (chassis container), then BSD date (macOS host).
    out=$(date -u -d "$ts" +%s 2>/dev/null) \
        || out=$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$ts" +%s 2>/dev/null) \
        || out=""
    printf '%s' "$out"
}

if [[ -f "$LAST_OFFERED_FILE" ]]; then
    LAST_OFFERED=$(jq -r '.version // empty' "$LAST_OFFERED_FILE" 2>/dev/null || echo "")
    LAST_OFFERED_AT=$(jq -r '.offered_at // empty' "$LAST_OFFERED_FILE" 2>/dev/null || echo "")
    if [[ "$LAST_OFFERED" == "$UPSTREAM_VERSION" ]]; then
        OFFERED_EPOCH=$(iso_to_epoch "$LAST_OFFERED_AT")
        if [[ "$OFFERED_EPOCH" =~ ^[0-9]+$ ]]; then
            NOW_EPOCH=$(date -u +%s)
            AGE=$(( NOW_EPOCH - OFFERED_EPOCH ))
            COOLDOWN=$(( OFFER_COOLDOWN_DAYS * 86400 ))
            if (( AGE >= 0 && AGE < COOLDOWN )); then
                emit_skip "already_offered"
            fi
        fi
    fi
fi

# --- Detect BREAKING CHANGES in the changelog window ---
# Pull the changelog once; check for any `BREAKING CHANGES:` marker between
# LOCAL_VERSION (exclusive) and UPSTREAM_VERSION (inclusive). If we can't
# fetch the changelog, default to BREAKING=false rather than blocking.
BREAKING="false"
CHANGELOG_PATH="${STATE_DIR}/upstream-changelog.md"
if curl --silent --fail --max-time 10 -o "$CHANGELOG_PATH" "$UPSTREAM_CHANGELOG_URL" 2>/dev/null; then
    # Awk window: capture lines from `## v${UPSTREAM_VERSION}` down to (but not
    # including) `## v${LOCAL_VERSION}`. Then grep for BREAKING CHANGES marker.
    WINDOW=$(awk -v upstream="## v${UPSTREAM_VERSION}" -v local_v="## v${LOCAL_VERSION}" '
        $0 ~ "^"upstream { capture = 1 }
        $0 ~ "^"local_v { capture = 0 }
        capture { print }
    ' "$CHANGELOG_PATH")
    if printf '%s\n' "$WINDOW" | grep -q "BREAKING CHANGES:"; then
        BREAKING="true"
    fi
fi

# --- Emit ---
CHANGELOG_URL="https://github.com/scrollinondubs/behalfbot/blob/main/chassis/CHANGELOG.md"

jq -n \
    --arg current "$LOCAL_VERSION" \
    --arg latest "$UPSTREAM_VERSION" \
    --arg changelog_url "$CHANGELOG_URL" \
    --argjson breaking "$BREAKING" \
    '{
        "count": 1,
        "current": $current,
        "latest": $latest,
        "changelog_url": $changelog_url,
        "breaking": $breaking
    }'

# Record what we offered. Rewriting `offered_at` on every emit is what restarts
# the cooldown - without it a re-offer would fire on every subsequent tick.
# Schema is fixed: chassis/skills/chassis-update.md reads `.version` off this
# file to know which version an `update chassis` / `skip update` reply means.
jq -n --arg v "$UPSTREAM_VERSION" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{"version": $v, "offered_at": $ts}' > "$LAST_OFFERED_FILE"
