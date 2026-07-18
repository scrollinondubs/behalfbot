#!/usr/bin/env bash
# Behalf.bot chassis container entrypoint
# =======================================
# Modes (passed as CMD or `docker compose run`):
#   dispatcher       - long-running gather-first heartbeat loop (default CMD)
#   bootstrap        - one-shot: hydrate .mcp.json/CLAUDE.md/HEARTBEATS.md, seed memory
#   install-plugin <name>
#                    - one-shot: run plugins/<name>/install.sh
#   update-plugins   - one-shot: fetch behalfbot-plugins at the pinned tag+SHA
#                      into $CUSTOMER_HOME/vendored-plugins (behalfbot#53);
#                      supports --freeze / --unfreeze for air-gapped installs
#   hydrate-env      - one-shot: pull secrets from Vaultwarden via rbw, write to .env
#   smoke-test       - one-shot: run chassis + plugin smoke checks
#   claude           - interactive Claude CLI (needs -it)
#   shell            - interactive zsh (needs -it)
#
# The dispatcher loop reads HEARTBEATS.md from /app/customer (bind-mounted)
# and invokes /app/chassis/scheduled-tasks/heartbeat-dispatcher.sh on a fixed
# tick. /tmp/dispatcher.alive is touched after each tick so the container
# healthcheck stays green.

set -euo pipefail

: "${CHASSIS_ROOT:=/app/chassis}"
# Issue #6 customer-state split. CUSTOMER_HOME is the canonical name for the
# customer-state mount inside the container; CHASSIS_HOME is kept as an alias
# pointing at the SAME path so legacy chassis scripts (which read
# $CHASSIS_HOME/.env, $CHASSIS_HOME/briefings, etc.) keep working untouched.
: "${CUSTOMER_HOME:=/app/customer}"
: "${CHASSIS_HOME:=$CUSTOMER_HOME}"

# Runtime-pull plugin roots (behalfbot#53 Phase 0):
#   PLUGINS_FETCH_ROOT - fetched from behalfbot-plugins at the pinned tag+SHA.
#     Deliberately NOT $CUSTOMER_HOME/plugins: that dir holds live
#     customer-local plugins and must never be clobbered by a fetch.
#     vendored-plugins/ is on the customer bind mount - writable at runtime,
#     survives container recreate.
#   PLUGINS_LOCAL_ROOT - customer-private plugins layered on top of fetched
#     ones (never published to the public plugins repo).
#   PLUGINS_BAKED_ROOT - image-baked offline fallback during migration.
: "${PLUGINS_FETCH_ROOT:=$CUSTOMER_HOME/vendored-plugins}"
: "${PLUGINS_LOCAL_ROOT:=$CUSTOMER_HOME/plugins-local}"
: "${PLUGINS_BAKED_ROOT:=/app/plugins}"

# CHASSIS_PLUGINS_ROOT resolution: an explicit env value always wins; else the
# fetched tree wins when a lockfile validates it; else the baked fallback.
# Re-runnable (called again after update-plugins fetches a new tree).
_PLUGINS_ROOT_EXPLICIT="${CHASSIS_PLUGINS_ROOT:-}"
resolve_plugins_root() {
    if [[ -n "$_PLUGINS_ROOT_EXPLICIT" ]]; then
        CHASSIS_PLUGINS_ROOT="$_PLUGINS_ROOT_EXPLICIT"
    elif [[ -f "$CUSTOMER_HOME/plugins.lock" && -d "$PLUGINS_FETCH_ROOT" ]]; then
        CHASSIS_PLUGINS_ROOT="$PLUGINS_FETCH_ROOT"
    else
        CHASSIS_PLUGINS_ROOT="$PLUGINS_BAKED_ROOT"
    fi
    export CHASSIS_PLUGINS_ROOT
}
resolve_plugins_root
export CUSTOMER_HOME CHASSIS_HOME CHASSIS_ROOT CHASSIS_PLUGINS_ROOT \
       PLUGINS_FETCH_ROOT PLUGINS_LOCAL_ROOT PLUGINS_BAKED_ROOT
: "${DISPATCHER_INTERVAL_SECONDS:=900}"
: "${DISPATCHER_SCRIPT:=$CHASSIS_ROOT/scheduled-tasks/heartbeat-dispatcher.sh}"

MODE="${1:-dispatcher}"
shift || true

