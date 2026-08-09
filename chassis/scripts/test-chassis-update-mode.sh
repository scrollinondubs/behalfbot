#!/usr/bin/env bash
# test-chassis-update-mode.sh - behavioural tests for chassis-update.sh's
# install-layout detection and its end-of-run status reporting.
#
# The bug this suite exists for (#152)
# ====================================
# detect_mode asked exactly one question - is CHASSIS_HOME's git origin
# scrollinondubs/behalfbot? - and treated every "no" as vendored_subtree. On an
# overlay-mount install (new-jaxity#136, the reference install's layout) the
# chassis is not in CHASSIS_HOME's repo at all: it is a separate behalfbot clone
# bind-mounted underneath it. The else branch therefore planned
# `git subtree pull --prefix=chassis` inside the CUSTOMER repo, for a path owned
# by a different repository, and the supported update path had never once worked
# there. Every real update on that install was a hand-run `git pull`.
#
# Two more defects rode along in the same six lines of output: the container
# refresh was skipped with a one-line note, and the final line said
# `Update complete: v0.4.0 → v0.5.0` on a run that pulled nothing and refreshed
# nothing. An operator reading only the last line believed the update landed.
#
# Per the "checks that cannot fail" rule, classification tests alone would prove
# little - they would pass on a script that printed a mode string and did the
# wrong thing anyway. So each case asserts the PLANNED COMMAND, and the overlay
# cases assert the absence of the subtree pull specifically.
#
# Scenarios:
#    1. canonical clone                       -> canonical_clone, git pull in CHASSIS_HOME
#    2. vendored subtree                      -> vendored_subtree, git subtree pull
#    3. overlay mount, state file present     -> overlay_mount, git pull in the clone
#    4. overlay mount                         -> never plans a subtree pull  (the #152 bug)
#    5. overlay mount, no state file          -> resolves via SCRIPT_DIR/..
#    6. state file beats SCRIPT_DIR/..        -> baked copy still classifies overlay
#    7. unrecognised layout                   -> refuses loudly, plans nothing
#    8. dry run                               -> never prints "Update complete"
#    9. real run whose pull delivers nothing  -> dies, no success line
#   10. skipped container refresh             -> visible in the final outcome
#
# No network and no docker daemon: upstream is served from a temp dir over
# file:// via CHASSIS_UPDATE_RAW_BASE, and `docker` is stubbed on PATH so the
# container probes answer "nothing running" instead of reaching a real daemon.
#
# Exit codes: 0 all pass, 1 on failure, 2 on harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATER="${SCRIPT_DIR}/chassis-update.sh"

for f in "$UPDATER" "${SCRIPT_DIR}/_chassis-update-health.sh" "${SCRIPT_DIR}/_compose-verify.sh"; do
    if [[ ! -f "$f" ]]; then
        echo "test-chassis-update-mode: missing $f" >&2
        exit 2
    fi
done
for bin in git jq curl; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "test-chassis-update-mode: $bin required" >&2
        exit 2
    fi
done

fail=0
pass=0

# Physical path: `git rev-parse --show-toplevel` reports one, and on macOS
# mktemp hands back /var/... which is a symlink to /private/var/....
TMP="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT

# --- docker stub -------------------------------------------------------------
# `command -v docker` must succeed (that is the real container-mode signal) but
# every probe must answer empty, so the suite never touches a real daemon and
# never depends on whether one is running.
STUB_BIN="$TMP/bin"
mkdir -p "$STUB_BIN"
printf '#!/bin/bash\nexit 0\n' > "$STUB_BIN/docker"
chmod +x "$STUB_BIN/docker"
export PATH="$STUB_BIN:$PATH"

# --- upstream served over file:// -------------------------------------------
UPSTREAM_DIR="$TMP/upstream"
mkdir -p "$UPSTREAM_DIR/chassis"
UPSTREAM_VERSION="0.9.0"
printf '%s\n' "$UPSTREAM_VERSION" > "$UPSTREAM_DIR/chassis/VERSION"
cat > "$UPSTREAM_DIR/chassis/CHANGELOG.md" <<'EOF'
# Chassis Changelog

