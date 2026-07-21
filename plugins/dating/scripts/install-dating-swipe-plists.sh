#!/usr/bin/env bash
# install-dating-swipe-plists.sh — fill the slot template + write three
# launchd plists into the installer's ~/Library/LaunchAgents.
#
# Reads `plugins/dating/scheduled-tasks/dating-swipe.plist.template`,
# substitutes placeholders for each slot (1=10:00, 2=14:00, 3=18:00),
# and lands the resulting plists at `~/Library/LaunchAgents/`.
#
# Why this exists instead of static plists in the plugin: launchd plists
# embed absolute paths (PATH, WorkingDirectory, ProgramArguments). Those
# paths are install-specific. Shipping concrete plists in the chassis
# subtree would either bake one installer's paths in or punt
# the path-resolution to the installer manually. This templater is the
# clean middle: chassis ships the SHAPE + placeholder names; each
# installer runs this script once at activation time and the resulting
# concrete plists go into their LaunchAgents dir.
#
# Re-run safely whenever:
#   - The installer's chassis-home moves
#   - The schedule changes (edit the SLOT_HOURS array + re-run)
#   - The plist template gets a new field in a chassis update
#
# Required env (or CLI args) before running:
#   CHASSIS_HOME                 install root that holds .env + chassis/ subtree
#   INSTALLER_LABEL              launchd reverse-DNS namespace, e.g. com.<v1-reference-install>
#                                (no per-customer collisions in launchctl)
#
# Optional env (auto-detected if unset):
#   HOMEBREW_PREFIX              auto-detected via `brew --prefix` or default
#                                /opt/homebrew (Apple Silicon) / /usr/local
#   INSTALLER_LOCAL_BIN_PARENT   default ${HOME}/.local
#   LEGACY_V1_HOME              default ${HOME}/v1-install (legacy V1 root, may
#                                not exist on fresh installs — that's fine,
#                                only used if dating-swipe-host.sh needs it)
#
# Usage:
#   bash plugins/dating/scripts/install-dating-swipe-plists.sh
#   bash plugins/dating/scripts/install-dating-swipe-plists.sh --dry-run
#
# Exit codes:
#   0 = wrote (or would have written, with --dry-run) all three plists
#   1 = bad args
#   2 = missing required env / template / LaunchAgents dir
#   3 = substitution / write failure

set -uo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be exported (install root)}"
: "${INSTALLER_LABEL:?INSTALLER_LABEL must be exported (e.g. com.<v1-reference-install>, com.<installer-name>, etc.)}"

# Auto-detect optional env.
if [[ -z "${HOMEBREW_PREFIX:-}" ]]; then
    HOMEBREW_PREFIX="$(brew --prefix 2>/dev/null || echo "")"
    if [[ -z "$HOMEBREW_PREFIX" ]]; then
        # Sensible default per arch.
        case "$(uname -m)" in
            arm64) HOMEBREW_PREFIX=/opt/homebrew ;;
            *)     HOMEBREW_PREFIX=/usr/local ;;
        esac
    fi
fi
INSTALLER_LOCAL_BIN_PARENT="${INSTALLER_LOCAL_BIN_PARENT:-${HOME}/.local}"
LEGACY_V1_HOME="${LEGACY_V1_HOME:-${HOME}/v1-install}"

TEMPLATE="$CHASSIS_HOME/chassis/plugins/dating/scheduled-tasks/dating-swipe.plist.template"
LAUNCHAGENTS_DIR="$HOME/Library/LaunchAgents"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "ERR: template not found: $TEMPLATE" >&2
    exit 2
fi

mkdir -p "$LAUNCHAGENTS_DIR"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# Slot-to-hour mapping. Reference install ran 10:00 / 14:00 / 18:00 per the
# installer's standing directive. Adjust here if the installer wants
# different anchors - re-run the script after.
# Plain `case` keeps the script bash-3.2 compatible (macOS default).
slot_to_hour() {
    case "$1" in
        1) echo 10 ;;
        2) echo 14 ;;
        3) echo 18 ;;
        *) echo "" ;;
    esac
}

written=()
for slot in 1 2 3; do
    hour=$(slot_to_hour "$slot")
    target="$LAUNCHAGENTS_DIR/${INSTALLER_LABEL}.dating-swipe-${slot}.plist"

    # Substitute placeholders. `envsubst` is the cleanest tool — explicit
    # variable list avoids accidentally expanding unintended `${...}` in
    # the template body.
    rendered=$(
        SLOT="$slot" \
        HOUR="$hour" \
        INSTALLER_LABEL="$INSTALLER_LABEL" \
        CHASSIS_HOME="$CHASSIS_HOME" \
        HOME="$HOME" \
        LEGACY_V1_HOME="$LEGACY_V1_HOME" \
        INSTALLER_LOCAL_BIN_PARENT="$INSTALLER_LOCAL_BIN_PARENT" \
        HOMEBREW_PREFIX="$HOMEBREW_PREFIX" \
        envsubst '${SLOT} ${HOUR} ${INSTALLER_LABEL} ${CHASSIS_HOME} ${HOME} ${LEGACY_V1_HOME} ${INSTALLER_LOCAL_BIN_PARENT} ${HOMEBREW_PREFIX}' \
        < "$TEMPLATE"
    )
    if [[ -z "$rendered" ]]; then
        echo "ERR: envsubst produced empty output for slot $slot" >&2
        exit 3
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        echo "=== slot $slot (hour=$hour) → $target (dry-run) ==="
        printf '%s\n' "$rendered" | head -20
        echo "..."
    else
        printf '%s\n' "$rendered" > "$target"
        echo "wrote $target"
        written+=("$target")
    fi
done

if [[ $DRY_RUN -eq 0 ]]; then
    echo ""
    echo "Next step — load the plists:"
    for f in "${written[@]}"; do
        echo "  launchctl load $f"
    done
fi
