#!/usr/bin/env bash
# test-chassis-update-check.sh - behavioural tests for gather-chassis-update-check.sh.
#
# The bug this suite exists for (#146): Gate 5 was documented as a one-week
# debounce and implemented as a permanent mute. It matched on version alone
# and never read the `offered_at` the script itself writes, so an installer
# who was busy the week they were offered an update was silently opted out of
# that version forever. Sean's install was offered 0.4.0 on 2026-08-03, did
# not apply it, and returned `{"count": 0, "reason": "already_offered"}` on
# every check after that.
#
# Per the "checks that cannot fail" rule, a suite that only asserts silence
# proves nothing. Half of these cases force an offer and assert it fires.
#
# Gate 4 (dismissed.json) must stay permanent - that is the operator's
# deliberate "stop telling me" and the distinction the whole fix rests on.
#
# Scenarios:
#    1. behind, no prior offer                    -> offers, writes state
#    2. offered 8 days ago (past cooldown)        -> re-offers   (the #146 bug)
#    3. re-offer rewrites offered_at              -> cooldown restarts
#    4. offered 1 day ago                         -> silent
#    5. offered 5 days ago                        -> silent      (6d default)
#    6. offered 6 days + 1 hour ago               -> re-offers   (6d default)
#    7. cooldown override shortens the window     -> re-offers
#    8. cooldown override lengthens the window    -> silent
#    9. dismissed, never offered                  -> silent, permanently
#   10. dismissed AND offered long ago            -> silent (gate 4 wins)
#   11. offered_at absent from the state file     -> re-offers (fail open)
#   12. offered_at unparseable                    -> re-offers (fail open)
#   13. offered_at in the future                  -> re-offers (fail open)
#   14. local == upstream                         -> up_to_date
#   15. local ahead of upstream                   -> up_to_date
#   16. auto_update.check: false                  -> disabled
#   17. local VERSION missing                     -> silent
#   18. upstream unreachable                      -> silent
#   19. BREAKING CHANGES in the window            -> breaking: true
#
# No docker, no network: upstream is served from a temp dir over file:// via
# the CHASSIS_UPDATE_RAW_BASE override. Exit 0 all pass, 1 on failure, 2 on
# harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATHER="${SCRIPT_DIR}/gather-chassis-update-check.sh"

if [[ ! -f "$GATHER" ]]; then
    echo "test-chassis-update-check: gather script not found at $GATHER" >&2
    exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "test-chassis-update-check: jq required" >&2
    exit 2
fi

fail=0
pass=0

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# --- fixtures ---------------------------------------------------------------

UPSTREAM_DIR="$TMP/upstream"
mkdir -p "$UPSTREAM_DIR/chassis"
printf '0.4.0\n' > "$UPSTREAM_DIR/chassis/VERSION"
cat > "$UPSTREAM_DIR/chassis/CHANGELOG.md" <<'EOF'
# Chassis Changelog

## v0.4.0 - 2026-07-28

### Fixed

- Something that does not break anything.

## v0.3.0 - 2026-07-14

### Added

- Earlier work.
EOF

RAW_BASE="file://${UPSTREAM_DIR}"

# GNU date first (chassis container), BSD second (macOS host). GNU `-r` takes a
# file, so it fails on an epoch argument and the order stays unambiguous.
iso_at() {
    local e="$1"
    date -u -d "@$e" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -r "$e" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null
}

NOW_EPOCH="$(date -u +%s)"
ago() { iso_at $(( NOW_EPOCH - $1 )); }
ahead() { iso_at $(( NOW_EPOCH + $1 )); }

DAY=86400
HOUR=3600

# Build an install under $TMP/$1. $2 = local VERSION ("none" to omit the file).
# Returns the customer home path; the fake chassis tree sits inside it so the
# script's SCRIPT_DIR/../VERSION resolution finds the right version per case.
make_install() {
    local name="$1" version="$2"
    local home="$TMP/$name"
    mkdir -p "$home/chassis/scripts"
    cp "$GATHER" "$home/chassis/scripts/"
    [[ "$version" == "none" ]] || printf '%s\n' "$version" > "$home/chassis/VERSION"
    printf '%s' "$home"
}

