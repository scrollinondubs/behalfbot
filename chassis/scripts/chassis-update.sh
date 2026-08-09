#!/bin/bash
# chassis-update.sh - apply a chassis version bump after operator consent.
#
# Issue: scrollinondubs/behalfbot#33 (Apple-style chassis auto-updater).
#
# Idempotent script that updates the chassis to upstream main:
#   1. Pre-flight (clean working tree)
#   2. Snapshot pre-update state + effective compose config
#   3. Drain in-flight heartbeats (state file lock)
#   4. Pull upstream, into the repo that actually owns the chassis tree
#      (canonical-clone / overlay-mount: git pull --ff-only; vendored: git
#      subtree pull) - see "Mode detection" below
#   5. Compose pull + up -d THROUGH compose.sh so the per-install override
#      applies (behalfbot#100) - bare compose only when the install has none
#   6. Healthcheck poll (60s)
#   7. Verify the merged compose config is actually running (ports published,
#      scaled-to-0 services down, override in the container's config_files
#      label) and report a config diff across the update
#   8. Run migration script if present for the new version
#   9. Post success / failure to the alerts channel
#
# Usage:
#   chassis-update.sh                # apply non-breaking update
#   chassis-update.sh --force        # apply BREAKING-CHANGE update (operator reviewed)
#   chassis-update.sh --dry-run      # print plan, don't execute
#   chassis-update.sh --rollback     # restore the most recent pre-update snapshot
#
# Exit codes:
#   0 - update applied (or dry-run printed, or already up to date)
#   1 - refused / failed (pre-flight, unrecognised layout, healthcheck rollback)
#   2 - bad usage
#   3 - PARTIAL: the tree was updated on disk but the running container was not
#       refreshed, so it still executes the previous code
#
# Two kinds of update (#147):
#
#   version - upstream chassis/VERSION is newer than this tree's.
#   drift   - the versions match but upstream main carries commits under
#             `## Unreleased` that this tree does not have. VERSION only moves
#             on an explicit release commit while main is the distribution
#             branch, so this is the normal state between releases, not an edge
#             case. Detected the same way gather-chassis-update-check.sh
#             detects it, via _chassis-changelog.sh, because a checker that can
#             see drift while the applier answers "Already up to date" delivers
#             nothing.
#
# Invoked by `skills/chassis-update.md` in response to the Discord trigger
# `update chassis` / `update chassis --force` in the alerts channel.

set -uo pipefail

CHASSIS_HOME="${CHASSIS_HOME:?CHASSIS_HOME must be set}"
CUSTOMER_HOME="${CUSTOMER_HOME:-${HOME}/.behalfbot}"
# Resolve VERSION relative to this script (works in both vendored-subtree and
# overlay-mount install layouts; see gather-chassis-update-check.sh header).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_VERSION_FILE="${SCRIPT_DIR}/../VERSION"
LOCAL_CHANGELOG_FILE="${SCRIPT_DIR}/../CHANGELOG.md"
# Container discovery + VERSION probe. Split into a sourceable lib so the
# healthcheck logic is testable without running a real update.
# shellcheck source=chassis/scripts/_chassis-update-health.sh
source "${SCRIPT_DIR}/_chassis-update-health.sh"
# Merged-config verification helpers (behalfbot#100). Ships alongside this
# script; a missing copy means a torn tree, and proceeding without it would
# re-open the silent-override-revert hole, so fail loudly (no `set -e` here).
# shellcheck source=chassis/scripts/_compose-verify.sh
source "${SCRIPT_DIR}/_compose-verify.sh" || {
    echo "[chassis-update] FATAL: ${SCRIPT_DIR}/_compose-verify.sh missing - it ships with this script" >&2
    exit 1
}
# `## Unreleased` digest helpers, shared with gather-chassis-update-check.sh so
# the applier's idea of drift is the checker's idea of drift (#147). Soft on
# purpose, unlike _compose-verify.sh above: without it the applier degrades to
# the version-only behavior it has always had rather than refusing a released
# update it is perfectly able to perform.
DRIFT_CAPABLE=0
# shellcheck source=chassis/scripts/_chassis-changelog.sh
if [[ -f "${SCRIPT_DIR}/_chassis-changelog.sh" ]] && source "${SCRIPT_DIR}/_chassis-changelog.sh"; then
    DRIFT_CAPABLE=1
fi
UPSTREAM_REMOTE_URL="${CHASSIS_UPDATE_REMOTE:-https://github.com/scrollinondubs/behalfbot.git}"
UPSTREAM_REMOTE_NAME="chassis"
UPSTREAM_BRANCH="main"
UPSTREAM_RAW_BASE="${CHASSIS_UPDATE_RAW_BASE:-https://raw.githubusercontent.com/scrollinondubs/behalfbot/main}"
STATE_DIR="${CUSTOMER_HOME}/state/chassis-update"
BACKUP_DIR="${CUSTOMER_HOME}/backups/chassis-update"
DRAIN_LOCK_FILE="${CUSTOMER_HOME}/state/heartbeat-dispatcher.lock"
DRAIN_TIMEOUT_SECONDS=60
HEALTHCHECK_TIMEOUT_SECONDS=60

FORCE=0
DRY_RUN=0
ROLLBACK=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --rollback) ROLLBACK=1 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

mkdir -p "$STATE_DIR" "$BACKUP_DIR"

log() { printf '[chassis-update] %s\n' "$*"; }
die() { printf '[chassis-update] FATAL: %s\n' "$*" >&2; exit 1; }

# This script runs without `set -e` on purpose - the healthcheck and drain
# loops depend on non-zero exits being survivable. That made dry_or_run a silent
# failure sink: `eval` returning non-zero was discarded, so a failed
# `git subtree pull` or a failed `docker compose up -d` flowed straight on to
# the healthcheck as though it had worked. Every step that must succeed now
# aborts here instead.
dry_or_run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        log "DRY-RUN: $*"
        return 0
    fi
    log "+ $*"
    eval "$@" || die "command failed: $*"
}

# Best-effort variant for the rollback path, where a failing step must be loud
# but must not abort before the remaining recovery steps get a chance to run.
dry_or_run_soft() {
    if [[ $DRY_RUN -eq 1 ]]; then
        log "DRY-RUN: $*"
        return 0
    fi
    log "+ $*"
    eval "$@" || { log "WARN: command failed (continuing recovery): $*"; return 1; }
}