## v0.9.0 - 2026-08-09

### Fixed

- Nothing that breaks anything.

## v0.1.0 - 2026-01-01

### Added

- The beginning.
EOF
export CHASSIS_UPDATE_RAW_BASE="file://${UPSTREAM_DIR}"

# --- assertions --------------------------------------------------------------

report_pass() { pass=$((pass + 1)); }
report_fail() {
    echo "FAIL [$1] $2"
    fail=$((fail + 1))
}

assert_contains() {
    local name="$1" needle="$2" haystack="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        report_pass
    else
        report_fail "$name" "expected output to contain: $needle"
        printf '%s\n' "$haystack" | sed 's/^/        | /'
    fi
}

assert_not_contains() {
    local name="$1" needle="$2" haystack="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        report_pass
    else
        report_fail "$name" "expected output NOT to contain: $needle"
        printf '%s\n' "$haystack" | sed 's/^/        | /'
    fi
}

assert_status() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        report_pass
    else
        report_fail "$name" "expected exit $expected, got $actual"
    fi
}

# --- fixture helpers ---------------------------------------------------------

git_q() { git -c user.name=test -c user.email=test@example.invalid -C "$1" "${@:2}"; }

# Drop the scripts under test into a chassis tree at $1, with VERSION $2.
plant_chassis_tree() {
    local tree="$1" version="$2"
    mkdir -p "$tree/scripts" "$tree/scheduled-tasks"
    cp "$UPDATER" "${SCRIPT_DIR}/_chassis-update-health.sh" \
       "${SCRIPT_DIR}/_compose-verify.sh" "$tree/scripts/"
    printf '%s\n' "$version" > "$tree/VERSION"
    printf '#!/bin/bash\n:\n' > "$tree/scheduled-tasks/heartbeat-dispatcher.sh"
    chmod +x "$tree/scheduled-tasks/heartbeat-dispatcher.sh"
}

write_state_file() {
    local customer_home="$1" resolved_root="$2"
    jq -n --arg r "$resolved_root" \
        '{"schema": 1, "mode": "live", "resolved_root": $r,
          "baked_root": "/app/chassis", "live_root": $r,
          "baked_version": "0.1.0", "live_version": "0.1.0",
          "resolved_at": "2026-08-09T00:00:00Z", "error": null}' \
        > "$customer_home/chassis-root.state.json"
}

# Run the updater copy that lives in the given chassis tree.
run_updater() {
    local chassis_home="$1" tree="$2"
    shift 2
    CHASSIS_HOME="$chassis_home" CUSTOMER_HOME="$chassis_home" \
        bash "$tree/scripts/chassis-update.sh" "$@" 2>&1
}

# --- Scenario 1: canonical clone --------------------------------------------
# CHASSIS_HOME is itself a behalfbot clone. Unchanged behaviour.
CANON="$TMP/canonical"
mkdir -p "$CANON"
git_q "$CANON" init -q -b main
git_q "$CANON" remote add origin https://github.com/scrollinondubs/behalfbot.git
plant_chassis_tree "$CANON/chassis" "0.1.0"
git_q "$CANON" add -A
git_q "$CANON" commit -qm "chassis"
out=$(run_updater "$CANON" "$CANON/chassis" --dry-run); status=$?
assert_status "canonical: exits clean" 0 $status
assert_contains "canonical: mode" "Mode: canonical_clone" "$out"
assert_contains "canonical: plans a ff-only pull in CHASSIS_HOME" \
    "DRY-RUN: cd '$CANON' && git pull --ff-only origin main" "$out"

# --- Scenario 2: vendored subtree -------------------------------------------
# Customer repo carrying the behalfbot repo at prefix chassis/, so the chassis
# tree nests one level down. Unchanged behaviour.
VENDOR="$TMP/vendored"
mkdir -p "$VENDOR"
git_q "$VENDOR" init -q -b main
git_q "$VENDOR" remote add origin https://github.com/acme/acme-behalfbot.git
plant_chassis_tree "$VENDOR/chassis/chassis" "0.1.0"
git_q "$VENDOR" add -A
git_q "$VENDOR" commit -qm "vendor chassis subtree"
out=$(run_updater "$VENDOR" "$VENDOR/chassis/chassis" --dry-run); status=$?
assert_status "vendored: exits clean" 0 $status
assert_contains "vendored: mode" "Mode: vendored_subtree" "$out"
assert_contains "vendored: plans a subtree pull in CHASSIS_HOME" \
    "DRY-RUN: cd '$VENDOR' && git subtree pull --prefix=chassis" "$out"

