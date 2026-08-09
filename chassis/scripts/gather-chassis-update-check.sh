#!/bin/bash
# gather-chassis-update-check.sh - weekly gate for the chassis-update-check heartbeat.
#
# Issue: scrollinondubs/behalfbot#33 (Apple-style chassis auto-updater).
#
# Compares this install's chassis tree against upstream main and emits JSON
# describing what an apply would deliver. Fires Claude only when:
#   1. There is something to deliver (a newer VERSION, or unreleased changes
#      under an unchanged VERSION - see "Two kinds of offer" below), AND
#   2. The offer is not in the dismissed list, AND
#   3. auto_update.check is true in chassis.config.yaml (default true), AND
#   4. the same offer was not already made inside the cooldown window
#
# Gate 4 (dismissed) is permanent: the operator typed `skip update` and meant
# it. Gate 5 (already offered) is a COOLDOWN, not a mute - an unanswered
# notification is not a dismissal (#146).
#
# Two kinds of offer (#147)
# =========================
# `chassis/VERSION` only moves on an explicit release commit, so a checker that
# gates on version equality goes blind to every merge that lands under
# `## Unreleased`. main is the distribution channel by design
# (chassis-update.sh hardcodes UPSTREAM_BRANCH="main"), so those merges are
# exactly what an apply delivers.
#
#   kind=version - local VERSION is behind upstream's. `current` < `latest`,
#                  the shape this script has always emitted.
#   kind=drift   - the versions are equal but the local tree's `## Unreleased`
#                  section differs from upstream's. `current` == `latest`, and
#                  consumers must not read that as "nothing to do".
#
# Cost contract (deliberately changed, #147)
# ==========================================
# The header used to promise one HTTP GET on the common path: VERSION, plus
# CHANGELOG.md only when already known to be behind. Drift detection needs the
# changelog BEFORE the up-to-date gate, so the common path is now two
# unauthenticated GETs against raw.githubusercontent.com instead of one. On a
# weekly heartbeat that is negligible, and the alternative is keeping a checker
# that structurally cannot see the thing it exists to see. Still no auth, no
# API token, no rate-limited endpoint and no paid call.
#
# The rejected alternative was keying on the upstream commit SHA for chassis/
# via the GitHub API. It is exact where the changelog digest is approximate,
# but it puts an authenticated-or-rate-limited dependency into a script that
# today needs neither, on every install including the ones behind a corporate
# proxy. The digest's weakness - a merge that adds no changelog entry moves no
# digest and stays invisible - is documented in _chassis-changelog.sh and
# closed properly by requiring a changelog entry per PR, which is CI work and a
# separate issue.

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
LOCAL_CHANGELOG_FILE="${SCRIPT_DIR}/../CHANGELOG.md"
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

# `## Unreleased` extraction + digest, shared with chassis-update.sh so the
# checker and the applier cannot disagree about what drift is.
#
# A missing lib degrades to version-only checking rather than to silence: the
# version path is what already worked, and going quiet is the failure mode this
# whole issue is about. The degradation is visible - the equal-version case
# reports `drift_undetectable` instead of `up_to_date`.
DRIFT_CAPABLE=0
# shellcheck source=chassis/scripts/_chassis-changelog.sh
if [[ -f "${SCRIPT_DIR}/_chassis-changelog.sh" ]] && source "${SCRIPT_DIR}/_chassis-changelog.sh"; then
    DRIFT_CAPABLE=1
fi

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

# --- Fetch the upstream changelog (BEFORE the up-to-date gate, #147) ---
# This GET used to sit below the version gates, purely to look for BREAKING
# CHANGES on a path already known to be behind. Drift detection needs it on
# every run - see the cost-contract note in the header.
CHANGELOG_PATH="${STATE_DIR}/upstream-changelog.md"
CHANGELOG_FETCHED=0
if curl --silent --fail --max-time 10 -o "$CHANGELOG_PATH" "$UPSTREAM_CHANGELOG_URL" 2>/dev/null; then
    CHANGELOG_FETCHED=1
fi

UPSTREAM_DIGEST=""
LOCAL_DIGEST=""
if [[ $DRIFT_CAPABLE -eq 1 && $CHANGELOG_FETCHED -eq 1 ]]; then
    UPSTREAM_DIGEST=$(chassis_unreleased_digest "$CHANGELOG_PATH" 2>/dev/null || echo "")
    LOCAL_DIGEST=$(chassis_unreleased_digest "$LOCAL_CHANGELOG_FILE" 2>/dev/null || echo "")
fi

# --- Decide what kind of offer this is, if any ---
#
# OFFER_KEY is what the cooldown and the dismissal list are keyed on. For a
# version offer it is the bare version string, which is exactly what those two
# files have always held, so nothing about the released path changes. For a
# drift offer it carries the digest too, so dismissing one drift offer mutes
# that digest rather than the whole version - a later merge under the same
# version produces a different key and is offered again.
CMP=$(semver_cmp "$LOCAL_VERSION" "$UPSTREAM_VERSION")
KIND=""
OFFER_KEY=""
if [[ "$CMP" == "1" ]]; then
    # Ahead of upstream. Nothing to deliver, and the changelog cannot say
    # otherwise.
    emit_skip "up_to_date"
