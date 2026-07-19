#!/bin/bash
# chassis-update.sh - apply a chassis version bump after operator consent.
#
# Issue: scrollinondubs/behalfbot#33 (Apple-style chassis auto-updater).
#
# Idempotent script that updates the chassis to upstream main:
#   1. Pre-flight (clean working tree)
#   2. Snapshot pre-update state
#   3. Drain in-flight heartbeats (state file lock)
#   4. Pull upstream (canonical-clone mode: git pull; vendored mode: git subtree pull)
#   5. Docker compose pull + up -d
#   6. Healthcheck poll (60s)
#   7. Run migration script if present for the new version
#   8. Post success / failure to the alerts channel
#
# Usage:
#   chassis-update.sh                # apply non-breaking update
#   chassis-update.sh --force        # apply BREAKING-CHANGE update (operator reviewed)
#   chassis-update.sh --dry-run      # print plan, don't execute
#   chassis-update.sh --rollback     # restore the most recent pre-update snapshot
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
# Container discovery + VERSION probe. Split into a sourceable lib so the
# healthcheck logic is testable without running a real update.
# shellcheck source=chassis/scripts/_chassis-update-health.sh
source "${SCRIPT_DIR}/_chassis-update-health.sh"
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

# --- Mode detection ---
# canonical_clone: CHASSIS_HOME's git origin is scrollinondubs/behalfbot itself.
#                  Update with `git pull --ff-only`.
# vendored_subtree: CHASSIS_HOME is a customer repo with chassis/ pulled in via
#                   `git subtree`. Update with `git subtree pull`.
detect_mode() {
    local origin_url
    origin_url=$(cd "$CHASSIS_HOME" && git config --get remote.origin.url 2>/dev/null || echo "")
    if [[ "$origin_url" == *"scrollinondubs/behalfbot"* ]]; then
        echo "canonical_clone"
    else
        echo "vendored_subtree"
    fi
}

MODE=$(detect_mode)
log "Mode: $MODE"

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
    if [[ -n "$pinned_image" ]]; then
        log "Pinning container back to pre-update image: $pinned_image"
        dry_or_run_soft "cd '$compose_dir' && CHASSIS_IMAGE='$pinned_image' docker compose up -d --force-recreate"
    else
        log "WARN: no pre-update image recorded for this snapshot. The disk tree is"
        log "WARN: restored but the container will come back up on whatever"
        log "WARN: CHASSIS_IMAGE currently resolves to. Verify the running version by hand."
        dry_or_run_soft "cd '$compose_dir' && docker compose up -d"
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

if [[ "$CURRENT_VERSION" == "$UPSTREAM_VERSION" ]]; then
    log "Already up to date. Exiting."
    exit 0
fi

# --- Step 1: pre-flight ---
log "Pre-flight: working tree clean check..."
DIRTY=$(cd "$CHASSIS_HOME" && git status --porcelain -- chassis/ 2>/dev/null | head)
if [[ -n "$DIRTY" ]]; then
    cat <<EOF >&2
Pre-flight FAILED: dirty chassis/ working tree.
Local edits in chassis/ would be clobbered by an update. Listing:

$DIRTY

Resolve by upstreaming the change or stashing it:
  git -C "$CHASSIS_HOME" stash push -- chassis/
EOF
    exit 1
fi