# --- Scenario 3 + 4: overlay mount, state file present ----------------------
# The #152 shape: $CHASSIS_HOME/chassis is a SEPARATE behalfbot clone, not a
# subtree of the customer repo.
OVERLAY="$TMP/overlay"
mkdir -p "$OVERLAY"
git_q "$OVERLAY" init -q -b main
git_q "$OVERLAY" remote add origin https://github.com/acme/acme-behalfbot.git
printf 'customer\n' > "$OVERLAY/README.md"
git_q "$OVERLAY" add -A
git_q "$OVERLAY" commit -qm "customer repo"
mkdir -p "$OVERLAY/chassis"
git_q "$OVERLAY/chassis" init -q -b main
git_q "$OVERLAY/chassis" remote add origin https://github.com/scrollinondubs/behalfbot.git
plant_chassis_tree "$OVERLAY/chassis/chassis" "0.1.0"
write_state_file "$OVERLAY" "$OVERLAY/chassis/chassis"
out=$(run_updater "$OVERLAY" "$OVERLAY/chassis/chassis" --dry-run); status=$?
assert_status "overlay: exits clean" 0 $status
assert_contains "overlay: mode" "Mode: overlay_mount" "$out"
assert_contains "overlay: pull target is the chassis clone" \
    "Pull target repo: $OVERLAY/chassis" "$out"
assert_contains "overlay: plans a ff-only pull in the chassis clone" \
    "DRY-RUN: cd '$OVERLAY/chassis' && git pull --ff-only origin main" "$out"
# Scenario 4 - the regression that motivated the issue.
assert_not_contains "overlay: never plans a subtree pull" "git subtree pull" "$out"
# No git operation of any kind is planned against the customer repo. The
# snapshot tar still runs from CHASSIS_HOME (out of scope for #152, see the PR),
# so this is scoped to git rather than to every command.
assert_not_contains "overlay: no git operation planned in the customer repo" \
    "cd '$OVERLAY' && git" "$out"

# --- Scenario 5: overlay mount with no state file ---------------------------
# Pre-#118 install, or one whose resolver has never run: fall back to
# SCRIPT_DIR/.. and still classify correctly.
OVERLAY_NS="$TMP/overlay-nostate"
mkdir -p "$OVERLAY_NS/chassis"
git_q "$OVERLAY_NS" init -q -b main
git_q "$OVERLAY_NS" remote add origin https://github.com/acme/acme-behalfbot.git
git_q "$OVERLAY_NS/chassis" init -q -b main
git_q "$OVERLAY_NS/chassis" remote add origin https://github.com/scrollinondubs/behalfbot.git
plant_chassis_tree "$OVERLAY_NS/chassis/chassis" "0.1.0"
out=$(run_updater "$OVERLAY_NS" "$OVERLAY_NS/chassis/chassis" --dry-run); status=$?
assert_status "overlay no-state: exits clean" 0 $status
assert_contains "overlay no-state: mode" "Mode: overlay_mount" "$out"
assert_contains "overlay no-state: plans a ff-only pull in the chassis clone" \
    "DRY-RUN: cd '$OVERLAY_NS/chassis' && git pull --ff-only origin main" "$out"
assert_not_contains "overlay no-state: never plans a subtree pull" "git subtree pull" "$out"

# --- Scenario 6: the state file beats SCRIPT_DIR/.. -------------------------
# The container case: the operator runs the BAKED copy of this script, which
# lives outside every git repo. SCRIPT_DIR/.. resolves to the baked tree and
# tells you nothing, so the recorded resolution has to win.
BAKED="$TMP/baked-chassis"
plant_chassis_tree "$BAKED" "0.1.0"
out=$(run_updater "$OVERLAY" "$BAKED" --dry-run); status=$?
assert_status "baked copy: exits clean" 0 $status
assert_contains "baked copy: still classifies overlay from the state file" \
    "Mode: overlay_mount" "$out"
