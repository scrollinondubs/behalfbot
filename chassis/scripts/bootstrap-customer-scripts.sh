#!/usr/bin/env bash
# bootstrap-customer-scripts.sh - render per-customer scripts and launchd
# plists from chassis-shipped templates into $CUSTOMER_HOME, then (optionally)
# install them into the right launchd domain.
#
# Per chassis#14, chassis-shipped host plists split into two domains:
#   - LaunchDaemon (/Library/LaunchDaemons/, system-level)  - survives
#     unattended reboot, no GUI/Aqua session needed. Requires sudo to install.
#     Default for jobs that only docker-exec / hit network / run headless.
#   - LaunchAgent  (~/Library/LaunchAgents/, user-level)    - needs an Aqua
#     session, dies silently across an unattended reboot. Use only for jobs
#     that genuinely touch the display (Android emulator, Playwright Chromium).
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
#   --dry-run    show what would be rendered, write nothing.
#   --plists     also render the launchd plists into $CUSTOMER_HOME/launchd/.
#                (Default: scripts only - plists are macOS-specific and require
#                an explicit follow-up activation step, see --activate-plists.)
#   --activate-plists
#                after rendering, actually install the plists into the right
#                launchd domain (LaunchDaemons or LaunchAgents) and bootstrap
#                them. This will prompt for sudo for daemon-domain plists.
#                Implies --plists. No-op on non-macOS hosts.

set -euo pipefail

# Resolve canonical env helper
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_env.sh"

DRY_RUN="${DRY_RUN:-false}"
RENDER_PLISTS=false
ACTIVATE_PLISTS=false
for arg in "$@"; do
    case "$arg" in
        --dry-run)          DRY_RUN=true ;;
        --plists)           RENDER_PLISTS=true ;;
        --activate-plists)  RENDER_PLISTS=true; ACTIVATE_PLISTS=true ;;
        *)                  echo "unknown arg: $arg" >&2; exit 2 ;;
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

# Pre-seed Claude Code's per-directory folder-trust state for CUSTOMER_HOME.
# Logic lives in preseed-claude-trust.sh so the restart template can call
# the same helper (chassis#21 follow-up: trust-wipe recovery requires the
# restart path to re-seed too, not just bootstrap).
if [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] would pre-seed claude folder-trust for $CUSTOMER_HOME via preseed-claude-trust.sh"
else
    bash "$SCRIPT_DIR/preseed-claude-trust.sh" "$CUSTOMER_HOME"
fi

if [[ "$RENDER_PLISTS" == "true" ]]; then
    render_template "$LAUNCHD_DIR/com.behalfbot.discord-restart.plist.template" \
        "$OUT_PLISTS/com.behalfbot.${BOT_NAME}-discord-restart.plist"
    render_template "$LAUNCHD_DIR/com.behalfbot.discord-watchdog.plist.template" \
        "$OUT_PLISTS/com.behalfbot.${BOT_NAME}-discord-watchdog.plist"
    # The heartbeat-dispatcher plist is deprecated as of chassis#14 (the
    # dispatcher now runs inside the chassis container). Still rendered for
    # the legacy bare-metal V1 install path; do NOT activate it as a daemon.
    render_template "$LAUNCHD_DIR/com.behalfbot.heartbeat-dispatcher.plist.template" \
        "$OUT_PLISTS/com.behalfbot.heartbeat-dispatcher.plist"
fi

# install_plist <rendered-plist-path> <agent|daemon>
# Install a rendered plist into the correct launchd domain and bootstrap it.
#   agent:   ~/Library/LaunchAgents/, gui/<uid> domain, no sudo.
#   daemon:  /Library/LaunchDaemons/, system domain, requires sudo.
# Re-runs are idempotent: bootouts any existing job at the same label first,
# then re-bootstraps the fresh copy.
install_plist() {
    local src="$1" type="$2"
    if [[ ! -f "$src" ]]; then
        echo "  WARN: plist not found, skipping: $src" >&2
        return 0
    fi

    local label
    label="$(basename "$src" .plist)"

    case "$type" in
        agent)
            local la_dir="$HOME/Library/LaunchAgents"
            local dst
            dst="$la_dir/$(basename "$src")"
            if [[ "$DRY_RUN" == "true" ]]; then
                echo "  [dry-run] install agent: $src -> $dst"
                echo "  [dry-run] launchctl bootout gui/${USER_UID}/${label} (if loaded)"
                echo "  [dry-run] launchctl bootstrap gui/${USER_UID} $dst"
                return 0
            fi
            mkdir -p "$la_dir"
            ln -sf "$src" "$dst"
            if launchctl print "gui/${USER_UID}/${label}" >/dev/null 2>&1; then
                launchctl bootout "gui/${USER_UID}/${label}" || true
            fi
            launchctl bootstrap "gui/${USER_UID}" "$dst" || true
            echo "  loaded agent: $dst"
            ;;
        daemon)
            local ld_dir="/Library/LaunchDaemons"
            local dst
            dst="$ld_dir/$(basename "$src")"
            if [[ "$DRY_RUN" == "true" ]]; then
                echo "  [dry-run] install daemon: sudo cp $src $dst"
                echo "  [dry-run] sudo chown root:wheel $dst && sudo chmod 644 $dst"
                echo "  [dry-run] sudo launchctl bootout system/${label} (if loaded)"
                echo "  [dry-run] sudo launchctl bootstrap system $dst"
                return 0
            fi
            sudo cp "$src" "$dst"
            sudo chown root:wheel "$dst"
            sudo chmod 644 "$dst"
            if sudo launchctl print "system/${label}" >/dev/null 2>&1; then
                sudo launchctl bootout "system/${label}" || true
            fi
            sudo launchctl bootstrap system "$dst" || true
            echo "  loaded daemon: $dst"
            ;;
        *)
            echo "  ERROR: install_plist: unknown type '$type' (expected agent|daemon)" >&2
            return 2
            ;;
    esac
}

