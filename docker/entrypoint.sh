#!/usr/bin/env bash
# Behalf.bot chassis container entrypoint
# =======================================
# Modes (passed as CMD or `docker compose run`):
#   dispatcher       - long-running gather-first heartbeat loop (default CMD).
#                      Also supervises the Discord control listener (the
#                      remote kill switch), so no compose change is needed
#                      on an existing install to gain it.
#   control-listener - the kill switch on its own, for installs that would
#                      rather run it as a separate service
#   bootstrap        - one-shot: hydrate .mcp.json/CLAUDE.md/HEARTBEATS.md, seed memory
#   install-plugin <name>
#                    - one-shot: run plugins/<name>/install.sh
#   hydrate-env      - one-shot: pull secrets from Vaultwarden via rbw, write to .env
#   migrate          - one-shot: apply chassis/db/migrations/*.sql
#   smoke-test       - one-shot: run chassis + plugin smoke checks
#   claude           - interactive Claude CLI (needs -it)
#   shell            - interactive zsh (needs -it)
#   update-cli [ver] - one-shot: update the Claude Code CLI in place
#                      (default `latest`). The image bakes whatever was
#                      current at build time, so a long-lived container
#                      falls behind and eventually gets 400s from the API
#                      for newer models.
#
# The dispatcher loop reads HEARTBEATS.md from /app/customer (bind-mounted)
# and invokes /app/chassis/scheduled-tasks/heartbeat-dispatcher.sh on a fixed
# tick. /tmp/dispatcher.alive is touched at tick start, kept fresh by a
# keepalive while the tick runs, and touched again at tick end - so the
# container healthcheck reflects "the loop is alive", not "a tick recently
# finished" (behalfbot#160).

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

# Give npm a cache directory owned by whichever UID is actually running.
#
# Why: two different UIDs share /home/chassis in a running container. The
# baked `chassis` user is UID 1000; an install that pulls the published image
# without rebuilding runs as the host UID instead (501 on macOS). The
# Dockerfile already chmods .npm to 0777 at build time, but that only covers
# the directories that exist at build. Every file npm CREATES at runtime is
# owned by the UID that created it at the default umask, so the first UID to
# run npm leaves _logs/ and _cacache/ entries the other UID cannot write.
#
# Concrete failure 2026-09-22 on the v1 reference install: `claude update`
# inside the container died with `Insufficient permissions to install update`
# as UID 501, then with `sudo chown -R 1000:1000 "/home/chassis/.npm"` as
# UID 1000, because /home/chassis/.npm/_logs was full of files owned by the
# other UID. Net effect: the container could not update its own CLI. It sat
# on Claude Code 2.1.261 while the host ran 2.1.280, old enough that the API
# refused newer models outright with a 400.
#
# A per-UID cache path removes the sharing, so no UID can poison another's
# cache. /tmp is container-local and disposable, the right lifetime for a
# cache. NPM_CONFIG_CACHE still wins if an operator sets it explicitly, via
# compose `environment:` or `docker -e`. Note this runs before source_env, so
# a value set only in the customer .env is not picked up here - that is
# deliberate, since npm may be invoked by modes that never source .env.
# The same UID split breaks /home/chassis/.npmrc, which npm writes at 0600
# owned by the build-time user. A runtime UID of 501 can neither read it (so
# it silently loses the baked `prefix=/home/chassis/.local`) nor write it
# (`EACCES ... path: '/home/chassis/.npmrc'` on any `npm config set`). Point
# npm at a per-UID user config as well, and carry the prefix forward in the
# environment so it survives not being able to read the baked file.
configure_npm_cache() {
    local uid
    uid="$(id -u)"
    export npm_config_cache="${NPM_CONFIG_CACHE:-/tmp/npm-cache-$uid}"
    export npm_config_userconfig="${NPM_CONFIG_USERCONFIG:-/tmp/npmrc-$uid}"
    export npm_config_prefix="${CLAUDE_NPM_PREFIX:-/home/chassis/.local}"
    mkdir -p "$npm_config_cache" 2>/dev/null || true
    touch "$npm_config_userconfig" 2>/dev/null || true
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
    # Keep /tmp/dispatcher.alive fresh for the duration of the tick, not just
    # at the end of it. Without this, the sentinel's age at the next touch is
    # tick_duration + sleep interval - any tick over ~300s crosses the
    # healthcheck's 1200s staleness threshold, flipping the container to
    # unhealthy while the dispatcher is doing exactly what it should
    # (behalfbot#160). If the loop dies, the keepalive subshell dies with it
    # and the sentinel goes stale as intended.
    touch /tmp/dispatcher.alive
    ( while sleep 60; do touch /tmp/dispatcher.alive; done ) &
    local keepalive=$!
    if ! CHASSIS_HOME="$CHASSIS_HOME" /usr/bin/zsh "$DISPATCHER_SCRIPT"; then
        log "dispatcher tick failed (continuing)"
    fi
    # `|| true` on BOTH is load-bearing under `set -e`. `wait` on a job we
    # just SIGTERM'd returns 143, and `kill` returns 1 if the subshell already
    # exited - either one takes PID 1 down and the container restarts. That is
    # not theoretical: it shipped in the 2026-08-18 image and put this install
    # into a ~19s restart loop for 25 hours (every boot ran one full tick, then
    # died on the `wait` the moment the tick returned).
    kill "$keepalive" 2>/dev/null || true
    wait "$keepalive" 2>/dev/null || true
    touch /tmp/dispatcher.alive
}

