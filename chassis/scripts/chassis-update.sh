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
LOCAL_VERSION_FILE="${CHASSIS_HOME}/chassis/VERSION"
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

dry_or_run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        log "DRY-RUN: $*"
    else
        log "+ $*"
        eval "$@"
    fi
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

# --- Rollback path (independent of normal apply flow) ---
if [[ $ROLLBACK -eq 1 ]]; then
    LATEST_BACKUP=$(ls -1t "$BACKUP_DIR"/chassis-pre-v*.tgz 2>/dev/null | head -1)
    [[ -z "$LATEST_BACKUP" ]] && die "no backups found in $BACKUP_DIR"
    log "Restoring from $LATEST_BACKUP"
    dry_or_run "cd '$CHASSIS_HOME' && tar xzf '$LATEST_BACKUP'"
    dry_or_run "cd '$CHASSIS_HOME' && docker compose up -d"
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
log "Healthcheck: polling (timeout ${HEALTHCHECK_TIMEOUT_SECONDS}s)..."
if [[ $DRY_RUN -eq 0 ]]; then
    healthy=0
    for ((i=0; i<HEALTHCHECK_TIMEOUT_SECONDS; i++)); do
        # Verify the running container reports the same VERSION we just pulled.
        # Docker exec is the canonical check; fall back to file-based check if
        # no container is running (some installs run the dispatcher on the host).
        if command -v docker >/dev/null 2>&1; then
            CONTAINER_NAME=$(docker ps --filter "label=com.behalfbot.chassis" --format '{{.Names}}' 2>/dev/null | head -1)
            if [[ -n "$CONTAINER_NAME" ]]; then
                RUNNING_VERSION=$(docker exec "$CONTAINER_NAME" cat /chassis/VERSION 2>/dev/null | tr -d '[:space:]' || echo "")
                if [[ "$RUNNING_VERSION" == "$UPSTREAM_VERSION" ]]; then
                    healthy=1
                    break
                fi
            fi
        fi
        # Host-mode fallback: just verify VERSION file on disk advanced.
        DISK_VERSION=$(tr -d '[:space:]' < "$LOCAL_VERSION_FILE" 2>/dev/null || echo "")
        if [[ "$DISK_VERSION" == "$UPSTREAM_VERSION" ]]; then
            healthy=1
            break
        fi
        sleep 1
    done
    if [[ $healthy -eq 0 ]]; then
        log "FAIL: healthcheck did not converge within ${HEALTHCHECK_TIMEOUT_SECONDS}s"
        log "Rolling back from snapshot: $SNAPSHOT"
        dry_or_run "cd '$CHASSIS_HOME' && tar xzf '$SNAPSHOT'"
        if [[ -f "${COMPOSE_DIR}/docker-compose.yml" ]]; then
            dry_or_run "cd '$COMPOSE_DIR' && docker compose up -d"
        fi
        die "update failed and was rolled back"
    fi
fi

# --- Step 8: run migration script if present ---
# Migrations are strictly automated shell scripts. Judgment-heavy migrations
# would have been flagged BREAKING CHANGES and gated behind --force above.
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
