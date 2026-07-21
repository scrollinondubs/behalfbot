#!/usr/bin/env bash
# _env.sh - canonical env-var resolution for the customer-state split (issue #6).
#
# Source this from any chassis-side script that needs to know where customer
# state lives. It establishes the hard separation between:
#
#   CHASSIS_ROOT          - chassis source code root (read-only, disposable).
#                           Container: /app/chassis (baked into image).
#                           Host: ${CHASSIS_HOME}/chassis (under the chassis git
#                           tree, e.g. ~/behalfbot/chassis).
#
#   CHASSIS_PLUGINS_ROOT  - plugins source root (read-only, disposable).
#                           Container: /app/plugins (baked).
#                           Host: ${CHASSIS_HOME}/plugins (e.g. ~/behalfbot/plugins).
#
#   CHASSIS_HOME          - HOST-side chassis git tree root, ONLY. Holds chassis/,
#                           plugins/, bootstrap.sh, docker-compose.yml, Dockerfile.
#                           Fully disposable. Re-pullable via git clone or docker
#                           pull. Inside the container this var is bound to the
#                           customer dir for backward compat (legacy scripts).
#
#   CUSTOMER_HOME         - customer state root. Survives reinstall. Holds .env,
#                           CLAUDE.md, HEARTBEATS.md, scripts/, state/, logs/,
#                           briefings/, memory/, data/, etc.
#                           Container: /app/customer (bind-mounted from host).
#                           Host (NEW installs):   ~/.behalfbot
#                           Host (LEGACY installs, pre-#6): same as CHASSIS_HOME
#                             (i.e. ~/behalfbot - customer state co-located).
#
# Backward-compat contract:
#   - If CUSTOMER_HOME is unset, fall back to CHASSIS_HOME. This keeps every
#     pre-#6 install working without changes - they continue pointing both vars
#     at the same dir.
#   - If CHASSIS_HOME is unset, fall back to CUSTOMER_HOME (e.g. when a script
#     is sourced in a context that only knows about the customer side).
#   - New code should prefer CUSTOMER_HOME for customer-side paths and
#     CHASSIS_ROOT / CHASSIS_PLUGINS_ROOT for chassis-side paths.
#
# This file is idempotent - sourcing it multiple times is fine.

# Resolve CUSTOMER_HOME first because the rest derive from it in the legacy case.
if [[ -z "${CUSTOMER_HOME:-}" ]]; then
    if [[ -n "${CHASSIS_HOME:-}" ]]; then
        # Legacy install: customer state co-located with chassis tree.
        export CUSTOMER_HOME="$CHASSIS_HOME"
    elif [[ -d "$HOME/.behalfbot" ]]; then
        # New install layout: customer state at ~/.behalfbot
        export CUSTOMER_HOME="$HOME/.behalfbot"
    elif [[ -d "/app/customer" ]]; then
        # In-container default
        export CUSTOMER_HOME="/app/customer"
    fi
fi

# CHASSIS_HOME is the chassis tree on host. In container land we keep it
# aliased to CUSTOMER_HOME for backward compat (existing chassis scripts
# treat $CHASSIS_HOME/.env etc. as customer-side - those paths happen to
# still resolve correctly when CHASSIS_HOME points at /app/customer).
if [[ -z "${CHASSIS_HOME:-}" ]]; then
    if [[ -n "${CUSTOMER_HOME:-}" ]]; then
        export CHASSIS_HOME="$CUSTOMER_HOME"
    fi
fi

# Chassis source root. On host this lives under the chassis git tree; in
# container it's baked at /app/chassis. Allow override via existing env.
if [[ -z "${CHASSIS_ROOT:-}" ]]; then
    if [[ -d "/app/chassis" ]]; then
        export CHASSIS_ROOT="/app/chassis"
    elif [[ -n "${CHASSIS_HOME:-}" && -d "$CHASSIS_HOME/chassis" ]]; then
        export CHASSIS_ROOT="$CHASSIS_HOME/chassis"
    fi
fi

# Plugin root resolution (behalfbot#82).
#
# Delegates to resolve-plugin-root.sh, which overlays the fetched tree
# ($CUSTOMER_HOME/vendored-plugins) over the baked one PER PLUGIN NAME and
# materialises the result as a composed symlink root under
# $CUSTOMER_HOME/state/plugins-root. See that script for the full contract,
# including the operator-override rule (a pre-set CHASSIS_PLUGINS_ROOT is
# honoured verbatim - which is why nothing may default this variable before
# resolution runs; the Dockerfile ENV that used to do so was what made the
# v0.2.0 fetched-tree preference unreachable).
#
# The old single-root preference chain is kept only as a fallback for the
# window where a stale chassis tree lacks the resolver script.
if [[ -z "${CHASSIS_PLUGINS_ROOT:-}" ]]; then
    _cpr_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
    _cpr=""
    if [[ -n "$_cpr_dir" && -x "$_cpr_dir/resolve-plugin-root.sh" ]]; then
        _cpr="$(bash "$_cpr_dir/resolve-plugin-root.sh" 2>/dev/null)" || true
    fi
    if [[ -n "$_cpr" ]]; then
        export CHASSIS_PLUGINS_ROOT="$_cpr"
    elif [[ -d "/app/plugins" ]]; then
        export CHASSIS_PLUGINS_ROOT="/app/plugins"
    elif [[ -n "${CHASSIS_HOME:-}" && -d "$CHASSIS_HOME/plugins" ]]; then
        export CHASSIS_PLUGINS_ROOT="$CHASSIS_HOME/plugins"
    fi
    unset _cpr _cpr_dir
fi
