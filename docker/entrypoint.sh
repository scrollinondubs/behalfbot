#!/usr/bin/env bash
# Behalf.bot chassis container entrypoint
# =======================================
# Modes (passed as CMD or `docker compose run`):
#   dispatcher       - long-running gather-first heartbeat loop (default CMD)
#   bootstrap        - one-shot: hydrate .mcp.json/CLAUDE.md/HEARTBEATS.md, seed memory
#   install-plugin <name>
#                    - one-shot: run plugins/<name>/install.sh
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
: "${CHASSIS_PLUGINS_ROOT:=/app/plugins}"
: "${CHASSIS_HOME:=/app/customer}"
: "${DISPATCHER_INTERVAL_SECONDS:=900}"
: "${DISPATCHER_SCRIPT:=$CHASSIS_ROOT/scheduled-tasks/heartbeat-dispatcher.sh}"

MODE="${1:-dispatcher}"
shift || true

log() {
    printf '[entrypoint %(%H:%M:%S)T] %s\n' -1 "$*"
}

ensure_customer_layout() {
    # Bind-mount may be empty on first run. Don't clobber existing state.
    mkdir -p "$CHASSIS_HOME"/{briefings,logs/scheduled,scheduled-tasks,state,data,memory,plugins,temp}
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
    log "running bootstrap.sh against CHASSIS_HOME=$CHASSIS_HOME"
    CHASSIS_HOME="$CHASSIS_HOME" bash /app/bootstrap.sh "$@"
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
    hydrate-env)      cmd_hydrate_env      "$@" ;;
    smoke-test)       cmd_smoke_test       "$@" ;;
    claude)           cmd_claude           "$@" ;;
    shell)            cmd_shell            "$@" ;;
    *)
        echo "unknown mode: $MODE" >&2
        echo "valid modes: dispatcher | bootstrap | install-plugin <name> | hydrate-env | smoke-test | claude | shell" >&2
        exit 2
        ;;
esac