supervise_control_listener() {
    # The remote kill switch (behalfbot#550). Runs as a child of the dispatcher
    # loop rather than a separate compose service on purpose: every existing
    # install gains it by pulling a new image, with no customer-side compose
    # edit. Installs that want the stronger isolation of a separate container
    # can run this image with the `control-listener` mode instead.
    local script="$CHASSIS_ROOT/scripts/discord-control-listener.py"
    local logfile="$CUSTOMER_HOME/logs/scheduled/control-listener.log"

    if [[ ! -f "$script" ]]; then
        log "WARN: control listener not found at $script - remote kill switch UNARMED"
        return
    fi

    # Config probe first, so the reason lands in the CONTAINER log where an
    # operator looks, not only in a file nobody reads. #550's other lesson was
    # a monitor that reported into a log for four days.
    local rc=0
    python3 "$script" --check >/dev/null 2>&1 || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        log "WARN: remote kill switch UNARMED - set CHASSIS_PRINCIPAL_USER_ID (or"
        log "WARN: INSTALLER_DISCORD_USER_ID), DISCORD_BOT_TOKEN and a control channel."
        log "WARN: details: python3 $script (see docs/remote-kill-switch.md)"
        python3 "$script" >> "$logfile" 2>&1 || true
        return
    fi

    log "control listener starting - kill switch armed, log: $logfile"
    while true; do
        rc=0
        python3 "$script" >> "$logfile" 2>&1 || rc=$?
        if [[ "$rc" -eq 78 ]]; then
            log "control listener exited 78 (config) - not restarting. See $logfile"
            return
        fi
        log "control listener exited rc=$rc - restarting in 30s"
        sleep 30
    done
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
    # Background, and deliberately NOT waited on. A wedged or crashed dispatcher
    # tick must not take the kill switch down with it - being able to say "halt"
    # matters most exactly when the rest of the loop is misbehaving.
    supervise_control_listener &
    while true; do
        run_dispatcher_once
        sleep "$DISPATCHER_INTERVAL_SECONDS"
    done
}

