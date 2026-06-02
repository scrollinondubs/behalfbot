#!/usr/bin/env bash
# bootstrap-customer-scripts.sh - render per-customer scripts and LaunchAgent
# plists from chassis-shipped templates into $CUSTOMER_HOME.
#
# Invoked by:
#   - bootstrap.sh (first install) - scaffolds the script set
#   - migrate-customer-state.sh (existing-install migration) - back-fills
#     anything missing during the customer-state move
#   - manually after a chassis pull when a template has changed
#
# Idempotent: re-running re-renders every template, overwriting prior renders.
# That's the intended behavior - the rendered scripts are chassis-managed
# artifacts that live on the CUSTOMER side of the split (so they survive
# CHASSIS_HOME teardown) but their canonical source-of-truth is in the
# chassis tree under chassis/scripts/templates/ and chassis/launchd/.
#
# Env contract (any of these can be in the environment, .env, or
# chassis.config.yaml; .env wins over yaml, env wins over both):
#   CUSTOMER_HOME       required - per-customer state root
#   CHASSIS_HOME        required - chassis git tree root
#   BOT_NAME            required - logical bot name (jax, asimov, ozzy)
#   TMUX_SESSION_NAME   default: ${BOT_NAME}-discord
#   INSTANCE_NAME       default: BOT_NAME (capitalised)
#   CLAUDE_CHANNELS     default: plugin:discord@claude-plugins-official
#   HOMEBREW_PREFIX     default: /opt/homebrew on Apple Silicon, else /usr/local
#   NODE_BIN_PATH       optional extra node bin dir
#
# Usage:
#   CUSTOMER_HOME=~/.behalfbot CHASSIS_HOME=~/behalfbot BOT_NAME=jax \
#       bash chassis/scripts/bootstrap-customer-scripts.sh [--dry-run]
#
# Flags:
#   --dry-run   show what would be rendered, write nothing.
#   --plists    also render the LaunchAgent plists into $CUSTOMER_HOME/launchd/.
#               (Default: scripts only - plists are macOS-specific and require
#               an explicit follow-up `launchctl bootstrap` step.)

set -euo pipefail

# Resolve canonical env helper
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_env.sh"

DRY_RUN="${DRY_RUN:-false}"
RENDER_PLISTS=false
for arg in "$@"; do
    case "$arg" in
        --dry-run)  DRY_RUN=true ;;
        --plists)   RENDER_PLISTS=true ;;
        *)          echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

: "${CUSTOMER_HOME:?CUSTOMER_HOME must be set}"
: "${CHASSIS_HOME:?CHASSIS_HOME must be set}"
: "${BOT_NAME:?BOT_NAME must be set (e.g. jax, asimov, ozzy)}"

TMUX_SESSION_NAME="${TMUX_SESSION_NAME:-${BOT_NAME}-discord}"
INSTANCE_NAME="${INSTANCE_NAME:-${BOT_NAME}}"
CLAUDE_CHANNELS="${CLAUDE_CHANNELS:-plugin:discord@claude-plugins-official}"

# Detect homebrew prefix on macOS; default to /opt/homebrew for Apple Silicon
# which is the modern reference shape.
if [[ -z "${HOMEBREW_PREFIX:-}" ]]; then
    if [[ -d "/opt/homebrew/bin" ]]; then
        HOMEBREW_PREFIX="/opt/homebrew"
    elif [[ -d "/usr/local/Homebrew" ]]; then
        HOMEBREW_PREFIX="/usr/local"
    else
        HOMEBREW_PREFIX="/usr/local"
    fi
fi

# NODE_BIN_PATH is optional. If unset, default to homebrew's node@22 keg.
# Either resolves; one of them existing is enough.
NODE_BIN_PATH="${NODE_BIN_PATH:-${HOMEBREW_PREFIX}/opt/node@22/bin}"

USER_UID="${USER_UID:-$(id -u)}"
USER_NAME="${USER:-$(id -un)}"

TEMPLATES_DIR="${CHASSIS_ROOT:-$CHASSIS_HOME/chassis}/scripts/templates"
LAUNCHD_DIR="${CHASSIS_ROOT:-$CHASSIS_HOME/chassis}/launchd"
OUT_SCRIPTS="$CUSTOMER_HOME/scripts"
OUT_PLISTS="$CUSTOMER_HOME/launchd"

