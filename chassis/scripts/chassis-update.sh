#!/bin/bash
# chassis-update.sh - apply a chassis version bump after operator consent.
#
# Issue: scrollinondubs/behalfbot#33 (Apple-style chassis auto-updater).
#
# Idempotent script that updates the chassis to upstream main:
#   1. Pre-flight (clean working tree)
#   2. Snapshot pre-update state + effective compose config
#   3. Drain in-flight heartbeats (state file lock)
#   4. Pull upstream (canonical-clone mode: git pull; vendored mode: git subtree pull)
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
# Merged-config verification helpers (behalfbot#100). Ships alongside this
# script; a missing copy means a torn tree, and proceeding without it would
# re-open the silent-override-revert hole, so fail loudly (no `set -e` here).
# shellcheck source=chassis/scripts/_compose-verify.sh
source "${SCRIPT_DIR}/_compose-verify.sh" || {
    echo "[chassis-update] FATAL: ${SCRIPT_DIR}/_compose-verify.sh missing - it ships with this script" >&2
    exit 1
}
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

# --- Step 4.5: snapshot the effective compose config before the pull ---
# Evidence for the operator: what the stack's merged config looked like BEFORE
# this update touched anything. Diffed against the post-up snapshot in step 7.
if [[ -f "${COMPOSE_DIR}/docker-compose.yml" ]]; then
    snapshot_config "$CONFIG_PRE"
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

# --- Step 6: docker compose pull + up (through compose.sh, behalfbot#100) ---
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
MIGRATION_SCRIPT="${SCRIPT_DIR}/chassis-migrations/v${UPSTREAM_VERSION}.sh"
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
