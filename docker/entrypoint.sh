#!/usr/bin/env bash
# Behalf.bot chassis container entrypoint
# =======================================
# Modes (passed as CMD or `docker compose run`):
#   dispatcher       - long-running gather-first heartbeat loop (default CMD)
#   bootstrap        - one-shot: hydrate .mcp.json/CLAUDE.md/HEARTBEATS.md, seed memory
#   install-plugin <name>
#                    - one-shot: run plugins/<name>/install.sh
#   hydrate-env      - one-shot: pull secrets from Vaultwarden via rbw, write to .env
#   migrate          - one-shot: apply chassis/db/migrations/*.sql
#   smoke-test       - one-shot: run chassis + plugin smoke checks
#   claude           - interactive Claude CLI (needs -it)
#   shell            - interactive zsh (needs -it)
#
# The dispatcher loop reads HEARTBEATS.md from /app/customer (bind-mounted)
# and invokes /app/chassis/scheduled-tasks/heartbeat-dispatcher.sh on a fixed
# tick. /tmp/dispatcher.alive is touched after each tick so the container
# healthcheck stays green.

set -euo pipefail

# CHASSIS_ROOT and CHASSIS_PLUGINS_ROOT are deliberately NOT defaulted here
# (or in the Dockerfile ENV). resolve_chassis_root() / resolve_plugin_root()
# set them after source_env, so a value that is already present reliably
# means an operator set it (compose environment, docker -e, or the customer
# .env). Defaulting either up here is exactly what made the v0.2.0
# fetched-plugin-tree preference in _env.sh unreachable - and, for
# CHASSIS_ROOT, what kept every install running the stale image-baked
# chassis tree while the operator's mounted clone sat updated and ignored.
# Issue #6 customer-state split. CUSTOMER_HOME is the canonical name for the
# customer-state mount inside the container; CHASSIS_HOME is kept as an alias
# pointing at the SAME path so legacy chassis scripts (which read
# $CHASSIS_HOME/.env, $CHASSIS_HOME/briefings, etc.) keep working untouched.
: "${CUSTOMER_HOME:=/app/customer}"
: "${CHASSIS_HOME:=$CUSTOMER_HOME}"
export CUSTOMER_HOME CHASSIS_HOME
: "${DISPATCHER_INTERVAL_SECONDS:=900}"

MODE="${1:-dispatcher}"
shift || true

log() {
    printf '[entrypoint %(%H:%M:%S)T] %s\n' -1 "$*"
}

