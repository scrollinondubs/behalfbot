#!/bin/bash
# v0.3.0.sh - re-assert the per-install compose override after the update.
#
# Why (behalfbot#100): every chassis-update.sh BEFORE the fix in this tree
# brought the stack back with bare `docker compose pull` + `docker compose
# up -d`, silently dropping the per-install override
# ($CUSTOMER_HOME/chassis-compose.override.yml - image pins, published ports,
# env_file, scaled-to-0 services) and then reporting success.
#
# An install upgrading TO v0.3.0 runs its OLD copy of the updater: the update
# pulls the new tree to disk, but the process executing is still the pre-fix
# script, so its `up -d` is the bare, override-stripping one. This migration
# is the repair channel for exactly that window: the old updater's step 8
# runs `chassis-migrations/v0.3.0.sh` FROM THE FRESHLY PULLED TREE, after its
# bare `up -d` and healthcheck. We re-run the stack through compose.sh (which
# layers the override) and then verify the result against the merged config.
#
# On the install that motivated this, the bare `up -d` unpublished postgres's
# 127.0.0.1:5432 (breaking every host-side consumer and triggering a watchdog
# VM bounce) and created a fresh empty Vaultwarden that the override scales to
# 0. `compose.sh up -d` reconciles both: compose recreates services whose
# config drifted and scales replicas-0 services down.
#
# No-override installs: exit 0 without touching anything - bare compose was
# already the correct invocation for them. Host-mode installs (no docker or
# no compose file): nothing was containerized, exit 0.
#
# Idempotent: re-running against an already-correct stack is a no-op `up -d`
# plus a passing verification.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_SH="${SCRIPT_DIR}/../compose.sh"
VERIFY_LIB="${SCRIPT_DIR}/../_compose-verify.sh"

log() { printf '[migrate-v0.3.0] %s\n' "$*"; }

# CUSTOMER_HOME resolution, explicit rather than via _env.sh: the pre-fix
# updater keeps CUSTOMER_HOME as a shell variable and does not export it to
# migrations, and _env.sh's legacy fallback (CUSTOMER_HOME := CHASSIS_HOME)
# points at the chassis CLONE on new-layout installs - which has no override,
# so this repair would silently no-op on exactly the installs it exists for.
# Prefer whichever layout actually holds an override file.
if [[ -z "${CUSTOMER_HOME:-}" ]]; then
    if [[ -f "$HOME/.behalfbot/chassis-compose.override.yml" ]]; then
        CUSTOMER_HOME="$HOME/.behalfbot"
    elif [[ -n "${CHASSIS_HOME:-}" && -f "$CHASSIS_HOME/chassis-compose.override.yml" ]]; then
        # Legacy co-located install: customer state lives in the chassis tree.
        CUSTOMER_HOME="$CHASSIS_HOME"
    elif [[ -d "$HOME/.behalfbot" ]]; then
        CUSTOMER_HOME="$HOME/.behalfbot"
    fi
fi

if [[ -z "${CUSTOMER_HOME:-}" ]]; then
    log "CUSTOMER_HOME could not be resolved; nothing to repair"
    exit 0
fi

# Mirror compose.sh's override semantics: ${VAR-} (no colon) so an explicitly
# empty CHASSIS_COMPOSE_OVERRIDE (deliberate no-override, chassis dev /
# smoke-test) is distinguishable from unset (use the default path).
OVERRIDE_FILE="${CHASSIS_COMPOSE_OVERRIDE-${CUSTOMER_HOME}/chassis-compose.override.yml}"
if [[ -z "$OVERRIDE_FILE" ]]; then
    log "CHASSIS_COMPOSE_OVERRIDE explicitly empty - no override to re-assert"
    exit 0
fi
if [[ ! -f "$OVERRIDE_FILE" ]]; then
    log "no compose override at $OVERRIDE_FILE - default install, bare compose was correct, nothing to repair"
    exit 0
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    log "docker compose not available - host-mode install, nothing to repair"
    exit 0
fi

if [[ ! -f "$COMPOSE_SH" ]]; then
    log "FATAL: $COMPOSE_SH missing from the pulled tree - cannot re-assert the override"
    log "FATAL: manual recovery: bash <chassis-repo>/chassis/scripts/compose.sh up -d"
    exit 1
fi

export CUSTOMER_HOME
log "re-running the stack through compose.sh so $OVERRIDE_FILE applies"
if ! bash "$COMPOSE_SH" up -d; then
    log "FATAL: compose.sh up -d failed - the stack may still be running WITHOUT the override"
    log "FATAL: fix the error above, then run: bash $COMPOSE_SH up -d"
    exit 1
fi

# Verify, don't assume: the whole point of #100 is that "the command ran" is
# not evidence. Check the engine against the merged config.
# shellcheck source=chassis/scripts/_compose-verify.sh
source "$VERIFY_LIB" || { log "FATAL: $VERIFY_LIB missing from the pulled tree"; exit 1; }

CONFIG_JSON="$(mktemp)"
trap 'rm -f "$CONFIG_JSON"' EXIT
if ! bash "$COMPOSE_SH" config --format json > "$CONFIG_JSON" || [[ ! -s "$CONFIG_JSON" ]]; then
    log "FATAL: could not render the merged compose config for verification"
    exit 1
fi

if verify_out=$(compose_verify_running_config "$CONFIG_JSON"); then
    log "override re-asserted and verified: declared ports published, scaled-to-0 services down"
    exit 0
fi

while IFS= read -r line; do log "$line"; done <<<"$verify_out"
log "FATAL: running stack still does not match the merged compose config after compose.sh up -d"
log "FATAL: manual recovery: bash $COMPOSE_SH up -d && docker ps"
exit 1