# Write $home/state/chassis-update/last-offered.json.
# $2 = version, $3 = offered_at value ("omit" to leave the key out).
set_last_offered() {
    local home="$1" version="$2" offered_at="$3"
    mkdir -p "$home/state/chassis-update"
    if [[ "$offered_at" == "omit" ]]; then
        jq -n --arg v "$version" '{"version": $v}' \
            > "$home/state/chassis-update/last-offered.json"
    else
        jq -n --arg v "$version" --arg ts "$offered_at" \
            '{"version": $v, "offered_at": $ts}' \
            > "$home/state/chassis-update/last-offered.json"
    fi
}

set_dismissed() {
    local home="$1"; shift
    mkdir -p "$home/state/chassis-update"
    printf '%s\n' "$*" | jq -R 'split(" ")' > "$home/state/chassis-update/dismissed.json"
}

# Run the gather against an install. CUSTOMER_HOME is ALWAYS passed explicitly:
# its default is ${HOME}/.behalfbot, and a forgotten override would write a
# fresh offered_at into the operator's real state and consume their pending
# offer.
run_gather() {
    local home="$1" raw_base="${2:-$RAW_BASE}" cooldown="${3:-}"
    env CHASSIS_HOME="$home" \
        CUSTOMER_HOME="$home" \
        CHASSIS_UPDATE_RAW_BASE="$raw_base" \
        ${cooldown:+CHASSIS_UPDATE_OFFER_COOLDOWN_DAYS="$cooldown"} \
        bash "$home/chassis/scripts/gather-chassis-update-check.sh" 2>/dev/null
}

# assert_result <name> <json> <expected count> <expected reason, "" when count 1>
assert_result() {
    local name="$1" out="$2" want_count="$3" want_reason="${4:-}"
    local got_count got_reason
    got_count=$(printf '%s' "$out" | jq -r '.count // "none"' 2>/dev/null || echo "unparseable")
    got_reason=$(printf '%s' "$out" | jq -r '.reason // ""' 2>/dev/null || echo "")
    if [[ "$got_count" == "$want_count" && "$got_reason" == "$want_reason" ]]; then
        printf '  ok   %s (count=%s reason=%s)\n' "$name" "$got_count" "${got_reason:-<none>}"
        pass=$((pass + 1))
    else
        printf '  FAIL %s: expected count=%s reason=%s, got count=%s reason=%s\n' \
            "$name" "$want_count" "${want_reason:-<none>}" "$got_count" "${got_reason:-<none>}"
        printf '%s\n' "$out" | sed 's/^/       | /'
        fail=$((fail + 1))
    fi
}

assert_equals() {
    local name="$1" got="$2" want="$3"
    if [[ "$got" == "$want" ]]; then
        printf '  ok   %s (%s)\n' "$name" "$got"
        pass=$((pass + 1))
    else
        printf '  FAIL %s: expected %s, got %s\n' "$name" "$want" "$got"
        fail=$((fail + 1))
    fi
}

echo "test-chassis-update-check"

# --- 1. behind, never offered -----------------------------------------------
h="$(make_install fresh 0.3.0)"
assert_result "behind, no prior offer" "$(run_gather "$h")" 1

STATE="$h/state/chassis-update/last-offered.json"
assert_equals "first offer records the version" \
    "$(jq -r '.version' "$STATE")" "0.4.0"
assert_equals "first offer records offered_at" \
    "$(jq -r '.offered_at | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")' "$STATE")" "true"

# --- 2. the #146 bug: offered and left alone past the cooldown ---------------
h="$(make_install stale-offer 0.3.0)"
set_last_offered "$h" 0.4.0 "$(ago $(( 8 * DAY )))"
assert_result "offered 8 days ago re-offers" "$(run_gather "$h")" 1

