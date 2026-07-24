#!/usr/bin/env bash
# resolve-chassis-root.sh - resolve the effective chassis source root: the
# LIVE (bind-mounted / vendored) tree when one is usable, the image-baked
# tree otherwise.
#
# The bug this fixes (behalfbot: stale-baked-chassis drift)
# =========================================================
# A containerized install carries TWO chassis trees:
#
#   /app/chassis                     - baked into the docker image at build time
#   $CUSTOMER_HOME/chassis/chassis   - the operator's live git tree, when the
#                                      install bind-mounts its chassis clone at
#                                      $CUSTOMER_HOME/chassis (or vendors the
#                                      repo as a subtree at the same path; both
#                                      layouts nest the tree one level down
#                                      because the repo root contains chassis/)
#
# CHASSIS_ROOT was baked as Dockerfile ENV pointing at /app/chassis, so the
# runtime resolved the baked tree unconditionally. `git pull` on the host
# updated the mounted tree, the operator believed the install was updated,
# and every consumer of CHASSIS_ROOT (bootstrap-mcp-config.sh and its
# .mcp.json.template, gather scripts, the dispatcher, first-boot-announce)
# kept executing the stale baked copy. The command ran; nothing changed.
# This is the same defect class as the v0.2.0 plugins no-op that
# resolve-plugin-root.sh exists to prevent, applied to the chassis itself.
#
# Resolution contract
# ===================
# Whole-tree preference, NOT a per-file overlay: unlike plugins (independent
# units that can mix provenance), the chassis is one coherent versioned tree.
# Mixing files across two versions is strictly worse than choosing one tree.
#
#   1. Operator override: a pre-set CHASSIS_ROOT is honoured VERBATIM. The
#      Dockerfile no longer bakes the variable, precisely so that "set"
#      reliably means an operator set it (compose environment, docker -e, or
#      the customer .env). Same lesson as CHASSIS_PLUGINS_ROOT.
#   2. Live tree, when usable: it is the operator's source of truth - the tree
#      `git pull` and chassis-update.sh actually mutate. A live tree OLDER
#      than the baked one still wins (that is what a rollback looks like);
#      it is logged as a WARN, not overridden.
#   3. Baked tree: the fallback that keeps the published image working for
#      installs that do not mount a chassis clone at all.
#
# Guards:
#   - "usable" = VERSION readable + scripts/ + scheduled-tasks/
#     heartbeat-dispatcher.sh present. A torn tree (mid-pull, partial mount)
#     never silently wins; it forces baked AND exits 5 so callers shout.
#   - MAJOR version skew between live and baked refuses the live tree (exit
#     5). Per CHANGELOG semver conventions MAJOR is reserved for chassis
#     architecture changes (docker image base, dispatcher API); running
#     cross-MAJOR code on this image is not safe to assume.
#
# Out-of-band visibility: the resolved root is materialised as a symlink at
# $CUSTOMER_HOME/state/chassis-root so processes that do NOT inherit the
# entrypoint's environment (docker exec sessions, the chassis-update.sh
# healthcheck probing from the host) can read the truth. A state record lands
# at $CUSTOMER_HOME/chassis-root.state.json (adjacent to
# plugins-root.state.json).
#
# Env seams:
#   CHASSIS_BAKED_TREE_ROOT - override the baked-tree location (tests, host
#       layouts). Defaults to /app/chassis.
#   CHASSIS_LIVE_TREE_ROOT  - override the live-tree location. Defaults to
#       $CUSTOMER_HOME/chassis/chassis (matches the mount/vendor layout that
#       fetch-plugins.sh already probes for its VERSION floor).
#
# Output: the resolved root on stdout. All logging goes to stderr.
#
# Exit codes:
#   0 - resolved (explicit | live | baked with no live tree present)
#   5 - ASSERTION FAILED: a live tree exists but is NOT active (torn tree,
#       MAJOR skew, or symlink materialisation failure). The best-available
#       root is still printed and the state file records the error. Callers
#       must surface this loudly; it exists so "the operator updated but the
#       runtime did not" can never pass quietly again.

set -uo pipefail

: "${CUSTOMER_HOME:=${CHASSIS_HOME:-/app/customer}}"

log() { printf '[resolve-chassis-root] %s\n' "$*" >&2; }

STATE_FILE="$CUSTOMER_HOME/chassis-root.state.json"
LINK_PATH="$CUSTOMER_HOME/state/chassis-root"

usable() {
    local dir="$1"
    [[ -d "$dir" ]] || return 1
    [[ -s "$dir/VERSION" ]] || return 1
    [[ -d "$dir/scripts" ]] || return 1
    [[ -f "$dir/scheduled-tasks/heartbeat-dispatcher.sh" ]] || return 1
}

read_version() {
    tr -d '[:space:]' < "$1/VERSION" 2>/dev/null || true
}

major_of() {
    printf '%s' "${1%%.*}"
}

# MODE is one of: explicit | live | baked
write_state() {
    local mode="$1" root="$2" error="${3:-}"
    MODE="$mode" ROOT="$root" ERROR="$error" \
    BAKED="${BAKED_ROOT:-}" LIVE="${LIVE_ROOT:-}" \
    BAKED_VERSION="${BAKED_VERSION:-}" LIVE_VERSION="${LIVE_VERSION:-}" \
    python3 - "$STATE_FILE" <<'PY' 2>/dev/null || log "WARN: could not write $STATE_FILE"
import datetime, json, os, sys
state = {
    "schema": 1,
    "mode": os.environ["MODE"],
    "resolved_root": os.environ["ROOT"],
    "baked_root": os.environ["BAKED"] or None,
    "live_root": os.environ["LIVE"] or None,
    "baked_version": os.environ["BAKED_VERSION"] or None,
    "live_version": os.environ["LIVE_VERSION"] or None,
    "resolved_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "error": os.environ["ERROR"] or None,
}
with open(sys.argv[1], "w") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
PY
}

