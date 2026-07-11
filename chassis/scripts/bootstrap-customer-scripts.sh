#!/usr/bin/env bash
# bootstrap-customer-scripts.sh - render per-customer scripts and launchd
# plists from chassis-shipped templates into $CUSTOMER_HOME, then (optionally)
# install them into the right launchd domain.
#
# chassis-shipped host plists split into two launchd domains:
#   - LaunchAgent  (~/Library/LaunchAgents/, user-level, gui/<uid>) - runs in
#     the Aqua session, so the user's login keychain is reachable. Loads at GUI
#     login, so an unattended reboot with auto-login off does not recover it.
#     REQUIRED for anything that touches the login keychain or spawns a Claude
#     process (or a tmux server hosting one) - see docs/launchd-domains.md.
#   - LaunchDaemon (/Library/LaunchDaemons/, system-level) - loads at boot, no
#     login needed, but runs in launchd's Background session where the login
#     keychain is unreachable even with UserName set. Only for genuinely
#     headless, keychain-free jobs. Requires sudo to install.
#
# chassis#14 got this backwards for discord-restart / discord-watchdog and broke
# every macOS install's keychain access from 2026-06-03 to 2026-07-11. Both are
# LaunchAgents again. Do not promote them back.
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
#                launchd domain (LaunchAgents or LaunchDaemons) and bootstrap
#                them. Agent-domain plists need no sudo; daemon-domain plists
#                prompt for it. Implies --plists. No-op on non-macOS hosts.

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
            echo "  Installing daemon $label into /Library/LaunchDaemons/ (needs sudo)."
            sudo -v
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
# new chassis-side plists. See docs/launchd-domains.md ("LaunchDaemon vs
# LaunchAgent - which to use") for the decision rule.
#
# Format: "<plist-basename> <agent|daemon>". Plists not listed here are
# rendered but not auto-installed; the operator activates them manually.
#
# discord-restart / discord-watchdog are AGENTS: they spawn a host tmux session
# running `claude`, which needs the user's login keychain. A daemon runs in the
# Background session and cannot reach it. chassis#14 got this wrong; do not
# promote them back.
CHASSIS_PLIST_DOMAINS=(
    "com.behalfbot.${BOT_NAME}-discord-restart.plist agent"
    "com.behalfbot.${BOT_NAME}-discord-watchdog.plist agent"
    # heartbeat-dispatcher.plist deliberately NOT listed - deprecated per #14.
)

# Stale-daemon check. An install from the #14 era has these same labels sitting
# in /Library/LaunchDaemons/. A leftover daemon keeps firing the restart script
# from the Background session and re-poisons the shared tmux server, fighting
# the agent we are about to install. Removing it needs sudo, so this script
# refuses to proceed rather than sudo behind the operator's back.
if [[ "$ACTIVATE_PLISTS" == "true" && "$(uname -s)" == "Darwin" ]]; then
    stale_daemons=()
    for entry in "${CHASSIS_PLIST_DOMAINS[@]}"; do
        # shellcheck disable=SC2086
        set -- $entry
        if [[ -f "/Library/LaunchDaemons/$1" ]]; then
            stale_daemons+=("$1")
        fi
    done
    if [[ ${#stale_daemons[@]} -gt 0 ]]; then
        echo "" >&2
        echo "  ERROR: stale LaunchDaemon(s) from a pre-fix install are still present:" >&2
        for d in "${stale_daemons[@]}"; do
            echo "    /Library/LaunchDaemons/$d" >&2
        done
        echo "" >&2
        echo "  They run the tmux-spawning restart script in launchd's Background" >&2
        echo "  session, where the login keychain is unreachable (security error 36)," >&2
        echo "  and they will fight the gui LaunchAgents for the shared tmux server." >&2
        echo "  Remove them first (needs sudo), then re-run:" >&2
        echo "" >&2
        for d in "${stale_daemons[@]}"; do
            echo "    sudo launchctl bootout system/${d%.plist}" >&2
            echo "    sudo rm -f /Library/LaunchDaemons/$d" >&2
        done
        echo "" >&2
        echo "  Or run bootstrap.sh, which offers to do it for you." >&2
        echo "  Background: docs/launchd-domains.md" >&2
        exit 3
    fi
fi

if [[ "$ACTIVATE_PLISTS" == "true" && "$(uname -s)" != "Darwin" ]]; then
    echo "  --activate-plists requested but host is not macOS; skipping."
fi

# Activating the restart agent fires it immediately (RunAtLoad) and, on a box
# whose tmux server was born in the Background session, its first act is
# `tmux kill-server`. Running this script from inside tmux would kill the shell
# it is running in, mid-render.
if [[ "$ACTIVATE_PLISTS" == "true" && "$(uname -s)" == "Darwin" && "$DRY_RUN" != "true" \
      && -n "${TMUX:-}" && "${BOOTSTRAP_ALLOW_TMUX:-0}" != "1" ]]; then
    echo "" >&2
    echo "  ERROR: --activate-plists was run from inside tmux." >&2
    echo "  The discord-restart agent rebuilds the tmux server on activation," >&2
    echo "  which would kill this shell. Re-run outside tmux, or set" >&2
    echo "  BOOTSTRAP_ALLOW_TMUX=1 if this session is expendable." >&2
    exit 4
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
            echo "      discord-restart + discord-watchdog are gui-domain LaunchAgents:"
            echo "      they spawn the tmux session that hosts claude, which needs the"
            echo "      login keychain. LaunchDaemons cannot reach it (docs/launchd-domains.md)."
            echo ""
            echo "      Easiest: re-run this script with --activate-plists (no sudo needed)."
            echo ""
            echo "      Or manually, for each agent:"
            echo "        ln -sf $OUT_PLISTS/com.behalfbot.${BOT_NAME}-discord-restart.plist  ~/Library/LaunchAgents/"
            echo "        ln -sf $OUT_PLISTS/com.behalfbot.${BOT_NAME}-discord-watchdog.plist ~/Library/LaunchAgents/"
            echo "        launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.behalfbot.${BOT_NAME}-discord-restart.plist"
            echo "        launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.behalfbot.${BOT_NAME}-discord-watchdog.plist"
            echo ""
            echo "      Run it outside tmux: activation rebuilds the tmux server."
        fi
    fi
fi