ensure_customer_layout() {
    # Bind-mount may be empty on first run. Don't clobber existing state.
    # Use CUSTOMER_HOME (which equals CHASSIS_HOME in the container) so the
    # naming matches the new issue #6 semantics.
    mkdir -p "$CUSTOMER_HOME"/{briefings,logs/scheduled,scheduled-tasks,state,data,memory,plugins,temp,scripts}
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

resolve_chassis_root() {
    # Stale-baked-chassis fix: prefer the operator's live (mounted / vendored)
    # chassis tree over the image-baked copy, so `git pull` on the host is
    # actually in effect on the next boot instead of silently ignored. Runs
    # AFTER source_env so a CHASSIS_ROOT from the customer .env or the compose
    # environment counts as an operator override (the resolver honours it
    # verbatim). Must run BEFORE anything dereferences $CHASSIS_ROOT -
    # run_plugin_fetch, resolve_plugin_root, migrations, the dispatcher.
    #
    # Bootstrapping rule: run the LIVE tree's copy of the resolver when one is
    # present - it is newer code, and preferring it is the same semantic the
    # resolver itself implements. The baked copy is the fallback.
    local live_candidate="${CHASSIS_LIVE_TREE_ROOT:-$CUSTOMER_HOME/chassis/chassis}"
    local resolver="$live_candidate/scripts/resolve-chassis-root.sh"
    [[ -f "$resolver" ]] || resolver="/app/chassis/scripts/resolve-chassis-root.sh"
    if [[ ! -f "$resolver" ]]; then
        export CHASSIS_ROOT="${CHASSIS_ROOT:-/app/chassis}"
        log "WARN: chassis-root resolver missing - using $CHASSIS_ROOT"
    else
        local resolved rc=0
        resolved="$(bash "$resolver")" || rc=$?
        if [[ -n "$resolved" ]]; then
            export CHASSIS_ROOT="$resolved"
        else
            export CHASSIS_ROOT="${CHASSIS_ROOT:-/app/chassis}"
        fi
        if [[ "$rc" -ne 0 ]]; then
            log "ERROR: CHASSIS ROOT ASSERTION FAILED (rc=$rc) - a live chassis tree exists but is NOT (fully) active."
            log "ERROR: running on $CHASSIS_ROOT - see $CUSTOMER_HOME/chassis-root.state.json for the resolution record."
        else
            log "chassis root: $CHASSIS_ROOT ($(tr -d '[:space:]' < "$CHASSIS_ROOT/VERSION" 2>/dev/null || echo 'VERSION unreadable'))"
        fi
    fi
    : "${DISPATCHER_SCRIPT:=$CHASSIS_ROOT/scheduled-tasks/heartbeat-dispatcher.sh}"
    export DISPATCHER_SCRIPT
}

resolve_plugin_root() {
    # Overlay resolution (behalfbot#82 fix): a plugin present in the fetched
    # vendored-plugins tree wins by name; anything only in the baked tree
    # still loads. Runs AFTER source_env so a CHASSIS_PLUGINS_ROOT from the
    # customer .env or the compose environment counts as an operator override
    # (the resolver honours it verbatim). Runs AFTER run_plugin_fetch in the
    # boot modes so a fresh fetch is active on the same boot.
    local resolver="$CHASSIS_ROOT/scripts/resolve-plugin-root.sh"
    if [[ ! -x "$resolver" ]]; then
        export CHASSIS_PLUGINS_ROOT="${CHASSIS_PLUGINS_ROOT:-/app/plugins}"
        log "WARN: plugin-root resolver missing at $resolver - using $CHASSIS_PLUGINS_ROOT"
        return 0
    fi
    local resolved rc=0
    resolved="$(bash "$resolver")" || rc=$?
    if [[ -n "$resolved" ]]; then
        export CHASSIS_PLUGINS_ROOT="$resolved"
    else
        export CHASSIS_PLUGINS_ROOT="${CHASSIS_PLUGINS_ROOT:-/app/plugins}"
    fi
    if [[ "$rc" -ne 0 ]]; then
        log "ERROR: PLUGIN ROOT ASSERTION FAILED (rc=$rc) - a usable fetched plugin tree exists but is NOT active."
        log "ERROR: running on $CHASSIS_PLUGINS_ROOT - see $CUSTOMER_HOME/plugins-root.state.json for the resolution record."
    else
        log "plugin root: $CHASSIS_PLUGINS_ROOT"
    fi
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

run_plugin_fetch() {
    # behalfbot#82. Pull the vendored plugin tree from scrollinondubs/behalfbot-plugins
    # at the tag+SHA recorded in PLUGINS_PIN, into $CUSTOMER_HOME/vendored-plugins.
    #
    # Non-fatal by design: a fetch problem must never take the bot down. On any
    # failure the previous tree (or the image-baked /app/plugins) stays active.
    #
    # Exit 3 is the exception worth shouting about - it means the pinned tag no
    # longer resolves to the pinned SHA, i.e. a tag was force-moved. That is a
    # supply-chain signal, not a transient error, so it gets its own log line.
    local fetcher="$CHASSIS_ROOT/scripts/fetch-plugins.sh"
    if [[ ! -x "$fetcher" ]]; then
        log "WARN: plugin fetcher missing at $fetcher - running on the baked plugin tree"
        return 0
    fi
    local rc=0
    "$fetcher" || rc=$?
    case "$rc" in
        0) : ;;
        3) log "SECURITY: plugin tag/SHA mismatch - refused to fetch, previous tree kept. Investigate before trusting the plugin set." ;;
        4) log "WARN: plugin fetch produced a corrupt tree - previous tree kept" ;;
        *) log "WARN: plugin fetch failed (rc=$rc) - continuing on the previous/baked tree" ;;
    esac
    return 0
}