# --- Step 2: BREAKING CHANGES gate ---
CHANGELOG_PATH="${STATE_DIR}/upstream-changelog.md"
if curl --silent --fail --max-time 10 -o "$CHANGELOG_PATH" "${UPSTREAM_RAW_BASE}/chassis/CHANGELOG.md" 2>/dev/null; then
    WINDOW=$(awk -v upstream="## v${UPSTREAM_VERSION}" -v local_v="## v${CURRENT_VERSION}" '
        $0 ~ "^"upstream { capture = 1 }
        $0 ~ "^"local_v { capture = 0 }
        capture { print }
    ' "$CHANGELOG_PATH")
    if printf '%s\n' "$WINDOW" | grep -q "BREAKING CHANGES:"; then
        if [[ $FORCE -ne 1 ]]; then
            cat <<EOF >&2
BREAKING CHANGES detected between v$CURRENT_VERSION and v$UPSTREAM_VERSION.
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

# --- Step 5: pull upstream ---
case "$MODE" in
    canonical_clone)
        dry_or_run "cd '$CHASSIS_HOME' && git pull --ff-only origin $UPSTREAM_BRANCH"
        ;;
    vendored_subtree)
        # Ensure the chassis remote exists (idempotent: ignore "already exists")
        if [[ $DRY_RUN -eq 0 ]]; then
            (cd "$CHASSIS_HOME" && git remote add "$UPSTREAM_REMOTE_NAME" "$UPSTREAM_REMOTE_URL" 2>/dev/null) || true
        fi
        dry_or_run "cd '$CHASSIS_HOME' && git subtree pull --prefix=chassis '$UPSTREAM_REMOTE_NAME' '$UPSTREAM_BRANCH' --squash -m 'chore(chassis): pull v$UPSTREAM_VERSION (#33)'"
        ;;
    *)
        die "unknown mode: $MODE"
        ;;
esac

# --- Step 6: docker compose pull + up ---
COMPOSE_DIR="$CHASSIS_HOME"
if [[ ! -f "${COMPOSE_DIR}/docker-compose.yml" ]]; then
    log "No docker-compose.yml at $COMPOSE_DIR; skipping container refresh"
else
    dry_or_run "cd '$COMPOSE_DIR' && docker compose pull"
    dry_or_run "cd '$COMPOSE_DIR' && docker compose up -d"
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
HEALTHCHECK_MODE=$(chassis_healthcheck_mode "$COMPOSE_DIR")
log "Healthcheck: ${HEALTHCHECK_MODE} mode, polling (timeout ${HEALTHCHECK_TIMEOUT_SECONDS}s)..."
if [[ $DRY_RUN -eq 0 ]]; then
    healthy=0
    last_reason="no poll ran"
    for ((i=0; i<HEALTHCHECK_TIMEOUT_SECONDS; i++)); do
        if [[ "$HEALTHCHECK_MODE" == "container" ]]; then
            CONTAINER_NAME=$(chassis_find_container "$COMPOSE_DIR")
            if [[ -z "$CONTAINER_NAME" ]]; then
                last_reason="no running chassis container found"
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

# --- Step 8: run migration script if present ---
# Migrations are strictly automated shell scripts. Judgment-heavy migrations
# would have been flagged BREAKING CHANGES and gated behind --force above.
#
# chassis/scripts/chassis-migrations/ does not exist in the repo yet - no
# release has needed a state migration. That is fine and deliberate: the `-f`
# test below is false for a path under a missing directory just as it is for a
# missing file, so the step no-ops. The directory gets created by whichever
# release first ships a migration. Do not pre-create it empty.
MIGRATION_SCRIPT="${CHASSIS_HOME}/chassis/scripts/chassis-migrations/v${UPSTREAM_VERSION}.sh"
if [[ -f "$MIGRATION_SCRIPT" ]]; then
    log "Running migration: $MIGRATION_SCRIPT"
    dry_or_run "bash '$MIGRATION_SCRIPT'"
fi

# --- Step 9: record successful apply ---
if [[ $DRY_RUN -eq 0 ]]; then
    jq -n \
        --arg from "$CURRENT_VERSION" \
        --arg to "$UPSTREAM_VERSION" \
        --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --arg snapshot "$SNAPSHOT" \
        '{"from": $from, "to": $to, "applied_at": $ts, "snapshot": $snapshot}' \
        > "${STATE_DIR}/last-applied.json"
fi

log "Update complete: v$CURRENT_VERSION → v$UPSTREAM_VERSION"