elif [[ "$CMP" == "0" ]]; then
    if [[ -z "$UPSTREAM_DIGEST" || -z "$LOCAL_DIGEST" ]]; then
        # Versions match and drift cannot be evaluated: no changelog shipped
        # with this tree, the upstream fetch failed, or the shared lib is
        # missing. Silent, but named differently from a true up_to_date so the
        # log says which of the two happened.
        emit_skip "drift_undetectable"
    fi
    if [[ "$UPSTREAM_DIGEST" == "empty" ]]; then
        # Upstream has nothing unreleased. If the local section is non-empty
        # this tree is AHEAD of main in the changelog, not behind it, and an
        # apply would have nothing to fast-forward.
        emit_skip "up_to_date"
    fi
    if [[ "$UPSTREAM_DIGEST" == "$LOCAL_DIGEST" ]]; then
        emit_skip "up_to_date"
    fi
    KIND="drift"
    OFFER_KEY="${UPSTREAM_VERSION}+${UPSTREAM_DIGEST}"
else
    KIND="version"
    OFFER_KEY="$UPSTREAM_VERSION"
fi

# --- Gate 4: not dismissed ---
# Two ways to be dismissed, and the first is the backward-compatible one: a
# bare version string in dismissed.json mutes that version entirely, whatever
# kind of offer it is. That is what every entry written before #147 means, and
# what `skip update` on a version offer still writes. A drift offer is
# additionally muted by its own `<version>+<digest>` key.
if [[ -f "$DISMISSED_FILE" ]]; then
    DISMISSED=$(jq -r --arg v "$UPSTREAM_VERSION" --arg k "$OFFER_KEY" \
        'if type == "array" then (index($v) // index($k) // empty) else empty end' \
        "$DISMISSED_FILE" 2>/dev/null || echo "")
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
    # `.offer_key` is written by this script since #147. The `// .version`
    # fallback reads a state file written by an older copy, where the key WAS
    # the bare version. That fallback is also what makes a drift offer fire
    # immediately on an install holding a pre-#147 record: `0.5.0` never equals
    # `0.5.0+<digest>`, so the cooldown does not apply to an offer that has
    # never actually been made.
    LAST_OFFERED=$(jq -r '.offer_key // .version // empty' "$LAST_OFFERED_FILE" 2>/dev/null || echo "")
    LAST_OFFERED_AT=$(jq -r '.offered_at // empty' "$LAST_OFFERED_FILE" 2>/dev/null || echo "")
    if [[ "$LAST_OFFERED" == "$OFFER_KEY" ]]; then
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

# --- Detect BREAKING CHANGES in the window an apply would deliver ---
#
# Capture starts at `## Unreleased` and runs down to (but not including)
# `## v${LOCAL_VERSION}`, so the window covers the unreleased section AND every
# release heading between the two versions.
#
# Including `## Unreleased` is a deliberate change of behavior for version
# offers too (#147). An apply is a `git pull` of main, which delivers the
# unreleased commits along with the released ones, so a BREAKING marker sitting
# under `## Unreleased` was being delivered while the notification said the
# update needed no review. Erring towards requiring --force is the safe
# direction.
#
# Capture must NOT start at the top of the file: the format-conventions
# preamble contains the literal string `BREAKING CHANGES:` while documenting
# the marker, and every check would flip true.
BREAKING="false"
if [[ $CHANGELOG_FETCHED -eq 1 ]]; then
    WINDOW=$(awk -v upstream="## v${UPSTREAM_VERSION}" -v local_v="## v${LOCAL_VERSION}" '
        /^## Unreleased/ { capture = 1 }
        $0 ~ "^"upstream { capture = 1 }
        $0 ~ "^"local_v { capture = 0 }
        capture { print }
    ' "$CHANGELOG_PATH")
    if printf '%s\n' "$WINDOW" | grep -q "BREAKING CHANGES:"; then
        BREAKING="true"
    fi
fi

# --- Emit ---
#
# `kind` is explicit rather than left to be inferred. A drift offer has
# current == latest, and both existing consumers of this payload - the
# notification prompt and the apply skill - were written assuming
# latest > current. Making them infer the difference from two equal strings is
# how a drift notification ends up reading as a no-op.
CHANGELOG_URL="https://github.com/scrollinondubs/behalfbot/blob/main/chassis/CHANGELOG.md"

jq -n \
    --arg kind "$KIND" \
    --arg current "$LOCAL_VERSION" \
    --arg latest "$UPSTREAM_VERSION" \
    --arg changelog_url "$CHANGELOG_URL" \
    --arg digest "$UPSTREAM_DIGEST" \
    --arg offer_key "$OFFER_KEY" \
    --argjson breaking "$BREAKING" \
    '{
        "count": 1,
        "kind": $kind,
        "current": $current,
        "latest": $latest,
        "changelog_url": $changelog_url,
        "breaking": $breaking,
        "unreleased_digest": $digest,
        "offer_key": $offer_key
    }'

# Record what we offered. Rewriting `offered_at` on every emit is what restarts
# the cooldown - without it a re-offer would fire on every subsequent tick.
#
# Schema is a contract: chassis/skills/chassis-update.md reads this file to know
# what an `update chassis` / `skip update` reply refers to. `.version` is kept
# exactly as it was so an older copy of that skill still reads something true.
# `.offer_key` is what `skip update` should append to dismissed.json, and for a
# version offer the two are the same string, so the released path is unchanged.
jq -n \
    --arg v "$UPSTREAM_VERSION" \
    --arg kind "$KIND" \
    --arg digest "$UPSTREAM_DIGEST" \
    --arg offer_key "$OFFER_KEY" \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{"version": $v, "kind": $kind, "unreleased_digest": $digest,
      "offer_key": $offer_key, "offered_at": $ts}' > "$LAST_OFFERED_FILE"