# --- Compose invocation strategy (behalfbot#100) ---
#
# This script used to bring the stack back with bare `docker compose pull` +
# `docker compose up -d`. Bare compose knows nothing about the per-install
# override ($CUSTOMER_HOME/chassis-compose.override.yml - image pins,
# published ports, env_file, scaled-to-0 services), so every update silently
# reverted the customer's compose configuration and then reported success.
# compose.sh is the chassis's one supported way to invoke compose - it layers
# the override and hard-errors when the file it was told to use is missing.
#
# Strategy, decided ONCE up front:
#   - CHASSIS_COMPOSE_OVERRIDE set (even to "") or the default override file
#     present  -> every compose call goes through compose.sh. `${VAR-}` (no
#     colon) mirrors compose.sh: set-but-empty means "deliberately no
#     override" (chassis dev / smoke-test), unset means "use the default path".
#   - neither -> a plain default install that never had an override. Keep the
#     exact legacy bare invocation, with a WARN: forcing compose.sh here would
#     turn its missing-override guard into a failed update for installs that
#     were never broken. We do NOT create an override to satisfy the guard.
#
# Old-copy-of-this-script note: the process applying an update is the OLD
# updater; the step-5 pull writes the NEW tree (including compose.sh, which
# first shipped in v0.2.0) to disk before step 6 runs. So by the time compose
# is invoked, ${SCRIPT_DIR}/compose.sh exists even when updating FROM a
# pre-v0.2.0 tree - and if it somehow does not, that is a torn pull and we
# die rather than fall back to the bare invocation this fix removes.
# Installs whose OLD updater predates this fix still run one last bare-compose
# update; chassis-migrations/v0.3.0.sh repairs those at the end of that run.
COMPOSE_SH="${SCRIPT_DIR}/compose.sh"
COMPOSE_DIR="$CHASSIS_HOME"
OVERRIDE_FILE="${CHASSIS_COMPOSE_OVERRIDE-${CUSTOMER_HOME}/chassis-compose.override.yml}"
if [[ -n "${CHASSIS_COMPOSE_OVERRIDE+x}" || -f "$OVERRIDE_FILE" ]]; then
    USE_COMPOSE_SH=1
else
    USE_COMPOSE_SH=0
fi

# Print the shell command that runs `docker compose $*` for this install.
# compose.sh resolves its compose files, project name and --env-file from its
# own location + CUSTOMER_HOME, independent of the caller's cwd.
compose_invoke() {
    if [[ $USE_COMPOSE_SH -eq 1 ]]; then
        echo "CUSTOMER_HOME='$CUSTOMER_HOME' bash '$COMPOSE_SH' $*"
    else
        echo "cd '$COMPOSE_DIR' && docker compose $*"
    fi
}

# Best-effort snapshot of the effective (merged) compose config, so a diff
# exists as evidence when the update changes the stack underneath an operator.
# Soft on purpose: a broken pre-update tree must not block the update - the
# step-7 verification after `up -d` is the hard gate. Output can contain
# interpolated secrets from .env.baked, hence chmod 600 and STATE_DIR only.
snapshot_config() {
    local out="$1"
    if [[ $DRY_RUN -eq 1 ]]; then
        log "DRY-RUN: snapshot effective compose config -> $out"
        return 0
    fi
    local cmd
    cmd=$(compose_invoke "config")
    if eval "$cmd" > "$out" 2>/dev/null && [[ -s "$out" ]]; then
        chmod 600 "$out"
        log "Effective compose config snapshot: $out"
    else
        rm -f "$out"
        log "WARN: could not snapshot effective compose config to $out (continuing)"
    fi
}

CONFIG_PRE="${STATE_DIR}/compose-config-pre.yaml"
CONFIG_POST="${STATE_DIR}/compose-config-post.yaml"
CONFIG_DIFF="${STATE_DIR}/compose-config.diff"

# --- Mode detection (behalfbot#152) ---
#
# The question this answers is "which git repository owns the chassis tree this
# install actually runs", and the answer decides the pull strategy:
#
#   canonical_clone  - CHASSIS_HOME is itself a clone of scrollinondubs/behalfbot.
#                      Update with `git pull --ff-only` there.
#   overlay_mount    - the chassis tree resolves into a SEPARATE git worktree
#                      that is a clone of scrollinondubs/behalfbot, mounted or
#                      nested underneath CHASSIS_HOME. This is the supported
#                      overlay layout (new-jaxity#136) the reference install
#                      runs, the one gather-chassis-update-check.sh names in its
#                      own header and resolve-chassis-root.sh already resolves.
#                      Update with `git pull --ff-only` in THAT repo.
#   vendored_subtree - CHASSIS_HOME is a customer repo that genuinely carries
#                      chassis/ as a git subtree. Update with `git subtree pull`.
#
# What was wrong before: detection asked one question - is CHASSIS_HOME's origin
# behalfbot? - and treated every "no" as vendored_subtree. On an overlay-mount
# install that planned `git subtree pull --prefix=chassis` inside the CUSTOMER
# repo for a path owned by a different repository, so the supported update path
# had never once worked there. An unrecognised layout now refuses rather than
# falling through to the destructive strategy: silently picking a subtree merge
# for a shape you cannot identify is worse than doing nothing.

git_origin_url() {
    git -C "$1" config --get remote.origin.url 2>/dev/null || true
}

git_worktree_root() {
    git -C "$1" rev-parse --show-toplevel 2>/dev/null || true
}

is_behalfbot_origin() {
    [[ "$1" == *"scrollinondubs/behalfbot"* ]]
}

# The chassis tree this install actually runs. resolve-chassis-root.sh decides
# that at boot and records the answer; read it rather than re-deriving one (the
# pattern gather-chassis-root-health.sh already uses). SCRIPT_DIR/.. is the
# fallback for installs whose resolver has never run - this script always ships
# at <chassis>/scripts/, in every layout.
resolve_chassis_source_root() {
    local state_file="${CUSTOMER_HOME}/chassis-root.state.json" root=""
    if [[ -f "$state_file" ]] && command -v jq >/dev/null 2>&1; then
        root=$(jq -r '.resolved_root // ""' "$state_file" 2>/dev/null || echo "")
    fi
    if [[ -z "$root" || ! -d "$root" ]]; then
        root=$(cd "${SCRIPT_DIR}/.." && pwd)
    fi
    printf '%s' "$root"
}

MODE=""
CHASSIS_REPO_DIR=""       # the repo the pull runs in
CHASSIS_SOURCE_ROOT=""    # the chassis tree itself
CUSTOMER_WORKTREE=""
CHASSIS_WORKTREE=""
CUSTOMER_ORIGIN=""
CHASSIS_ORIGIN=""

