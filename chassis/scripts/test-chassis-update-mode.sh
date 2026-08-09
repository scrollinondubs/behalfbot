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
#   11. same VERSION, drifted changelog       -> plans a real pull        (#147)
#   12. same VERSION, same changelog          -> still the up-to-date no-op
#   13. REAL drift apply                      -> lands, records kind=drift
#   14. re-running it                         -> up-to-date no-op
#   15. REAL drift run that pulls nothing     -> dies, no success line
#
# No network and no docker daemon: upstream is served from a temp dir over
# file:// via CHASSIS_UPDATE_RAW_BASE, and `docker` is stubbed on PATH so the
# container probes answer "nothing running" instead of reaching a real daemon.
#
# Exit codes: 0 all pass, 1 on failure, 2 on harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATER="${SCRIPT_DIR}/chassis-update.sh"

for f in "$UPDATER" "${SCRIPT_DIR}/_chassis-update-health.sh" \
         "${SCRIPT_DIR}/_compose-verify.sh" "${SCRIPT_DIR}/_chassis-changelog.sh"; do
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

## Unreleased

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

# Write a changelog at $1 whose `## Unreleased` body is the line $2. An empty
# $2 leaves the section empty, which is the state of main right after a release.
write_changelog() {
    local path="$1" unreleased="${2:-}"
    {
        printf '# Chassis Changelog\n\n'
        printf '## Unreleased\n\n'
        [[ -n "$unreleased" ]] && printf -- '- %s\n\n' "$unreleased"
        printf '## v0.9.0 - 2026-08-09\n\n### Fixed\n\n- Nothing that breaks anything.\n\n'
        printf '## v0.1.0 - 2026-01-01\n\n### Added\n\n- The beginning.\n'
    } > "$path"
}