# --- 3. a re-offer restarts the cooldown ------------------------------------
STATE="$h/state/chassis-update/last-offered.json"
REOFFERED_AT="$(jq -r '.offered_at' "$STATE")"
REOFFERED_EPOCH=$(date -u -d "$REOFFERED_AT" +%s 2>/dev/null \
    || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$REOFFERED_AT" +%s 2>/dev/null)
if (( REOFFERED_EPOCH >= NOW_EPOCH - 5 )); then
    printf '  ok   re-offer rewrote offered_at to now (%s)\n' "$REOFFERED_AT"
    pass=$((pass + 1))
else
    printf '  FAIL re-offer did not rewrite offered_at: still %s\n' "$REOFFERED_AT"
    fail=$((fail + 1))
fi
# ...and the very next tick is therefore silent, not a re-offer loop.
assert_result "next tick after a re-offer is silent" "$(run_gather "$h")" 0 already_offered

# --- 4-6. cooldown boundaries at the 6-day default --------------------------
h="$(make_install recent-offer 0.3.0)"
set_last_offered "$h" 0.4.0 "$(ago $DAY)"
assert_result "offered 1 day ago stays silent" "$(run_gather "$h")" 0 already_offered

h="$(make_install five-days 0.3.0)"
set_last_offered "$h" 0.4.0 "$(ago $(( 5 * DAY )))"
assert_result "offered 5 days ago stays silent" "$(run_gather "$h")" 0 already_offered

# Six days plus an hour must clear. The default is 6 rather than 7 precisely so
# the weekly monday 09:00 heartbeat always finds the cooldown expired instead of
# racing it by minutes and degrading to a fortnightly nag.
h="$(make_install six-days 0.3.0)"
set_last_offered "$h" 0.4.0 "$(ago $(( 6 * DAY + HOUR )))"
assert_result "offered 6d1h ago re-offers at the default window" "$(run_gather "$h")" 1

# --- 7-8. the window is a named constant, overridable ------------------------
h="$(make_install short-window 0.3.0)"
set_last_offered "$h" 0.4.0 "$(ago $(( 2 * DAY )))"
assert_result "override to 1 day re-offers a 2-day-old offer" "$(run_gather "$h" "$RAW_BASE" 1)" 1

h="$(make_install long-window 0.3.0)"
set_last_offered "$h" 0.4.0 "$(ago $(( 8 * DAY )))"
assert_result "override to 30 days silences an 8-day-old offer" \
    "$(run_gather "$h" "$RAW_BASE" 30)" 0 already_offered

# --- 9-10. dismissal stays permanent ----------------------------------------
h="$(make_install dismissed 0.3.0)"
set_dismissed "$h" 0.4.0
assert_result "explicit dismissal stays silent" "$(run_gather "$h")" 0 dismissed

h="$(make_install dismissed-old 0.3.0)"
set_dismissed "$h" 0.4.0
set_last_offered "$h" 0.4.0 "$(ago $(( 400 * DAY )))"
assert_result "dismissal outranks an expired cooldown" "$(run_gather "$h")" 0 dismissed

# --- 11-13. corrupt state fails open ----------------------------------------
h="$(make_install no-timestamp 0.3.0)"
set_last_offered "$h" 0.4.0 omit
assert_result "missing offered_at re-offers" "$(run_gather "$h")" 1

h="$(make_install bad-timestamp 0.3.0)"
set_last_offered "$h" 0.4.0 "not-a-timestamp"
assert_result "unparseable offered_at re-offers" "$(run_gather "$h")" 1

h="$(make_install future-timestamp 0.3.0)"
set_last_offered "$h" 0.4.0 "$(ahead $(( 30 * DAY )))"
assert_result "future offered_at re-offers" "$(run_gather "$h")" 1

# --- 14-18. the untouched gates ---------------------------------------------
h="$(make_install current 0.4.0)"
assert_result "local == upstream" "$(run_gather "$h")" 0 up_to_date

h="$(make_install ahead 0.5.0)"
assert_result "local ahead of upstream" "$(run_gather "$h")" 0 up_to_date

h="$(make_install optout 0.3.0)"
printf 'auto_update:\n  check: false\n' > "$h/chassis.config.yaml"
assert_result "auto_update.check false" "$(run_gather "$h")" 0 auto_update_check_disabled

h="$(make_install noversion none)"
assert_result "local VERSION missing" "$(run_gather "$h")" 0 local_version_missing

h="$(make_install offline 0.3.0)"
assert_result "upstream unreachable" \
    "$(run_gather "$h" "file://${TMP}/nowhere")" 0 upstream_unreachable

# --- 19. BREAKING detection survives the rewrite ----------------------------
BREAKING_UPSTREAM="$TMP/upstream-breaking"
mkdir -p "$BREAKING_UPSTREAM/chassis"
printf '0.4.0\n' > "$BREAKING_UPSTREAM/chassis/VERSION"
cat > "$BREAKING_UPSTREAM/chassis/CHANGELOG.md" <<'EOF'
# Chassis Changelog

## v0.4.0 - 2026-07-28

BREAKING CHANGES:

- The dispatcher API moved.

## v0.3.0 - 2026-07-14

### Added

- Earlier work.
EOF
h="$(make_install breaking 0.3.0)"
out="$(run_gather "$h" "file://${BREAKING_UPSTREAM}")"
assert_result "breaking release still offers" "$out" 1
assert_equals "breaking flag set" "$(printf '%s' "$out" | jq -r '.breaking')" "true"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]] || exit 1