log() {
    printf '[entrypoint %(%H:%M:%S)T] %s\n' -1 "$*"
}

ensure_customer_layout() {
    # Bind-mount may be empty on first run. Don't clobber existing state.
    # Use CUSTOMER_HOME (which equals CHASSIS_HOME in the container) so the
    # naming matches the new issue #6 semantics.
    # plugins-local/ is the customer-private plugin layer (behalfbot#53);
    # vendored-plugins/ is created by fetch-plugins.sh on first fetch.
    mkdir -p "$CUSTOMER_HOME"/{briefings,logs/scheduled,scheduled-tasks,state,data,memory,plugins,plugins-local,temp,scripts}
}

fetch_plugins() {
    # Runtime-pull fetch (behalfbot#53 Phase 0). Best-effort at boot: a fetch
    # problem must never take the bot down - fetch-plugins.sh degrades to the
    # previous fetched tree or the baked fallback, and we resolve the root
    # again afterwards. Prefer the clone-overlay copy (freshest, ships without
    # an image release), fall back to the image-baked copy.
    local fetcher=""
    local cand
    for cand in \
        "$CUSTOMER_HOME/chassis/chassis/scripts/fetch-plugins.sh" \
        "$CHASSIS_ROOT/scripts/fetch-plugins.sh"; do
        [[ -f "$cand" ]] && { fetcher="$cand"; break; }
    done
    if [[ -z "$fetcher" ]]; then
        log "fetch-plugins.sh not found (stale clone + old image) - baked plugins stay active"
        return 0
    fi
    if ! CUSTOMER_HOME="$CUSTOMER_HOME" CHASSIS_ROOT="$CHASSIS_ROOT" \
         PLUGINS_FETCH_ROOT="$PLUGINS_FETCH_ROOT" bash "$fetcher" "$@"; then
        log "WARN: fetch-plugins.sh exited nonzero - continuing on previous/baked plugin tree"
    fi
    resolve_plugins_root
    log "plugin root: $CHASSIS_PLUGINS_ROOT"
}

source_env() {
    # Prefer .env.baked when present. Host-side `scripts/bake-env.sh` expands
    # the Vaultwarden hydration block from .env into literal KEY=VALUE pairs
    # in .env.baked at install/restart time. Inside the container the
    # hydration block in .env silently fails (no Keychain, no bw-unlock auth
    # path), so every VW-backed secret (DISCORD_BOT_TOKEN, OURA_TOKEN,
    # STRAVA_*, etc.) ends up empty if we source the raw .env directly.
    # .env.baked has the literals; reads cleanly from any process.
    if [[ -f "$CHASSIS_HOME/.env.baked" ]]; then
        # shellcheck disable=SC1091
        set -a; . "$CHASSIS_HOME/.env.baked"; set +a
    elif [[ -f "$CHASSIS_HOME/.env" ]]; then
        # Fall back for installs in early bootstrap (before first bake), or
        # for installs that keep a literal-only .env with no hydration block.
        # shellcheck disable=SC1091
        set -a; . "$CHASSIS_HOME/.env"; set +a
    fi
    # CRITICAL: unset ANTHROPIC_API_KEY so `claude -p` uses OAuth subscription
    # billing, not PAYG. Matches the rationale in
    # chassis/scheduled-tasks/heartbeat-dispatcher.sh lines 67-80.
    unset ANTHROPIC_API_KEY || true
}

run_dispatcher_once() {
    if [[ ! -x "$DISPATCHER_SCRIPT" ]]; then
        log "FATAL: dispatcher not found at $DISPATCHER_SCRIPT"
        exit 2
    fi
    if ! CHASSIS_HOME="$CHASSIS_HOME" /usr/bin/zsh "$DISPATCHER_SCRIPT"; then
        log "dispatcher tick failed (continuing)"
    fi
    touch /tmp/dispatcher.alive
}

cmd_dispatcher() {
    ensure_customer_layout
    source_env
    fetch_plugins
    log "dispatcher loop starting - tick=${DISPATCHER_INTERVAL_SECONDS}s, CHASSIS_HOME=$CHASSIS_HOME"
    # Touch sentinel up-front so healthcheck doesn't fail before first tick.
    touch /tmp/dispatcher.alive
    # Emit bot user ID + OAuth invite URL on first boot (issue #53 item 4).
    # No-ops on subsequent boots once the sentinel exists.
    bash "$CHASSIS_ROOT/scripts/first-boot-announce.sh" || \
        log "WARN: first-boot-announce.sh exited non-zero (non-fatal)"
    while true; do
        run_dispatcher_once
        sleep "$DISPATCHER_INTERVAL_SECONDS"
    done
}