# Drop the scripts under test into a chassis tree at $1, with VERSION $2 and an
# `## Unreleased` body of $3 (default: none).
plant_chassis_tree() {
    local tree="$1" version="$2" unreleased="${3:-}"
    mkdir -p "$tree/scripts" "$tree/scheduled-tasks"
    cp "$UPDATER" "${SCRIPT_DIR}/_chassis-update-health.sh" \
       "${SCRIPT_DIR}/_compose-verify.sh" "${SCRIPT_DIR}/_chassis-changelog.sh" \
       "$tree/scripts/"
    printf '%s\n' "$version" > "$tree/VERSION"
    write_changelog "$tree/CHANGELOG.md" "$unreleased"
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

# --- Scenarios 11-15: unreleased drift (#147) --------------------------------
#
# The critical half of the issue. The checker can now see that main carries
# unreleased commits under an unchanged VERSION, but this script gated on
# `CURRENT_VERSION == UPSTREAM_VERSION` and exited 0 with "Already up to date."
# A notification the apply path refuses to act on is worse than no
# notification, so these cases drive the applier, not the checker.
#
# Two more gates would have failed a real drift apply even after the first one
# was opened, and only a REAL run reaches them: step 5.5 ("pull did not advance
# the chassis tree", which is true of every drift apply by definition) and the
# step 7 healthcheck (which compares VERSION, unchanged, so it passes on the
# first poll whether or not anything happened). Scenarios 13-15 run for real.

DRIFT_RAW="$TMP/upstream-drift"
mkdir -p "$DRIFT_RAW/chassis"
printf '0.9.0\n' > "$DRIFT_RAW/chassis/VERSION"
write_changelog "$DRIFT_RAW/chassis/CHANGELOG.md" "the fix that merged after v0.9.0 was cut"

LEVEL_RAW="$TMP/upstream-level"
mkdir -p "$LEVEL_RAW/chassis"
printf '0.9.0\n' > "$LEVEL_RAW/chassis/VERSION"
write_changelog "$LEVEL_RAW/chassis/CHANGELOG.md" "already applied here"

# --- Scenario 11: same VERSION, drifted changelog, dry run ------------------
DRIFT_CANON="$TMP/drift-canonical"
mkdir -p "$DRIFT_CANON"
git_q "$DRIFT_CANON" init -q -b main
git_q "$DRIFT_CANON" remote add origin https://github.com/scrollinondubs/behalfbot.git
plant_chassis_tree "$DRIFT_CANON/chassis" "0.9.0" "already applied here"
git_q "$DRIFT_CANON" add -A
git_q "$DRIFT_CANON" commit -qm "chassis at 0.9.0"
out=$(CHASSIS_UPDATE_RAW_BASE="file://${DRIFT_RAW}" \
    run_updater "$DRIFT_CANON" "$DRIFT_CANON/chassis" --dry-run); status=$?
assert_status "drift dry-run: exits clean" 0 $status
assert_not_contains "drift dry-run: does not claim to be up to date" \
    "Already up to date" "$out"
assert_contains "drift dry-run: names the unchanged VERSION and the drift" \
    "VERSION is level at v0.9.0, but main carries unreleased changes" "$out"
assert_contains "drift dry-run: plans a real pull" \
    "DRY-RUN: cd '$DRIFT_CANON' && git pull --ff-only origin main" "$out"
assert_contains "drift dry-run: the plan line is not a v-to-same-v no-op" \
    "DRY-RUN plan was: v0.9.0 unreleased" "$out"

# --- Scenario 12: same VERSION, same changelog, dry run ---------------------
# The genuinely up-to-date case must still be a silent no-op. Without this the
# suite would pass on a script that simply removed the gate.
out=$(CHASSIS_UPDATE_RAW_BASE="file://${LEVEL_RAW}" \
    run_updater "$DRIFT_CANON" "$DRIFT_CANON/chassis" --dry-run); status=$?
assert_status "level: exits clean" 0 $status
assert_contains "level: still reports up to date" "Already up to date. Exiting." "$out"
assert_not_contains "level: plans no pull" "DRY-RUN: cd" "$out"

# --- Scenario 13: a REAL drift apply -----------------------------------------
# A bare origin whose chassis/CHANGELOG.md has moved while VERSION has not,
# which is what every merge between releases does to main. The clone starts one
# commit behind it.
DRIFT_ORIGIN="$TMP/remote-drift/scrollinondubs/behalfbot.git"
mkdir -p "$DRIFT_ORIGIN"
git_q "$DRIFT_ORIGIN" init -q --bare -b main
DRIFT_SEED="$TMP/drift-seed"
mkdir -p "$DRIFT_SEED"
git_q "$DRIFT_SEED" init -q -b main
plant_chassis_tree "$DRIFT_SEED/chassis" "0.9.0" "already applied here"
git_q "$DRIFT_SEED" add -A
git_q "$DRIFT_SEED" commit -qm "seed at 0.9.0"
git_q "$DRIFT_SEED" remote add origin "$DRIFT_ORIGIN"
git_q "$DRIFT_SEED" push -q origin main

DRIFT_INSTALL="$TMP/drift-install"
git -c user.name=test -c user.email=test@example.invalid \
    clone -q "$DRIFT_ORIGIN" "$DRIFT_INSTALL"

# Now main moves: a merge that changes no VERSION.
write_changelog "$DRIFT_SEED/chassis/CHANGELOG.md" "the fix that merged after v0.9.0 was cut"
git_q "$DRIFT_SEED" add -A
git_q "$DRIFT_SEED" commit -qm "fix: something, under Unreleased"
git_q "$DRIFT_SEED" push -q origin main

out=$(CHASSIS_UPDATE_RAW_BASE="file://${DRIFT_RAW}" \
    run_updater "$DRIFT_INSTALL" "$DRIFT_INSTALL/chassis"); status=$?
assert_status "real drift apply: succeeds" 0 $status
assert_contains "real drift apply: does not die at the step 5.5 version check" \
    "Update complete" "$out"
assert_not_contains "real drift apply: step 5.5 does not fire on an unmoved VERSION" \
    "did not advance the chassis tree" "$out"
assert_contains "real drift apply: healthcheck proves the unreleased section, not VERSION" \
    "Host-mode unreleased section on disk is" "$out"
assert_contains "real drift apply: the tree really did move" \
    "the fix that merged after v0.9.0 was cut" \
    "$(cat "$DRIFT_INSTALL/chassis/CHANGELOG.md")"
APPLIED="$DRIFT_INSTALL/state/chassis-update/last-applied.json"
if [[ -f "$APPLIED" ]]; then
    report_pass
    assert_contains "real drift apply: last-applied records kind=drift" \
        '"kind": "drift"' "$(cat "$APPLIED")"
    if [[ "$(jq -r '.from_unreleased_digest' "$APPLIED")" \
        != "$(jq -r '.to_unreleased_digest' "$APPLIED")" ]]; then
        report_pass
    else
        report_fail "real drift apply: digests" "from and to are identical in last-applied.json"
    fi
else
    report_fail "real drift apply: last-applied.json" "not written"
    report_fail "real drift apply: last-applied kind" "no file to read"
    report_fail "real drift apply: last-applied digests" "no file to read"
fi

# --- Scenario 14: the drift apply is idempotent ------------------------------
# Immediately re-running it must be the up-to-date no-op, not a second apply.
out=$(CHASSIS_UPDATE_RAW_BASE="file://${DRIFT_RAW}" \
    run_updater "$DRIFT_INSTALL" "$DRIFT_INSTALL/chassis"); status=$?
assert_status "drift re-run: exits clean" 0 $status
assert_contains "drift re-run: is now genuinely up to date" "Already up to date. Exiting." "$out"

# --- Scenario 15: a drift run whose pull delivers nothing --------------------
# The drift equivalent of scenario 9. The clone is level with its origin while
# the raw base advertises a different unreleased section, so the pull returns 0
# having moved nothing. Without a digest-based post-pull check this run would
# report a completed update that never happened.
DRIFT_NOPULL="$TMP/drift-nopull"
git -c user.name=test -c user.email=test@example.invalid \
    clone -q "$DRIFT_ORIGIN" "$DRIFT_NOPULL"
GHOST_RAW="$TMP/upstream-ghost"
mkdir -p "$GHOST_RAW/chassis"
printf '0.9.0\n' > "$GHOST_RAW/chassis/VERSION"
write_changelog "$GHOST_RAW/chassis/CHANGELOG.md" "a change no git remote actually carries"
out=$(CHASSIS_UPDATE_RAW_BASE="file://${GHOST_RAW}" \
    run_updater "$DRIFT_NOPULL" "$DRIFT_NOPULL/chassis"); status=$?
assert_status "drift no-op pull: fails instead of reporting success" 1 $status
assert_not_contains "drift no-op pull: never prints a success line" "Update complete" "$out"
assert_contains "drift no-op pull: names the defect" \
    "pull did not deliver the unreleased changes" "$out"
if [[ -f "$DRIFT_NOPULL/state/chassis-update/last-applied.json" ]]; then
    report_fail "drift no-op pull: last-applied.json" "written for an update that never landed"
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