cmd_control_listener() {
    ensure_customer_layout
    source_env
    resolve_chassis_root
    local script="$CHASSIS_ROOT/scripts/discord-control-listener.py"
    if [[ ! -f "$script" ]]; then
        log "FATAL: control listener not found at $script"
        exit 2
    fi
    log "running control listener from $script"
    exec python3 "$script"
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

# Update the Claude Code CLI in place, so a long-lived container is not stuck
# on whatever version its image was built with.
#
# The image installs @anthropic-ai/claude-code@latest at build time, which
# means "latest as of the build" and nothing more. A container that has been
# up for weeks is weeks behind, and the API rejects models outright once the
# CLI is old enough, so this is a hard failure rather than a slow drift.
#
# `claude update` is deliberately NOT used here. It shells out to a global npm
# install that assumes the invoking UID owns the install tree, which is the
# assumption that breaks in this image (see configure_npm_cache above). Going
# through npm directly with an explicit prefix keeps it working at any UID.
#
# umask 0000 matters: it makes the files npm writes group- and other-writable,
# so the NEXT update still works even if it runs as a different UID. Without
# it, one successful update re-creates node_modules at 0755 owned by the
# updating user and locks the other UID out again, which is exactly the state
# the v1 reference install was found in.
cmd_update_cli() {
    local target="${1:-latest}"
    local prefix="${CLAUDE_NPM_PREFIX:-/home/chassis/.local}"

    log "updating claude CLI to '$target' (prefix $prefix, uid $(id -u))"
    log "before: $(claude --version 2>/dev/null || echo 'not installed')"

    # Make the existing install replaceable before touching it. npm swaps a
    # package in by renaming the old directory aside, which needs write access
    # to the tree it is renaming. If the previous install was done by the other
    # UID at a default umask, that access is missing and npm fails with
    # `ENOTEMPTY: directory not empty, rename ... -> .claude-code-XXXXXXXX`,
    # which reads like a stale-file problem but is really a permission one.
    local scope="$prefix/lib/node_modules/@anthropic-ai"
    if [ -d "$scope" ]; then
        chmod -R a+rwX "$scope" 2>/dev/null || true
        # Clear debris from any earlier failed swap. These are npm's rename
        # targets; left behind they make the next run fail the same way.
        rm -rf "$scope"/.claude-code-* 2>/dev/null || true
    fi

    # npm_config_prefix is exported by configure_npm_cache, so no `npm config
    # set` is needed. That matters: `npm config set` writes the user npmrc,
    # which is exactly the file a non-owning UID cannot open.
    (
        umask 0000
        npm_config_prefix="$prefix" npm install -g "@anthropic-ai/claude-code@${target}"
    )

    # npm honours umask for files it writes, but directories it creates via
    # mkdir can still come back restrictive on some npm versions. Re-assert
    # any-UID access rather than trusting that.
    chmod -R a+rwX "$prefix/lib/node_modules/@anthropic-ai" 2>/dev/null || true

    log "after: $(claude --version 2>/dev/null || echo 'MISSING - update failed')"
}

# Every mode below may shell out to npm or npx, directly or through a gather
# script, so the cache fix is applied once here rather than per-mode.
configure_npm_cache

case "$MODE" in
    dispatcher)       cmd_dispatcher       "$@" ;;
    control-listener) cmd_control_listener "$@" ;;
    bootstrap)        cmd_bootstrap        "$@" ;;
    install-plugin)   cmd_install_plugin   "$@" ;;
    hydrate-env)      cmd_hydrate_env      "$@" ;;
    migrate)          cmd_migrate          "$@" ;;
    smoke-test)       cmd_smoke_test       "$@" ;;
    claude)           cmd_claude           "$@" ;;
    shell)            cmd_shell            "$@" ;;
    update-cli)       cmd_update_cli       "$@" ;;
    *)
        echo "unknown mode: $MODE" >&2
        echo "valid modes: dispatcher | control-listener | bootstrap | install-plugin <name> | hydrate-env | migrate | smoke-test | claude | shell | update-cli [version]" >&2
        exit 2
        ;;
esac