# DOMAIN MAP for chassis-shipped host plists. Update this list when adding
# new chassis-side plists. See docs section "LaunchDaemon vs LaunchAgent -
# which to use" for the decision rule.
#
# Format: "<plist-basename> <agent|daemon>". Plists not listed here are
# rendered but not auto-installed; the operator activates them manually.
CHASSIS_PLIST_DOMAINS=(
    "com.behalfbot.${BOT_NAME}-discord-restart.plist daemon"
    "com.behalfbot.${BOT_NAME}-discord-watchdog.plist daemon"
    # heartbeat-dispatcher.plist deliberately NOT listed - deprecated per #14.
)

if [[ "$ACTIVATE_PLISTS" == "true" ]]; then
    if [[ "$(uname -s)" != "Darwin" ]]; then
        echo "  --activate-plists requested but host is not macOS; skipping."
    else
        # Pre-flight sudo prompt so the operator isn't surprised mid-loop.
        daemon_count=0
        for entry in "${CHASSIS_PLIST_DOMAINS[@]}"; do
            # shellcheck disable=SC2086
            set -- $entry
            [[ "${2:-}" == "daemon" ]] && daemon_count=$((daemon_count + 1))
        done
        if [[ "$daemon_count" -gt 0 && "$DRY_RUN" != "true" ]]; then
            echo ""
            echo "About to install $daemon_count LaunchDaemon(s) into /Library/LaunchDaemons/."
            echo "This requires sudo. You may be prompted for your password now."
            # Touch sudo once up front so the rest of the loop runs without
            # additional prompts (within sudo's default timestamp window).
            sudo -v || {
                echo "  sudo refused; skipping daemon installation." >&2
                ACTIVATE_PLISTS=false
            }
        fi
    fi
fi

if [[ "$ACTIVATE_PLISTS" == "true" && "$(uname -s)" == "Darwin" ]]; then
    echo ""
    echo "Activating chassis-shipped host plists..."
    for entry in "${CHASSIS_PLIST_DOMAINS[@]}"; do
        # shellcheck disable=SC2086
        set -- $entry
        plist_name="$1"
        plist_type="${2:-agent}"
        install_plist "$OUT_PLISTS/$plist_name" "$plist_type"
    done
fi

if [[ "$DRY_RUN" != "true" ]]; then
    echo ""
    echo "Customer-side scripts rendered into: $OUT_SCRIPTS"
    if [[ "$RENDER_PLISTS" == "true" ]]; then
        echo "Customer-side plists rendered into:  $OUT_PLISTS"
        if [[ "$ACTIVATE_PLISTS" != "true" ]]; then
            echo ""
            echo "Next: activate the plists into the right launchd domain."
            echo "      (chassis#14: discord-restart + discord-watchdog are now"
            echo "       LaunchDaemons so they survive unattended reboots.)"
            echo ""
            echo "      Easiest: re-run this script with --activate-plists, which"
            echo "      handles the sudo-cp-bootstrap dance for daemons and the"
            echo "      symlink-bootstrap dance for agents."
            echo ""
            echo "      Or manually, for each daemon:"
            echo "        sudo cp $OUT_PLISTS/com.behalfbot.${BOT_NAME}-discord-restart.plist  /Library/LaunchDaemons/"
            echo "        sudo cp $OUT_PLISTS/com.behalfbot.${BOT_NAME}-discord-watchdog.plist /Library/LaunchDaemons/"
            echo "        sudo chown root:wheel /Library/LaunchDaemons/com.behalfbot.${BOT_NAME}-discord-{restart,watchdog}.plist"
            echo "        sudo chmod 644       /Library/LaunchDaemons/com.behalfbot.${BOT_NAME}-discord-{restart,watchdog}.plist"
            echo "        sudo launchctl bootstrap system /Library/LaunchDaemons/com.behalfbot.${BOT_NAME}-discord-restart.plist"
            echo "        sudo launchctl bootstrap system /Library/LaunchDaemons/com.behalfbot.${BOT_NAME}-discord-watchdog.plist"
        fi
    fi
fi
