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
# Order of preference:
#   1. $CUSTOMER_HOME/vendored-plugins - the tree fetched from
#      scrollinondubs/behalfbot-plugins at the pinned tag+SHA. This is the
#      authoritative source once a fetch has succeeded.
#   2. /app/plugins - the image-baked tree. The fallback when no fetch has run
#      yet, when the fetch failed, or on an air-gapped/frozen install.
#   3. $CHASSIS_HOME/plugins - host-side installs with no container layout.
#
# The non-empty test on (1) is the important part and is not decoration. An
# empty or half-written vendored-plugins directory must NOT shadow the baked
# tree: that would turn a failed fetch into a chassis that silently loads no
# plugins and reports nothing wrong. A directory only counts as a usable
# plugin root if it actually contains at least one plugin manifest.
_chassis_plugin_root_usable() {
    local dir="$1"
    [[ -d "$dir" ]] || return 1
    compgen -G "$dir"/*/openclaw.plugin.json > /dev/null 2>&1
}

if [[ -z "${CHASSIS_PLUGINS_ROOT:-}" ]]; then
    _vendored="${CUSTOMER_HOME:-${CHASSIS_HOME:-}}/vendored-plugins"
    if _chassis_plugin_root_usable "$_vendored"; then
        export CHASSIS_PLUGINS_ROOT="$_vendored"
    elif [[ -d "/app/plugins" ]]; then
        export CHASSIS_PLUGINS_ROOT="/app/plugins"
    elif [[ -n "${CHASSIS_HOME:-}" && -d "$CHASSIS_HOME/plugins" ]]; then
        export CHASSIS_PLUGINS_ROOT="$CHASSIS_HOME/plugins"
    fi
    unset _vendored
fi