# Sets MODE + CHASSIS_REPO_DIR as globals (NOT via echo - the caller needs both,
# and a command substitution would throw the assignments away with the subshell).
# Returns non-zero when the layout is unrecognised.
detect_mode() {
    CHASSIS_SOURCE_ROOT=$(resolve_chassis_source_root)
    CUSTOMER_WORKTREE=$(git_worktree_root "$CHASSIS_HOME")
    CHASSIS_WORKTREE=$(git_worktree_root "$CHASSIS_SOURCE_ROOT")
    CUSTOMER_ORIGIN=$(git_origin_url "$CHASSIS_HOME")

    if is_behalfbot_origin "$CUSTOMER_ORIGIN"; then
        CHASSIS_REPO_DIR="$CHASSIS_HOME"
        MODE="canonical_clone"
        return 0
    fi

    # Overlay: the chassis tree sits in a git worktree that is NOT the one
    # CHASSIS_HOME belongs to, and that worktree is a behalfbot clone.
    if [[ -n "$CHASSIS_WORKTREE" && "$CHASSIS_WORKTREE" != "$CUSTOMER_WORKTREE" ]]; then
        CHASSIS_ORIGIN=$(git_origin_url "$CHASSIS_WORKTREE")
        if is_behalfbot_origin "$CHASSIS_ORIGIN"; then
            CHASSIS_REPO_DIR="$CHASSIS_WORKTREE"
            MODE="overlay_mount"
            return 0
        fi
    fi

    # Vendored subtree: the chassis tree is inside CHASSIS_HOME and chassis/ is
    # a tracked path of CHASSIS_HOME's repo. Both halves matter - a directory
    # that merely exists at chassis/ is what the old detection settled for.
    if [[ "$CHASSIS_SOURCE_ROOT" == "$CHASSIS_HOME"/* ]] \
        && [[ -n "$(git -C "$CHASSIS_HOME" ls-tree -d --name-only HEAD chassis 2>/dev/null)" ]]; then
        CHASSIS_REPO_DIR="$CHASSIS_HOME"
        MODE="vendored_subtree"
        return 0
    fi

    MODE="unknown"
    return 1
}

if ! detect_mode; then
    cat >&2 <<EOF
[chassis-update] FATAL: cannot classify this install's chassis layout, so there
[chassis-update] FATAL: is no update strategy that is safe to pick. Observed:
[chassis-update] FATAL:
[chassis-update] FATAL:   CHASSIS_HOME              = ${CHASSIS_HOME}
[chassis-update] FATAL:   CHASSIS_HOME git worktree = ${CUSTOMER_WORKTREE:-<none>}
[chassis-update] FATAL:   CHASSIS_HOME origin       = ${CUSTOMER_ORIGIN:-<none>}
[chassis-update] FATAL:   resolved chassis root     = ${CHASSIS_SOURCE_ROOT:-<none>}
[chassis-update] FATAL:   its git worktree          = ${CHASSIS_WORKTREE:-<none>}
[chassis-update] FATAL:   its origin                = ${CHASSIS_ORIGIN:-<none>}
[chassis-update] FATAL:
[chassis-update] FATAL: Expected one of: a behalfbot clone at CHASSIS_HOME
[chassis-update] FATAL: (canonical_clone), a behalfbot clone mounted underneath it
[chassis-update] FATAL: (overlay_mount), or chassis/ vendored into CHASSIS_HOME's
[chassis-update] FATAL: own repo as a subtree (vendored_subtree).
[chassis-update] FATAL:
[chassis-update] FATAL: An empty worktree or origin above usually means git REFUSED
[chassis-update] FATAL: the directory rather than that it is not a repo - run
[chassis-update] FATAL: 'git -C <path> status' and look for a dubious-ownership
[chassis-update] FATAL: refusal, then add the safe.directory exception it asks for.
EOF
    die "unrecognised chassis layout - refusing to guess an update strategy (behalfbot#152)"
fi

log "Mode: $MODE"
log "Chassis tree: $CHASSIS_SOURCE_ROOT"
log "Pull target repo: $CHASSIS_REPO_DIR"

# --- Snapshot restore, shared by --rollback and the healthcheck failure path ---
#
# Restoring chassis/ on disk is only half of a rollback. When the install runs
# containers, the code that actually executes lives in the image, and
# `docker compose up -d` after a restore just re-resolves the same (new) tag.
# So each snapshot carries a sidecar recording the image the container was on
# BEFORE the update, and the restore pins CHASSIS_IMAGE back to it.
restore_snapshot() {
    local snapshot="$1"
    local compose_dir="$2"
    local image_sidecar="${snapshot%.tgz}.image"
    local pinned_image=""

    log "Restoring from $snapshot"
    dry_or_run_soft "cd '$CHASSIS_HOME' && tar xzf '$snapshot'"

    if [[ ! -f "${compose_dir}/docker-compose.yml" ]]; then
        log "No docker-compose.yml at $compose_dir; disk restore only, no container to roll back"
        return 0
    fi

    if [[ -s "$image_sidecar" ]]; then
        pinned_image=$(tr -d '[:space:]' < "$image_sidecar")
    fi
    # Same rule as step 5 (behalfbot#100): the recovery `up -d` must not
    # silently strip the per-install override either. compose.sh may be
    # missing here when the snapshot restored a pre-v0.2.0 tree over it, so
    # fall back to bare compose with a warning rather than dying mid-recovery.
    local up_prefix="cd '$compose_dir' &&"
    local up_cmd="docker compose"
    if [[ $USE_COMPOSE_SH -eq 1 ]]; then
        if [[ -f "$COMPOSE_SH" ]]; then
            up_prefix="CUSTOMER_HOME='$CUSTOMER_HOME'"
            up_cmd="bash '$COMPOSE_SH'"
        else
            log "WARN: $COMPOSE_SH not present after restore; recovering with bare"
            log "WARN: docker compose - the per-install override will NOT apply."
            log "WARN: re-assert it once healthy: bash chassis/scripts/compose.sh up -d"
        fi
    fi

    if [[ -n "$pinned_image" ]]; then
        log "Pinning container back to pre-update image: $pinned_image"
        dry_or_run_soft "$up_prefix CHASSIS_IMAGE='$pinned_image' $up_cmd up -d --force-recreate"
    else
        log "WARN: no pre-update image recorded for this snapshot. The disk tree is"
        log "WARN: restored but the container will come back up on whatever"
        log "WARN: CHASSIS_IMAGE currently resolves to. Verify the running version by hand."
        dry_or_run_soft "$up_prefix $up_cmd up -d"
    fi
}

# --- Rollback path (independent of normal apply flow) ---
if [[ $ROLLBACK -eq 1 ]]; then
    LATEST_BACKUP=$(ls -1t "$BACKUP_DIR"/chassis-pre-v*.tgz 2>/dev/null | head -1)
    [[ -z "$LATEST_BACKUP" ]] && die "no backups found in $BACKUP_DIR"
    restore_snapshot "$LATEST_BACKUP" "$CHASSIS_HOME"
    log "Rollback complete. Verify health manually."
    exit 0
fi

# --- Step 0: read current and upstream version ---
[[ -f "$LOCAL_VERSION_FILE" ]] || die "missing $LOCAL_VERSION_FILE - cannot determine current version"
CURRENT_VERSION=$(tr -d '[:space:]' < "$LOCAL_VERSION_FILE")
UPSTREAM_VERSION=$(curl --silent --fail --max-time 10 "${UPSTREAM_RAW_BASE}/chassis/VERSION" 2>/dev/null | tr -d '[:space:]')
[[ -z "$UPSTREAM_VERSION" ]] && die "could not fetch upstream VERSION from ${UPSTREAM_RAW_BASE}/chassis/VERSION"

log "Current: v$CURRENT_VERSION"
log "Latest:  v$UPSTREAM_VERSION"

# --- Step 0.5: fetch the upstream changelog ---
# Moved above the version gate (#147): the drift decision below needs it, and
# the BREAKING gate in step 2 then reuses the same file rather than fetching
# twice.
CHANGELOG_PATH="${STATE_DIR}/upstream-changelog.md"
CHANGELOG_FETCHED=0
if curl --silent --fail --max-time 10 -o "$CHANGELOG_PATH" "${UPSTREAM_RAW_BASE}/chassis/CHANGELOG.md" 2>/dev/null; then
    CHANGELOG_FETCHED=1
fi

# --- Step 0.6: is there anything to apply? ---
#
# UPDATE_KIND=version - upstream VERSION is newer. The path this script has
#                       always taken.
# UPDATE_KIND=drift   - the versions match but upstream main carries unreleased
#                       changes this tree does not have (#147).
#
# The equality check used to exit 0 here unconditionally, which meant that even
# once the CHECKER could see drift, the APPLIER would answer `update chassis`
# with "Already up to date. Exiting." A notification the apply path refuses to
# act on is worse than no notification.
UPDATE_KIND="version"
UPSTREAM_UNRELEASED_DIGEST=""
LOCAL_UNRELEASED_DIGEST=""
if [[ "$CURRENT_VERSION" == "$UPSTREAM_VERSION" ]]; then
    if [[ $DRIFT_CAPABLE -eq 1 && $CHANGELOG_FETCHED -eq 1 ]]; then
        UPSTREAM_UNRELEASED_DIGEST=$(chassis_unreleased_digest "$CHANGELOG_PATH" 2>/dev/null || echo "")
        LOCAL_UNRELEASED_DIGEST=$(chassis_unreleased_digest "$LOCAL_CHANGELOG_FILE" 2>/dev/null || echo "")
    fi
    if [[ -z "$UPSTREAM_UNRELEASED_DIGEST" || -z "$LOCAL_UNRELEASED_DIGEST" ]]; then
        log "Already up to date at v$CURRENT_VERSION."
        log "Note: could not compare the unreleased changelog sections, so this"
        log "Note: cannot rule out unreleased changes on main. Missing"
        log "Note: ${LOCAL_CHANGELOG_FILE}, an unreachable upstream changelog,"
        log "Note: or a torn tree missing _chassis-changelog.sh."
        exit 0
    fi
    if [[ "$UPSTREAM_UNRELEASED_DIGEST" == "empty" ]]; then
        log "Already up to date. Exiting."
        exit 0
    fi
    if [[ "$UPSTREAM_UNRELEASED_DIGEST" == "$LOCAL_UNRELEASED_DIGEST" ]]; then
        log "Already up to date. Exiting."
        exit 0
    fi
    UPDATE_KIND="drift"
    log "VERSION is level at v$CURRENT_VERSION, but main carries unreleased changes"
    log "this tree does not have: unreleased section ${LOCAL_UNRELEASED_DIGEST} here,"
    log "${UPSTREAM_UNRELEASED_DIGEST} upstream. Applying that drift."
fi

# --- Step 1: pre-flight ---
log "Pre-flight: working tree clean check..."
if [[ "$MODE" == "overlay_mount" ]]; then
    # Ask the repo the pull will actually run in. Checking CHASSIS_HOME here was
    # the same wrong-repo defect as the mode detection, and on the reference
    # install it was worse than wrong: git refuses that directory outright, so
    # the check returned empty and passed vacuously.
    #
    # Whole repo, because `git pull --ff-only` in the clone is blocked by any
    # modified TRACKED file, not only ones under chassis/. Untracked files
    # cannot block a fast-forward, so -uno keeps a stray scratch file from
    # bricking the supported update path.
    DIRTY=$(git -C "$CHASSIS_REPO_DIR" status --porcelain -uno 2>/dev/null | head)
    DIRTY_HINT="git -C '$CHASSIS_REPO_DIR' stash push"
else
    DIRTY=$(cd "$CHASSIS_HOME" && git status --porcelain -- chassis/ 2>/dev/null | head)
    DIRTY_HINT="git -C '$CHASSIS_HOME' stash push -- chassis/"
fi
if [[ -n "$DIRTY" ]]; then
    cat <<EOF >&2
Pre-flight FAILED: dirty working tree in $CHASSIS_REPO_DIR ($MODE).
Local edits would be clobbered by an update. Listing:

$DIRTY

Resolve by upstreaming the change or stashing it:
  $DIRTY_HINT
EOF
    exit 1
fi

# --- Step 1.5: pre-flight - a missing override is not a default install ---
#
# When no override is on disk and none is configured, this is EITHER a plain
# default install (fine - proceed bare, exactly as before) OR an install whose
# override has gone missing, where proceeding would silently revert the
# customer's compose configuration: the exact #100 failure. The two are
# distinguishable, because the docker engine records the -f files a stack was
# built with on its containers. If the running chassis container was built
# WITH an override that no longer exists, refuse - loudly, and BEFORE the
# pull mutates the tree. We deliberately do not create an override to make
# the problem disappear. Applies to --dry-run too: it is a truthful prediction
# that the real run would be refused.
if [[ $USE_COMPOSE_SH -eq 0 && -f "${COMPOSE_DIR}/docker-compose.yml" ]] && command -v docker >/dev/null 2>&1; then
    existing_container=$(chassis_find_container "$COMPOSE_DIR")
    if [[ -n "$existing_container" ]]; then
        built_with=$(docker inspect "$existing_container" \
            --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' 2>/dev/null)
        if [[ "$built_with" == *"chassis-compose.override"* ]]; then
            log "FATAL: the running stack was built with a compose override:"
            log "FATAL:   $built_with"
            log "FATAL: but no override exists at $OVERRIDE_FILE now. Proceeding would"
            log "FATAL: silently revert this install's compose configuration (published"
            log "FATAL: ports, image pins, scaled-to-0 services) - behalfbot#100."
            log "FATAL: Restore the override file, or point CHASSIS_COMPOSE_OVERRIDE at its"
            log "FATAL: location, or set CHASSIS_COMPOSE_OVERRIDE= (empty) if running"
            log "FATAL: override-less is genuinely intended. Not creating one for you."
            die "refusing to update: compose override missing but the running stack was built with one"
        fi
    fi
fi

# --- Step 2: BREAKING CHANGES gate ---
#
# Window matches gather-chassis-update-check.sh exactly: `## Unreleased` down to
# (not including) the local version's heading. The unreleased section is in
# scope because a pull of main delivers it (#147), and capture must not start
# at the top of the file because the format-conventions preamble contains the
# literal marker text.
if [[ $CHANGELOG_FETCHED -eq 1 ]]; then
    WINDOW=$(awk -v upstream="## v${UPSTREAM_VERSION}" -v local_v="## v${CURRENT_VERSION}" '
        /^## Unreleased/ { capture = 1 }
        $0 ~ "^"upstream { capture = 1 }
        $0 ~ "^"local_v { capture = 0 }
        capture { print }
    ' "$CHANGELOG_PATH")
    if printf '%s\n' "$WINDOW" | grep -q "BREAKING CHANGES:"; then
        if [[ $FORCE -ne 1 ]]; then
            cat <<EOF >&2
BREAKING CHANGES detected in what this update would deliver (v$CURRENT_VERSION to v$UPSTREAM_VERSION, unreleased section included).
Review the changelog: ${UPSTREAM_RAW_BASE}/chassis/CHANGELOG.md
Re-run with --force to apply after review.
EOF
            exit 1
        fi
        log "BREAKING CHANGES present; --force supplied, proceeding."
    fi
fi

# --- Step 3: snapshot ---
SNAPSHOT="${BACKUP_DIR}/chassis-pre-v${UPSTREAM_VERSION}-$(date -u +%Y%m%dT%H%M%SZ).tgz"
log "Snapshot: $SNAPSHOT"
dry_or_run "cd '$CHASSIS_HOME' && tar czf '$SNAPSHOT' chassis/"

# Record the image the container is on right now, so a rollback can pin back to
# it. Without this the container half of a rollback is a no-op: restoring the
# source tree does nothing to a container running a published image.
PRE_UPDATE_CONTAINER=$(chassis_find_container "$CHASSIS_HOME")
if [[ $DRY_RUN -eq 0 && -n "$PRE_UPDATE_CONTAINER" ]]; then
    PRE_UPDATE_IMAGE=$(chassis_container_image "$PRE_UPDATE_CONTAINER" || echo "")
    if [[ -n "$PRE_UPDATE_IMAGE" ]]; then
        printf '%s\n' "$PRE_UPDATE_IMAGE" > "${SNAPSHOT%.tgz}.image"
        log "Pre-update image recorded: $PRE_UPDATE_IMAGE"
    else
        log "WARN: could not read the current image of container '$PRE_UPDATE_CONTAINER'."
        log "WARN: a rollback will restore the disk tree but not the container image."
    fi
fi

# --- Step 4: drain in-flight heartbeats ---
log "Drain: waiting for in-flight heartbeat (timeout ${DRAIN_TIMEOUT_SECONDS}s)..."
if [[ $DRY_RUN -eq 0 ]]; then
    drained=0
    for ((i=0; i<DRAIN_TIMEOUT_SECONDS; i++)); do
        if [[ ! -f "$DRAIN_LOCK_FILE" ]]; then
            drained=1
            break
        fi
        sleep 1
    done
    if [[ $drained -eq 0 ]]; then
        log "WARN: dispatcher lock still held after ${DRAIN_TIMEOUT_SECONDS}s; proceeding anyway"
    fi
fi

# --- Step 4.5: snapshot the effective compose config before the pull ---
# Evidence for the operator: what the stack's merged config looked like BEFORE
# this update touched anything. Diffed against the post-up snapshot in step 7.
if [[ -f "${COMPOSE_DIR}/docker-compose.yml" ]]; then
    snapshot_config "$CONFIG_PRE"
fi

# --- Step 5: pull upstream ---
case "$MODE" in
    canonical_clone|overlay_mount)
        # Same command, different repo. CHASSIS_REPO_DIR is CHASSIS_HOME for a
        # canonical clone and the overlaid behalfbot clone otherwise.
        dry_or_run "cd '$CHASSIS_REPO_DIR' && git pull --ff-only origin $UPSTREAM_BRANCH"
        ;;
    vendored_subtree)
        # Ensure the chassis remote exists (idempotent: ignore "already exists")
        if [[ $DRY_RUN -eq 0 ]]; then
            (cd "$CHASSIS_REPO_DIR" && git remote add "$UPSTREAM_REMOTE_NAME" "$UPSTREAM_REMOTE_URL" 2>/dev/null) || true
        fi
        dry_or_run "cd '$CHASSIS_REPO_DIR' && git subtree pull --prefix=chassis '$UPSTREAM_REMOTE_NAME' '$UPSTREAM_BRANCH' --squash -m 'chore(chassis): pull v$UPSTREAM_VERSION (#33)'"
        ;;
    *)
        die "unknown mode: $MODE"
        ;;
esac

# --- Step 5.5: did the pull actually deliver a new tree? ---
#
# behalfbot#152 defect 3. The pull step can return 0 having moved nothing (wrong
# repo, already-merged subtree squash, a ff-only pull with no new commits), and
# everything after this point - healthcheck, migration, last-applied.json, the
# final status line - then describes an update that did not happen. VERSION on
# the chassis tree is the ground truth for what landed, so read it back before
# building anything else on top of the claim.
#
# LOCAL_VERSION_FILE is SCRIPT_DIR/../VERSION, so a real run must be launched
# from the chassis tree's own copy of this script. Running a detached copy (the
# baked image tree, a scratch dir) still pulls the correct repo but then reads
# back its own untouched VERSION and fails here. Use --dry-run for those.
#
# A drift apply moves no version by definition, so VERSION is the wrong ground
# truth for it and this check would fail every one of them. The equivalent
# evidence for drift is the tree's own `## Unreleased` section: the run set out
# to deliver a specific upstream digest, so after the pull the local section
# must hash to it.
POST_PULL_VERSION="$CURRENT_VERSION"
POST_PULL_DIGEST="$LOCAL_UNRELEASED_DIGEST"
if [[ $DRY_RUN -eq 0 ]]; then
    POST_PULL_VERSION=$(tr -d '[:space:]' < "$LOCAL_VERSION_FILE" 2>/dev/null || echo "")
    if [[ -z "$POST_PULL_VERSION" ]]; then
        die "pull reported success but $LOCAL_VERSION_FILE is now unreadable (mode: $MODE, repo: $CHASSIS_REPO_DIR)"
    fi
    if [[ "$UPDATE_KIND" == "drift" ]]; then
        POST_PULL_DIGEST=$(chassis_unreleased_digest "$LOCAL_CHANGELOG_FILE" 2>/dev/null || echo "")
        if [[ -z "$POST_PULL_DIGEST" ]]; then
            die "pull reported success but $LOCAL_CHANGELOG_FILE is now unreadable (mode: $MODE, repo: $CHASSIS_REPO_DIR)"
        fi
        if [[ "$POST_PULL_DIGEST" != "$UPSTREAM_UNRELEASED_DIGEST" ]]; then
            log "FAIL: the pull returned success but this tree's unreleased changelog"
            log "FAIL: section still hashes to ${POST_PULL_DIGEST}, not the"
            log "FAIL: ${UPSTREAM_UNRELEASED_DIGEST} this run set out to apply. Nothing was"
            log "FAIL: delivered, so there is no update to healthcheck."
            die "pull did not deliver the unreleased changes (mode: $MODE, repo: $CHASSIS_REPO_DIR)"
        fi
    elif [[ "$POST_PULL_VERSION" == "$CURRENT_VERSION" ]]; then
        log "FAIL: the pull returned success but $LOCAL_VERSION_FILE still reads"
        log "FAIL: v$CURRENT_VERSION. Nothing was delivered, so there is no update"
        log "FAIL: to healthcheck and nothing to report as complete."
        die "pull did not advance the chassis tree (mode: $MODE, repo: $CHASSIS_REPO_DIR)"
    fi
    if [[ "$UPDATE_KIND" == "version" && "$POST_PULL_VERSION" != "$UPSTREAM_VERSION" ]]; then
        log "WARN: the tree advanced to v$POST_PULL_VERSION, not the v$UPSTREAM_VERSION this"
        log "WARN: run set out to apply - upstream most likely moved mid-update. The"
        log "WARN: healthcheck below still requires v$UPSTREAM_VERSION."
    fi
fi

# --- Step 6: docker compose pull + up (through compose.sh, behalfbot#100) ---
#
# CONTAINER_REFRESHED is carried to the final status line: a skipped refresh
# used to be a single mid-log note under a closing line that said the update was
# complete, which is how a containerized install could go on running the old
# code while the operator read "complete" (behalfbot#152 defect 2).
CONTAINER_REFRESHED="no"
CONTAINER_REFRESH_NOTE="no docker-compose.yml at $COMPOSE_DIR"
if [[ ! -f "${COMPOSE_DIR}/docker-compose.yml" ]]; then
    log "No docker-compose.yml at $COMPOSE_DIR; skipping container refresh"
else
    if [[ $USE_COMPOSE_SH -eq 1 ]]; then
        # The step-5 pull delivers compose.sh when updating from a pre-v0.2.0
        # tree, so in a real run it must exist by now. Never fall back to bare
        # compose - that IS the bug this step replaces. The dry-run path never
        # pulled, so it only reports the plan.
        if [[ $DRY_RUN -eq 0 && ! -f "$COMPOSE_SH" ]]; then
            die "compose.sh not found at $COMPOSE_SH after the upstream pull - refusing to fall back to bare docker compose (behalfbot#100)"
        fi
        log "Compose override in effect: ${OVERRIDE_FILE:-<none - explicitly disabled via CHASSIS_COMPOSE_OVERRIDE>}"
    else
        log "WARN: no compose override at $OVERRIDE_FILE - bringing the stack up on chassis defaults."
        log "WARN: real installs carry an override (env_file, image pins, published ports);"
        log "WARN: see docs/per-customer-repo-pattern.md. Not creating one on your behalf."
    fi
    dry_or_run "$(compose_invoke "pull")"
    dry_or_run "$(compose_invoke "up -d")"
    CONTAINER_REFRESHED="yes"
    CONTAINER_REFRESH_NOTE=""
fi

# --- Step 7: healthcheck ---
#
# The contract, decided ONCE before the poll starts rather than per-iteration:
#
#   container mode - docker is present and there is a compose file, so the code
#       that runs lives in an image. The ONLY acceptable evidence is the running
#       container reporting the new VERSION. There is no disk fallback. If the
#       container cannot be found or cannot be read, that is a failure.
#   host mode - no docker, or no compose file. Nothing was containerized, so the
#       VERSION file on disk is the real artifact and checking it is honest.
#
# The old code tried container mode inside the loop and fell through to the disk
# check on any miss. Since the disk check compares the file the subtree pull
# just wrote against the upstream value it was pulled from, it passed
# unconditionally - which is why the rollback below had never once fired.
#
# For a drift apply the artifact is the same but the evidence is not: VERSION
# does not move, so the version probe would pass on the first poll whether or
# not the container was ever recreated. The container's own `## Unreleased`
# digest is the honest equivalent, and it is read out and hashed HOST-side so
# the comparison does not depend on what hash tools the image carries.
HEALTHCHECK_MODE=$(chassis_healthcheck_mode "$COMPOSE_DIR")
log "Healthcheck: ${HEALTHCHECK_MODE} mode, ${UPDATE_KIND} update, polling (timeout ${HEALTHCHECK_TIMEOUT_SECONDS}s)..."
if [[ $DRY_RUN -eq 0 ]]; then
    healthy=0
    last_reason="no poll ran"
    for ((i=0; i<HEALTHCHECK_TIMEOUT_SECONDS; i++)); do
        if [[ "$HEALTHCHECK_MODE" == "container" ]]; then
            CONTAINER_NAME=$(chassis_find_container "$COMPOSE_DIR")
            if [[ -z "$CONTAINER_NAME" ]]; then
                last_reason="no running chassis container found"
            elif [[ "$UPDATE_KIND" == "drift" ]]; then
                RUNNING_DIGEST=$(chassis_container_changelog "$CONTAINER_NAME" \
                    | chassis_unreleased_digest -) || RUNNING_DIGEST=""
                if [[ -z "$RUNNING_DIGEST" || "$RUNNING_DIGEST" == "empty" ]]; then
                    last_reason="container '$CONTAINER_NAME' is up but its CHANGELOG could not be read"
                elif [[ "$RUNNING_DIGEST" == "$UPSTREAM_UNRELEASED_DIGEST" ]]; then
                    healthy=1
                    log "Container '$CONTAINER_NAME' carries unreleased section $RUNNING_DIGEST"
                    break
                else
                    last_reason="container '$CONTAINER_NAME' still carries unreleased section $RUNNING_DIGEST, expected $UPSTREAM_UNRELEASED_DIGEST"
                fi
            else
                RUNNING_VERSION=$(chassis_container_version "$CONTAINER_NAME") || RUNNING_VERSION=""
                if [[ -z "$RUNNING_VERSION" ]]; then
                    last_reason="container '$CONTAINER_NAME' is up but VERSION could not be read from it"
                elif [[ "$RUNNING_VERSION" == "$UPSTREAM_VERSION" ]]; then
                    healthy=1
                    log "Container '$CONTAINER_NAME' reports v$RUNNING_VERSION"
                    break
                else
                    last_reason="container '$CONTAINER_NAME' still reports v$RUNNING_VERSION, expected v$UPSTREAM_VERSION"
                fi
            fi
        elif [[ "$UPDATE_KIND" == "drift" ]]; then
            # Host mode: the disk tree IS the artifact, and step 5.5 already
            # required this digest to match before getting here. Re-reading it
            # keeps the two modes symmetrical and costs nothing.
            DISK_DIGEST=$(chassis_unreleased_digest "$LOCAL_CHANGELOG_FILE" 2>/dev/null || echo "")
            if [[ "$DISK_DIGEST" == "$UPSTREAM_UNRELEASED_DIGEST" ]]; then
                healthy=1
                log "Host-mode unreleased section on disk is $DISK_DIGEST"
                break
            fi
            last_reason="unreleased section on disk is ${DISK_DIGEST:-<unreadable>}, expected $UPSTREAM_UNRELEASED_DIGEST"
        else
            DISK_VERSION=$(tr -d '[:space:]' < "$LOCAL_VERSION_FILE" 2>/dev/null || echo "")
            if [[ "$DISK_VERSION" == "$UPSTREAM_VERSION" ]]; then
                healthy=1
                log "Host-mode VERSION on disk is v$DISK_VERSION"
                break
            fi
            last_reason="VERSION on disk is v${DISK_VERSION:-<unreadable>}, expected v$UPSTREAM_VERSION"
        fi
        sleep 1
    done
    if [[ $healthy -eq 0 ]]; then
        log "FAIL: healthcheck did not converge within ${HEALTHCHECK_TIMEOUT_SECONDS}s"
        log "FAIL: last observed state - $last_reason"
        restore_snapshot "$SNAPSHOT" "$COMPOSE_DIR"
        die "update failed and was rolled back"
    fi
fi

# --- Step 7.5: verify the merged compose config is actually running ---
#
# behalfbot#100: the version healthcheck above proves the chassis container is
# up on the new code - over the INTERNAL compose network. It is structurally
# blind to the things the per-install override changes: published host ports,
# scaled-to-0 services, image pins. On the install that motivated this, the
# update dropped the override, postgres stopped publishing 127.0.0.1:5432,
# every host-side consumer broke, a watchdog bounced the whole VM - and the
# healthcheck reported green throughout. So: render the merged config the
# stack SHOULD be running and check the docker engine against it.
#
# With an override in play a mismatch is a failed update (rollback + die) -
# reverting a customer's compose configuration is not a warning-level event.
# Without one (plain default install) the check still runs but only warns,
# preserving the no-override contract exactly.
if [[ $DRY_RUN -eq 0 && "$HEALTHCHECK_MODE" == "container" ]]; then
    verify_failed=0
    verify_msgs=""
    CONFIG_POST_JSON="${STATE_DIR}/compose-config-post.json"

    if eval "$(compose_invoke "config --format json")" > "$CONFIG_POST_JSON" 2>"${CONFIG_POST_JSON}.err" && [[ -s "$CONFIG_POST_JSON" ]]; then
        chmod 600 "$CONFIG_POST_JSON"
        rm -f "${CONFIG_POST_JSON}.err"
        MERGED_PROJECT=$(jq -r '.name // empty' "$CONFIG_POST_JSON")

        verify_msgs=$(compose_verify_running_config "$CONFIG_POST_JSON") || verify_failed=1

        # Direct evidence the engine built the stack WITH the override: the
        # compose config_files label on the chassis container.
        if [[ $USE_COMPOSE_SH -eq 1 && -n "$OVERRIDE_FILE" ]]; then
            compose_override_in_config_files "$MERGED_PROJECT" "$OVERRIDE_FILE"
            case $? in
                1)
                    verify_msgs="${verify_msgs}${verify_msgs:+$'\n'}VERIFY-FAIL: chassis container's compose config_files label does not include $OVERRIDE_FILE - the stack was brought up without the per-install override"
                    verify_failed=1
                    ;;
                2)
                    verify_msgs="${verify_msgs}${verify_msgs:+$'\n'}VERIFY-FAIL: no running chassis container found in project '$MERGED_PROJECT' to inspect for the override"
                    verify_failed=1
                    ;;
            esac
        fi
    else
        verify_msgs="VERIFY-FAIL: could not render the merged compose config ($(head -c 300 "${CONFIG_POST_JSON}.err" 2>/dev/null | tr '\n' ' '))"
        verify_failed=1
    fi

    if [[ $verify_failed -eq 1 ]]; then
        while IFS= read -r line; do log "$line"; done <<<"$verify_msgs"
        if [[ $USE_COMPOSE_SH -eq 1 ]]; then
            log "FAIL: the running stack does not match the merged compose config - the"
            log "FAIL: per-install override was not (fully) applied. Rolling back."
            restore_snapshot "$SNAPSHOT" "$COMPOSE_DIR"
            die "update failed config verification and was rolled back (behalfbot#100)"
        fi
        log "WARN: running stack does not match the compose config (no override install;"
        log "WARN: not failing the update). Review the lines above."
    else
        log "Config verification passed: declared ports published, scaled-to-0 services down."
    fi

    # Operator-facing evidence: did the effective config change across the
    # update? Snapshot post-up and diff against the pre-pull snapshot. The
    # diff content can carry interpolated secrets, so it stays in STATE_DIR -
    # only the fact of drift and the path are logged.
    snapshot_config "$CONFIG_POST"
    if [[ -f "$CONFIG_PRE" && -f "$CONFIG_POST" ]]; then
        if diff -u "$CONFIG_PRE" "$CONFIG_POST" > "$CONFIG_DIFF" 2>/dev/null; then
            log "Effective compose config unchanged across the update."
            rm -f "$CONFIG_DIFF"
        else
            chmod 600 "$CONFIG_DIFF"
            log "NOTICE: effective compose config CHANGED across the update ($(grep -c '^[+-]' "$CONFIG_DIFF") diff lines)."
            log "NOTICE: review: $CONFIG_DIFF"
        fi
    fi
fi

# --- Step 8: run migration script if present ---
# Migrations are strictly automated shell scripts. Judgment-heavy migrations
# would have been flagged BREAKING CHANGES and gated behind --force above.
#
# The `-f` test below is false for a path under a missing directory just as
# it is for a missing file, so versions without a migration no-op here. First
# shipped migration: v0.3.0.sh (behalfbot#100 override repair).
# Resolved relative to this script (like LOCAL_VERSION_FILE), not
# ${CHASSIS_HOME}/chassis/scripts/...: that literal path only exists in
# canonical-clone mode. In vendored-subtree mode the pulled repo root lands
# UNDER chassis/, so the old path silently skipped every migration there.
#
# Migrations are keyed to a version bump, so a drift apply must not run one:
# vX.Y.Z.sh already ran when vX.Y.Z was applied, and re-running it is a second
# execution of a state mutation nobody asked for. A migration that a drift
# delivers gets run when its release is cut.
MIGRATION_SCRIPT="${SCRIPT_DIR}/chassis-migrations/v${UPSTREAM_VERSION}.sh"
if [[ "$UPDATE_KIND" == "version" && -f "$MIGRATION_SCRIPT" ]]; then
    log "Running migration: $MIGRATION_SCRIPT"
    dry_or_run "bash '$MIGRATION_SCRIPT'"
elif [[ "$UPDATE_KIND" == "drift" && -f "$MIGRATION_SCRIPT" ]]; then
    log "Skipping $MIGRATION_SCRIPT: it belongs to the v${UPSTREAM_VERSION} bump, which"
    log "Skipping: this install already applied. Drift applies run no migration."
fi

# --- Step 9: record what was applied ---
# `to` is the version read back off the tree after the pull, not the version
# this run set out to apply. Those differ exactly when the update did not do
# what it intended, which is the case worth recording accurately.
#
# `kind` and the two digests are recorded because a drift apply has
# from == to, which is otherwise indistinguishable from the pathology #147
# describes: a hand-run `git pull` that self-applies unreleased code while
# last-applied.json still names an old version pair. With the digests in the
# record, "we applied drift on top of v0.5.0" is a fact, not an inference.
if [[ $DRY_RUN -eq 0 ]]; then
    jq -n \
        --arg kind "$UPDATE_KIND" \
        --arg from "$CURRENT_VERSION" \
        --arg to "$POST_PULL_VERSION" \
        --arg from_digest "$LOCAL_UNRELEASED_DIGEST" \
        --arg to_digest "$POST_PULL_DIGEST" \
        --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --arg snapshot "$SNAPSHOT" \
        --arg mode "$MODE" \
        --arg repo "$CHASSIS_REPO_DIR" \
        --argjson container_refreshed "$([[ "$CONTAINER_REFRESHED" == "yes" ]] && echo true || echo false)" \
        '{"kind": $kind, "from": $from, "to": $to,
          "from_unreleased_digest": $from_digest, "to_unreleased_digest": $to_digest,
          "applied_at": $ts, "snapshot": $snapshot,
          "mode": $mode, "repo": $repo, "container_refreshed": $container_refreshed}' \
        > "${STATE_DIR}/last-applied.json"
fi

# --- Step 10: report what actually happened (behalfbot#152 defect 3) ---
#
# The old final line was a constant - `Update complete: vX → vY` printed
# whatever the run did, including a dry run that changed nothing and a run whose
# container was never refreshed. An operator who reads only the last line has to
# be able to trust it.
#
# A skipped refresh is not automatically a failure: an install with no docker
# and no compose file legitimately runs the dispatcher on the host, and its disk
# tree IS the artifact. What makes it a partial update is a chassis container
# that is up and therefore still executing the previous code.
STALE_CONTAINER=""
if [[ "$CONTAINER_REFRESHED" == "no" ]] && command -v docker >/dev/null 2>&1; then
    STALE_CONTAINER=$(chassis_find_container "$COMPOSE_DIR")
fi

# What this run set out to deliver, in one phrase, for the closing lines. A
# drift apply that reported `v0.5.0 → v0.5.0` would read as the no-op it is not.
if [[ "$UPDATE_KIND" == "drift" ]]; then
    PLAN_DESC="v$CURRENT_VERSION unreleased $LOCAL_UNRELEASED_DIGEST → $UPSTREAM_UNRELEASED_DIGEST (VERSION unchanged)"
    DONE_DESC="v$POST_PULL_VERSION unreleased $LOCAL_UNRELEASED_DIGEST → $POST_PULL_DIGEST (VERSION unchanged)"
else
    PLAN_DESC="v$CURRENT_VERSION → v$UPSTREAM_VERSION"
    DONE_DESC="v$CURRENT_VERSION → v$POST_PULL_VERSION"
fi

if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY-RUN complete: nothing was changed."
    log "DRY-RUN plan was: $PLAN_DESC via $MODE in $CHASSIS_REPO_DIR"
    if [[ "$CONTAINER_REFRESHED" == "no" ]]; then
        log "DRY-RUN: container refresh WOULD BE SKIPPED - $CONTAINER_REFRESH_NOTE"
        if [[ -n "$STALE_CONTAINER" ]]; then
            log "DRY-RUN: container '$STALE_CONTAINER' would keep running its current code"
        fi
    fi
    exit 0
fi

if [[ "$CONTAINER_REFRESHED" == "yes" ]]; then
    log "Update complete: $DONE_DESC (tree pulled, container refreshed)"
    exit 0
fi

if [[ -n "$STALE_CONTAINER" ]]; then
    log "Update PARTIAL: the chassis tree is now $DONE_DESC but no container"
    log "Update PARTIAL: was refreshed - $CONTAINER_REFRESH_NOTE."
    log "Update PARTIAL: container '$STALE_CONTAINER' is still running its previous code."
    log "Update PARTIAL: bring the stack up from wherever its compose file lives, then re-check."
    exit 3
fi

log "Update complete (host install): $DONE_DESC (tree pulled; no container to refresh - $CONTAINER_REFRESH_NOTE)"