assert_contains "baked copy: chassis tree comes from the state file" \
    "Chassis tree: $OVERLAY/chassis/chassis" "$out"
assert_contains "baked copy: plans a ff-only pull in the chassis clone" \
    "DRY-RUN: cd '$OVERLAY/chassis' && git pull --ff-only origin main" "$out"

# --- Scenario 7: unrecognised layout refuses --------------------------------
# CHASSIS_HOME is not a repo, the chassis tree is not a repo, nothing is
# vendored. The old code called this vendored_subtree and planned a subtree
# merge into a directory it knew nothing about.
UNKNOWN="$TMP/unknown"
plant_chassis_tree "$UNKNOWN/chassis/chassis" "0.1.0"
out=$(run_updater "$UNKNOWN" "$UNKNOWN/chassis/chassis" --dry-run); status=$?
assert_status "unknown: refuses" 1 $status
assert_contains "unknown: says why" "cannot classify this install's chassis layout" "$out"
assert_contains "unknown: prints the evidence" "resolved chassis root" "$out"
assert_not_contains "unknown: does not fall through to a subtree pull" \
    "git subtree pull" "$out"
assert_not_contains "unknown: plans nothing at all" "DRY-RUN: cd" "$out"

# --- Scenario 8 + 10: dry-run honesty ---------------------------------------
# The exact line from the issue: `Update complete: v0.4.0 → v0.5.0` printed by a
# run that changed nothing. And the skipped container refresh, which used to be
# a mid-log note under that same success line.
out=$(run_updater "$OVERLAY" "$OVERLAY/chassis/chassis" --dry-run)
assert_not_contains "dry-run: never claims the update completed" "Update complete" "$out"
assert_contains "dry-run: says nothing changed" "DRY-RUN complete: nothing was changed." "$out"
assert_contains "dry-run: surfaces the skipped container refresh in the outcome" \
    "container refresh WOULD BE SKIPPED" "$out"

# --- Scenario 9: a real run whose pull delivers nothing ---------------------
# Canonical clone that is already level with its origin, while upstream VERSION
# says there is a newer release. `git pull --ff-only` returns 0 and moves
# nothing. The pre-#152 script did eventually catch this - but only by burning
# the full 60-second healthcheck poll and then rolling back a snapshot of a tree
# that had never changed. The failure is now named at the point it happens.
ORIGIN_DIR="$TMP/remote/scrollinondubs/behalfbot.git"
mkdir -p "$ORIGIN_DIR"
git_q "$ORIGIN_DIR" init -q --bare -b main
SEED="$TMP/seed"
mkdir -p "$SEED"
git_q "$SEED" init -q -b main
plant_chassis_tree "$SEED/chassis" "0.1.0"
git_q "$SEED" add -A
git_q "$SEED" commit -qm "seed"
git_q "$SEED" remote add origin "$ORIGIN_DIR"
git_q "$SEED" push -q origin main
NOPULL="$TMP/nopull"
git -c user.name=test -c user.email=test@example.invalid \
    clone -q "$ORIGIN_DIR" "$NOPULL"
out=$(run_updater "$NOPULL" "$NOPULL/chassis"); status=$?
assert_status "no-op pull: fails instead of reporting success" 1 $status
assert_not_contains "no-op pull: never prints a success line" "Update complete" "$out"
assert_contains "no-op pull: names the defect" "did not advance the chassis tree" "$out"
if [[ -f "$NOPULL/state/chassis-update/last-applied.json" ]]; then
    report_fail "no-op pull: last-applied.json" "written for an update that never landed"
else
    report_pass
fi

# --- summary ----------------------------------------------------------------
echo
if [[ $fail -eq 0 ]]; then
    echo "test-chassis-update-mode: ${pass} passed"
    exit 0
fi
echo "test-chassis-update-mode: ${pass} passed, ${fail} FAILED"
exit 1