# Point the out-of-band symlink at the resolved root. Failure is recorded but
# does not change the resolution - the printed root is the contract; the
# symlink only serves probes that cannot inherit the environment.
materialise_link() {
    local target="$1"
    mkdir -p "$CUSTOMER_HOME/state" 2>/dev/null || return 1
    ln -sfn "$target" "$LINK_PATH" 2>/dev/null || return 1
    return 0
}

# --- operator override: set means set, honour verbatim ----------------------
if [[ -n "${CHASSIS_ROOT:-}" ]]; then
    LIVE_ROOT="${CHASSIS_LIVE_TREE_ROOT:-$CUSTOMER_HOME/chassis/chassis}"
    if usable "$LIVE_ROOT" && [[ "$CHASSIS_ROOT" != "$LIVE_ROOT" ]]; then
        log "operator override: CHASSIS_ROOT=$CHASSIS_ROOT (explicitly set)"
        log "note: a usable live chassis tree exists at $LIVE_ROOT and is being ignored BY EXPLICIT CHOICE"
    fi
    LIVE_ROOT="" BAKED_ROOT=""
    materialise_link "$CHASSIS_ROOT" || log "WARN: could not materialise $LINK_PATH"
    write_state explicit "$CHASSIS_ROOT"
    printf '%s\n' "$CHASSIS_ROOT"
    exit 0
fi

# --- locate the two trees ----------------------------------------------------
BAKED_ROOT=""
if [[ -n "${CHASSIS_BAKED_TREE_ROOT:-}" && -d "${CHASSIS_BAKED_TREE_ROOT}" ]]; then
    BAKED_ROOT="$CHASSIS_BAKED_TREE_ROOT"
elif [[ -d /app/chassis ]]; then
    BAKED_ROOT="/app/chassis"
fi
LIVE_ROOT="${CHASSIS_LIVE_TREE_ROOT:-$CUSTOMER_HOME/chassis/chassis}"

BAKED_VERSION=""
[[ -n "$BAKED_ROOT" ]] && BAKED_VERSION="$(read_version "$BAKED_ROOT")"

# --- no live tree at all: baked, exactly the pre-fix behaviour ---------------
if [[ ! -d "$LIVE_ROOT" ]]; then
    LIVE_ROOT=""
    materialise_link "$BAKED_ROOT" || log "WARN: could not materialise $LINK_PATH"
    write_state baked "$BAKED_ROOT"
    printf '%s\n' "$BAKED_ROOT"
    exit 0
fi

# --- live tree present but torn: baked + LOUD --------------------------------
if ! usable "$LIVE_ROOT"; then
    LIVE_VERSION="$(read_version "$LIVE_ROOT")"
    err="live chassis tree at $LIVE_ROOT exists but is not usable (missing VERSION, scripts/, or scheduled-tasks/heartbeat-dispatcher.sh) - running BAKED v${BAKED_VERSION:-unknown} instead"
    log "ERROR: CHASSIS ROOT ASSERTION FAILED - $err"
    materialise_link "$BAKED_ROOT" || log "WARN: could not materialise $LINK_PATH"
    write_state baked "$BAKED_ROOT" "$err"
    printf '%s\n' "$BAKED_ROOT"
    exit 5
fi

LIVE_VERSION="$(read_version "$LIVE_ROOT")"

# --- MAJOR skew: the image cannot be assumed to run cross-MAJOR code ---------
if [[ -n "$BAKED_VERSION" && "$(major_of "$LIVE_VERSION")" != "$(major_of "$BAKED_VERSION")" ]]; then
    err="live chassis v$LIVE_VERSION has a different MAJOR than baked v$BAKED_VERSION - MAJOR bumps change the image contract; refresh the image (compose pull + up -d) instead of running live code on this base. Running BAKED."
    log "ERROR: CHASSIS ROOT ASSERTION FAILED - $err"
    materialise_link "$BAKED_ROOT" || log "WARN: could not materialise $LINK_PATH"
    write_state baked "$BAKED_ROOT" "$err"
    printf '%s\n' "$BAKED_ROOT"
    exit 5
fi

# --- live tree wins ----------------------------------------------------------
if [[ -n "$BAKED_VERSION" && "$LIVE_VERSION" != "$BAKED_VERSION" ]]; then
    if [[ "$(printf '%s\n%s\n' "$LIVE_VERSION" "$BAKED_VERSION" | sort -V | head -1)" == "$LIVE_VERSION" ]]; then
        log "WARN: live chassis v$LIVE_VERSION is OLDER than baked v$BAKED_VERSION (rollback in effect?) - live tree still wins"
    else
        log "live chassis v$LIVE_VERSION supersedes baked v$BAKED_VERSION"
    fi
fi

link_err=""
if ! materialise_link "$LIVE_ROOT"; then
    link_err="resolved live tree $LIVE_ROOT but could not materialise $LINK_PATH - out-of-band probes (chassis-update healthcheck, docker exec sessions) will not see it"
    log "ERROR: CHASSIS ROOT ASSERTION FAILED - $link_err"
fi
write_state live "$LIVE_ROOT" "$link_err"
printf '%s\n' "$LIVE_ROOT"
[[ -z "$link_err" ]] || exit 5
exit 0