run_chassis_migrations() {
    # Idempotent and advisory-locked, so running it on every boot is safe and
    # two containers starting together cannot race. Non-fatal on failure: an
    # install with no Postgres still gets a dispatcher, it just gets a Pacman
    # queue that fails loudly when touched (which is the intended behaviour -
    # see chassis/db/connection.py).
    #
    # cd to the RESOLVED tree's parent (not a hardcoded /app) so `python3 -m
    # chassis.db.migrate` imports the chassis package that is actually active.
    (cd "$(dirname "$CHASSIS_ROOT")" && python3 -m chassis.db.migrate) || \
        log "WARN: chassis migrations did not apply - Postgres-backed features will fail loudly until they do"
}

cmd_migrate() {
    ensure_customer_layout
    source_env
    resolve_chassis_root
    (cd "$(dirname "$CHASSIS_ROOT")" && exec python3 -m chassis.db.migrate "$@")
}

cmd_dispatcher() {
    ensure_customer_layout
    source_env
    resolve_chassis_root
    run_plugin_fetch
    resolve_plugin_root
    run_chassis_migrations
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
    resolve_chassis_root
    run_plugin_fetch
    resolve_plugin_root
    run_chassis_migrations
    # bootstrap.sh sits at the repo root, one level above the chassis tree -
    # run the copy that belongs to the RESOLVED tree so a mounted-clone
    # install bootstraps with current code, not the baked snapshot.
    local bootstrap_script="$(dirname "$CHASSIS_ROOT")/bootstrap.sh"
    [[ -f "$bootstrap_script" ]] || bootstrap_script="/app/bootstrap.sh"
    log "running $bootstrap_script against CHASSIS_HOME=$CHASSIS_HOME"
    CHASSIS_HOME="$CHASSIS_HOME" bash "$bootstrap_script" "$@"
}

cmd_install_plugin() {
    local name="${1:?install-plugin requires a plugin name}"
    ensure_customer_layout
    source_env
    resolve_chassis_root
    resolve_plugin_root
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
    resolve_chassis_root
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
    resolve_chassis_root
    resolve_plugin_root
    log "running chassis smoke tests"
    CHASSIS_HOME="$CHASSIS_HOME" bash "$CHASSIS_ROOT/scripts/smoke-test.sh" "$@"
}

cmd_claude() {
    ensure_customer_layout
    source_env
    resolve_chassis_root
    resolve_plugin_root
    exec claude "$@"
}

cmd_shell() {
    ensure_customer_layout
    source_env
    resolve_chassis_root
    resolve_plugin_root
    exec /usr/bin/zsh
}

case "$MODE" in
    dispatcher)       cmd_dispatcher       "$@" ;;
    bootstrap)        cmd_bootstrap        "$@" ;;
    install-plugin)   cmd_install_plugin   "$@" ;;
    hydrate-env)      cmd_hydrate_env      "$@" ;;
    migrate)          cmd_migrate          "$@" ;;
    smoke-test)       cmd_smoke_test       "$@" ;;
    claude)           cmd_claude           "$@" ;;
    shell)            cmd_shell            "$@" ;;
    *)
        echo "unknown mode: $MODE" >&2
        echo "valid modes: dispatcher | bootstrap | install-plugin <name> | hydrate-env | migrate | smoke-test | claude | shell" >&2
        exit 2
        ;;
esac