mkdir -p "$OUT_SCRIPTS"
if [[ "$RENDER_PLISTS" == "true" ]]; then
    mkdir -p "$OUT_PLISTS"
fi

# render_template <template_path> <output_path>
# Substitutes ${BOT_NAME}, ${TMUX_SESSION_NAME}, ${CUSTOMER_HOME},
# ${CHASSIS_HOME}, ${CLAUDE_CHANNELS}, ${INSTANCE_NAME}, ${HOMEBREW_PREFIX},
# ${NODE_BIN_PATH}, ${USER}, ${USER_UID} via sed. Anything not in this list
# is passed through verbatim - templates are responsible for not including
# accidental ${...} tokens that would silently survive.
render_template() {
    local src="$1" dst="$2"
    if [[ ! -f "$src" ]]; then
        echo "ERROR: template missing: $src" >&2
        return 1
    fi

    local rendered
    rendered=$(sed \
        -e "s|\${BOT_NAME}|${BOT_NAME}|g" \
        -e "s|\${TMUX_SESSION_NAME}|${TMUX_SESSION_NAME}|g" \
        -e "s|\${CUSTOMER_HOME}|${CUSTOMER_HOME}|g" \
        -e "s|\${CHASSIS_HOME}|${CHASSIS_HOME}|g" \
        -e "s|\${CLAUDE_CHANNELS}|${CLAUDE_CHANNELS}|g" \
        -e "s|\${INSTANCE_NAME}|${INSTANCE_NAME}|g" \
        -e "s|\${HOMEBREW_PREFIX}|${HOMEBREW_PREFIX}|g" \
        -e "s|\${NODE_BIN_PATH}|${NODE_BIN_PATH}|g" \
        -e "s|\${USER}|${USER_NAME}|g" \
        -e "s|\${USER_UID}|${USER_UID}|g" \
        "$src")

    if [[ "$DRY_RUN" == "true" ]]; then
        echo "[dry-run] would render $src -> $dst"
        return 0
    fi

    printf '%s\n' "$rendered" > "$dst"
    chmod +x "$dst" 2>/dev/null || true
    echo "rendered $dst"
}

# Per-bot script renderers
render_template "$TEMPLATES_DIR/restart-discord.sh.template" \
    "$OUT_SCRIPTS/restart-${BOT_NAME}-discord.sh"
render_template "$TEMPLATES_DIR/watchdog-discord.sh.template" \
    "$OUT_SCRIPTS/watchdog-${BOT_NAME}-discord.sh"

if [[ "$RENDER_PLISTS" == "true" ]]; then
    render_template "$LAUNCHD_DIR/com.behalfbot.discord-restart.plist.template" \
        "$OUT_PLISTS/com.behalfbot.${BOT_NAME}-discord-restart.plist"
    render_template "$LAUNCHD_DIR/com.behalfbot.discord-watchdog.plist.template" \
        "$OUT_PLISTS/com.behalfbot.${BOT_NAME}-discord-watchdog.plist"
    render_template "$LAUNCHD_DIR/com.behalfbot.heartbeat-dispatcher.plist.template" \
        "$OUT_PLISTS/com.behalfbot.heartbeat-dispatcher.plist"
fi

if [[ "$DRY_RUN" != "true" ]]; then
    echo ""
    echo "Customer-side scripts rendered into: $OUT_SCRIPTS"
    if [[ "$RENDER_PLISTS" == "true" ]]; then
        echo "Customer-side plists rendered into:  $OUT_PLISTS"
        echo ""
        echo "Next: link the plists into ~/Library/LaunchAgents and load them:"
        echo "  ln -sf $OUT_PLISTS/com.behalfbot.${BOT_NAME}-discord-restart.plist ~/Library/LaunchAgents/"
        echo "  ln -sf $OUT_PLISTS/com.behalfbot.${BOT_NAME}-discord-watchdog.plist ~/Library/LaunchAgents/"
        echo "  launchctl bootstrap gui/${USER_UID} ~/Library/LaunchAgents/com.behalfbot.${BOT_NAME}-discord-restart.plist"
        echo "  launchctl bootstrap gui/${USER_UID} ~/Library/LaunchAgents/com.behalfbot.${BOT_NAME}-discord-watchdog.plist"
    fi
fi