cmd_bootstrap() {
    ensure_customer_layout
    source_env
    fetch_plugins
    log "running bootstrap.sh against CHASSIS_HOME=$CHASSIS_HOME"
    CHASSIS_HOME="$CHASSIS_HOME" bash /app/bootstrap.sh "$@"
}

cmd_update_plugins() {
    # Explicit fetch/refresh: propagates fetch-plugins.sh's exit code so a
    # SECURITY refusal (moved tag, exit 3) or corrupt tree (exit 4) is
    # visible to the operator, unlike the best-effort boot-time fetch.
    ensure_customer_layout
    source_env
    local fetcher=""
    local cand
    for cand in \
        "$CUSTOMER_HOME/chassis/chassis/scripts/fetch-plugins.sh" \
        "$CHASSIS_ROOT/scripts/fetch-plugins.sh"; do
        [[ -f "$cand" ]] && { fetcher="$cand"; break; }
    done
    if [[ -z "$fetcher" ]]; then
        log "FATAL: fetch-plugins.sh not found in clone or image"
        exit 2
    fi
    CUSTOMER_HOME="$CUSTOMER_HOME" CHASSIS_ROOT="$CHASSIS_ROOT" \
        PLUGINS_FETCH_ROOT="$PLUGINS_FETCH_ROOT" bash "$fetcher" "$@"
    resolve_plugins_root
    log "plugin root: $CHASSIS_PLUGINS_ROOT"
}

cmd_install_plugin() {
    local name="${1:?install-plugin requires a plugin name}"
    ensure_customer_layout
    source_env
    local installer="$CHASSIS_PLUGINS_ROOT/$name/install.sh"
    if [[ ! -x "$installer" ]]; then
        log "FATAL: plugin installer not found at $installer"
        exit 2
    fi
    log "installing plugin: $name"
    CHASSIS_HOME="$CHASSIS_HOME" bash "$installer"
    local validator="$CHASSIS_PLUGINS_ROOT/$name/validate.sh"
    if [[ -x "$validator" ]]; then
        log "validating plugin: $name"
        CHASSIS_HOME="$CHASSIS_HOME" bash "$validator"
    fi
}

cmd_hydrate_env() {
    ensure_customer_layout
    if ! command -v rbw >/dev/null 2>&1; then
        log "FATAL: rbw not installed in image"
        exit 2
    fi
    # rbw config + unlock relies on env passed in. Caller responsibility:
    #   docker compose run -e RBW_EMAIL=... -e RBW_URL=... -e RBW_PINENTRY=... chassis hydrate-env
    # See docs/containerization.md § Vaultwarden hydration for the full flow.
    log "running chassis/scripts/hydrate-env-from-vw.sh"
    CHASSIS_HOME="$CHASSIS_HOME" bash "$CHASSIS_ROOT/scripts/hydrate-env-from-vw.sh" "$@"
}

cmd_smoke_test() {
    ensure_customer_layout
    source_env
    log "running chassis smoke tests"
    CHASSIS_HOME="$CHASSIS_HOME" bash "$CHASSIS_ROOT/scripts/smoke-test.sh" "$@"
}

cmd_claude() {
    ensure_customer_layout
    source_env
    exec claude "$@"
}

cmd_shell() {
    ensure_customer_layout
    source_env
    exec /usr/bin/zsh
}

case "$MODE" in
    dispatcher)       cmd_dispatcher       "$@" ;;
    bootstrap)        cmd_bootstrap        "$@" ;;
    install-plugin)   cmd_install_plugin   "$@" ;;
    update-plugins)   cmd_update_plugins   "$@" ;;
    hydrate-env)      cmd_hydrate_env      "$@" ;;
    smoke-test)       cmd_smoke_test       "$@" ;;
    claude)           cmd_claude           "$@" ;;
    shell)            cmd_shell            "$@" ;;
    *)
        echo "unknown mode: $MODE" >&2
        echo "valid modes: dispatcher | bootstrap | install-plugin <name> | update-plugins [--freeze|--unfreeze] | hydrate-env | smoke-test | claude | shell" >&2
        exit 2
        ;;
esac
