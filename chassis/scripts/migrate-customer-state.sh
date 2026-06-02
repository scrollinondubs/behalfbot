#!/usr/bin/env bash
# migrate-customer-state.sh - one-shot migration from the legacy pre-#6 layout
# (customer state co-located with chassis tree at $CHASSIS_HOME) to the new
# hard-split layout ($CHASSIS_HOME for chassis-disposable code,
# $CUSTOMER_HOME for customer state that survives reinstall).
#
# Background: the chassis tree at $CHASSIS_HOME (e.g. ~/behalfbot) used to mix
# chassis-managed code (chassis/, plugins/, bootstrap.sh, Dockerfile) with
# customer state that must survive a reinstall (logs/, briefings/, state/,
# scripts/, etc.). Only the gitignored dirs survived `rm -rf` + `git clone`;
# anything customer-side that wasn't in .gitignore (notably scripts/ holding
# restart-${BOT}-discord.sh) got nuked silently. Toby's install died for 14h
# overnight on 2026-06-02 because of this exact failure mode. See issue #6.
#
# This script:
#   1. Verifies $CUSTOMER_HOME does not already contain state (refuses if so)
#   2. Creates the $CUSTOMER_HOME subdir layout
#   3. mv's each customer-side artifact out of $CHASSIS_HOME into $CUSTOMER_HOME
#   4. Re-renders launchd plists with new paths and reloads them
#   5. Re-renders customer scripts (restart/watchdog) from chassis templates
#   6. Prints a summary + any follow-up actions
#
# Idempotency: subsequent runs are a no-op once the target dir exists with a
# state file. To re-run from scratch, blow away $CUSTOMER_HOME first.
#
# Flags:
#   --dry-run            print every action without executing
#   --customer-home P    override $CUSTOMER_HOME target (default: $HOME/.behalfbot)
#   --chassis-home P     override $CHASSIS_HOME source (default: $HOME/behalfbot)
#   --bot-name N         override $BOT_NAME (default: parsed from chassis.config.yaml)
#   --skip-launchd       don't touch launchctl - useful on Linux installs or
#                        when launchd state will be handled manually
#
# Exit codes:
#   0  migration succeeded (or dry-run completed cleanly)
#   2  refusing to migrate - target exists or source missing
#   3  template-render step failed
#   4  launchctl reload step failed

set -euo pipefail

DRY_RUN=false
SKIP_LAUNCHD=false
ARG_CUSTOMER_HOME=""
ARG_CHASSIS_HOME=""
ARG_BOT_NAME=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)         DRY_RUN=true; shift ;;
        --skip-launchd)    SKIP_LAUNCHD=true; shift ;;
        --customer-home)   ARG_CUSTOMER_HOME="$2"; shift 2 ;;
        --chassis-home)    ARG_CHASSIS_HOME="$2"; shift 2 ;;
        --bot-name)        ARG_BOT_NAME="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,40p' "$0" | sed 's/^# *//'
            exit 0 ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2 ;;
    esac
done

# Resolve source/target paths
CHASSIS_HOME="${ARG_CHASSIS_HOME:-${CHASSIS_HOME:-$HOME/behalfbot}}"
CUSTOMER_HOME="${ARG_CUSTOMER_HOME:-${CUSTOMER_HOME:-$HOME/.behalfbot}}"
export CHASSIS_HOME CUSTOMER_HOME

say() {
    printf '%s\n' "$*"
}

run() {
    if [[ "$DRY_RUN" == "true" ]]; then
        say "  [dry-run] $*"
        return 0
    fi
    "$@"
}

# Pretty-print a section header
section() {
    say ""
    say "--- $* ---"
}

section "Migration plan"
say "  Source (CHASSIS_HOME): $CHASSIS_HOME"
say "  Target (CUSTOMER_HOME): $CUSTOMER_HOME"
say "  Dry run: $DRY_RUN"
say "  Skip launchd: $SKIP_LAUNCHD"

if [[ ! -d "$CHASSIS_HOME" ]]; then
    say "FATAL: source dir does not exist: $CHASSIS_HOME"
    exit 2
fi

if [[ ! -d "$CHASSIS_HOME/chassis" ]]; then
    say "FATAL: $CHASSIS_HOME does not look like a chassis tree (no chassis/ subdir)."
    say "       Pass --chassis-home <path> to point at the right tree."
    exit 2
fi

# Refuse-if-target-state-file-exists guard.
STATE_FILE_TARGET="$CUSTOMER_HOME/.migrated-from-chassis-home"
if [[ -f "$STATE_FILE_TARGET" ]]; then
    say "Already migrated: $STATE_FILE_TARGET exists."
    say "Re-run by removing $CUSTOMER_HOME first (after backing up if needed)."
    exit 0
fi

# Derive BOT_NAME from chassis.config.yaml if not explicitly passed.
BOT_NAME="${ARG_BOT_NAME:-${BOT_NAME:-}}"
if [[ -z "$BOT_NAME" ]]; then
    if [[ -f "$CHASSIS_HOME/chassis.config.yaml" ]]; then
        BOT_NAME=$(awk -F': *' '/^  *(instance_name|bot_name):/ { gsub(/["'"'"']/, "", $2); print $2; exit }' "$CHASSIS_HOME/chassis.config.yaml" || true)
    fi
    BOT_NAME="${BOT_NAME:-bot}"
fi
export BOT_NAME

section "Step 1: create CUSTOMER_HOME layout"
CUSTOMER_SUBDIRS=(
    scripts
    state
    scheduled-tasks
    memory
    briefings
    logs
    logs/scheduled
    logs/telemetry
    data
    temp
    launchd
)
for d in "${CUSTOMER_SUBDIRS[@]}"; do
    run mkdir -p "$CUSTOMER_HOME/$d"
    say "  created: $CUSTOMER_HOME/$d"
done

# Track what we moved so the summary at the end is concrete.
MOVED_PATHS=()
SKIPPED_PATHS=()

# move_if_present <src-relative> [<dst-relative>]
# Move $CHASSIS_HOME/<src-relative> to $CUSTOMER_HOME/<dst-relative or src-relative>.
# Skips silently if the source doesn't exist.
move_if_present() {
    local src_rel="$1"
    local dst_rel="${2:-$1}"
    local src="$CHASSIS_HOME/$src_rel"
    local dst="$CUSTOMER_HOME/$dst_rel"

    if [[ ! -e "$src" ]]; then
        SKIPPED_PATHS+=("$src_rel (absent)")
        return 0
    fi

    if [[ -e "$dst" ]]; then
        SKIPPED_PATHS+=("$src_rel (target $dst already exists)")
        return 0
    fi

    # Ensure parent dir of dst exists.
    local dst_parent
    dst_parent="$(dirname "$dst")"
    run mkdir -p "$dst_parent"

    run mv "$src" "$dst"
    MOVED_PATHS+=("$src_rel -> $dst_rel")
}

section "Step 2: move customer-side artifacts"

# Customer dotfiles / config (top-level)
move_if_present ".env"
move_if_present ".env.baked"
move_if_present ".mcp.json"
move_if_present "CLAUDE.md"
move_if_present "HEARTBEATS.md"
move_if_present "chassis.config.yaml"
move_if_present "INSTALL_PROFILE.md"
move_if_present "triggers.yaml"

# Customer-side directories
move_if_present "scripts"
move_if_present "state"
move_if_present "scheduled-tasks"
move_if_present "briefings"
move_if_present "logs"
move_if_present "data"
move_if_present "temp"
# Top-level memory/ on legacy installs (Sean's stack): the MCP memory graph
# at memory/memory.jsonl. Newer layouts may not have this.
move_if_present "memory"

# Memory: was at chassis/memory/* in legacy installs. Move installer-specific
# memory entries (lead_*, topic_*, feedback_*, project_*, reference_*,
# student_*, user_*, task_*, MEMORY.md) - leave the chassis-tree memory/
# directory structure (.gitkeep + non-installer scaffolding) alone, since
# that's chassis-disposable.
if [[ -d "$CHASSIS_HOME/chassis/memory" ]]; then
    say "  inspecting $CHASSIS_HOME/chassis/memory for installer entries..."
    run mkdir -p "$CUSTOMER_HOME/memory"
    shopt -s nullglob
    for pattern in MEMORY.md lead_*.md topic_*.md feedback_*.md project_*.md reference_*.md student_*.md user_*.md task_*.md installer/; do
        for src in "$CHASSIS_HOME/chassis/memory/"$pattern; do
            [[ -e "$src" ]] || continue
            local_dst="$CUSTOMER_HOME/memory/$(basename "$src")"
            if [[ -e "$local_dst" ]]; then
                SKIPPED_PATHS+=("chassis/memory/$(basename "$src") (target exists)")
                continue
            fi
            run mv "$src" "$local_dst"
            MOVED_PATHS+=("chassis/memory/$(basename "$src") -> memory/$(basename "$src")")
        done
    done
    shopt -u nullglob
fi

section "Step 3: re-render customer-side scripts from chassis templates"
RENDER_RC=0
BOOTSTRAP_SCRIPT="$CHASSIS_HOME/chassis/scripts/bootstrap-customer-scripts.sh"
if [[ "$DRY_RUN" == "true" ]]; then
    say "  [dry-run] would run: bash $BOOTSTRAP_SCRIPT --plists"
else
    if ! env BOT_NAME="$BOT_NAME" CUSTOMER_HOME="$CUSTOMER_HOME" CHASSIS_HOME="$CHASSIS_HOME" \
        bash "$BOOTSTRAP_SCRIPT" --plists; then
        RENDER_RC=1
    fi
fi

if [[ $RENDER_RC -ne 0 ]]; then
    say "ERROR: bootstrap-customer-scripts.sh failed"
    say "  Migration partially applied; the moved artifacts are at $CUSTOMER_HOME"
    say "  Inspect, fix the template render, then re-run bootstrap-customer-scripts.sh"
    exit 3
fi

section "Step 4: reload LaunchAgent plists (macOS only)"
if [[ "$SKIP_LAUNCHD" == "true" ]]; then
    say "  skipped (--skip-launchd)"
elif ! command -v launchctl >/dev/null 2>&1; then
    say "  launchctl not in PATH - skipping (likely Linux install)"
else
    USER_UID="${USER_UID:-$(id -u)}"
    PLIST_DIR="$CUSTOMER_HOME/launchd"
    LA_DIR="$HOME/Library/LaunchAgents"

    # Bootout any of the existing per-bot plists so the new ones load clean.
    for legacy in \
        "com.${BOT_NAME}.discord-restart" \
        "com.${BOT_NAME}.discord-watchdog" \
        "com.${BOT_NAME}.heartbeat-dispatcher" \
        "com.behalfbot.${BOT_NAME}-discord-restart" \
        "com.behalfbot.${BOT_NAME}-discord-watchdog" \
        "com.behalfbot.heartbeat-dispatcher"; do
        if launchctl print "gui/${USER_UID}/${legacy}" >/dev/null 2>&1; then
            run launchctl bootout "gui/${USER_UID}/${legacy}" || true
        fi
    done

    # Symlink new plists in and bootstrap them.
    mkdir -p "$LA_DIR"
    for plist in \
        "com.behalfbot.${BOT_NAME}-discord-restart.plist" \
        "com.behalfbot.${BOT_NAME}-discord-watchdog.plist"; do
        src="$PLIST_DIR/$plist"
        dst="$LA_DIR/$plist"
        if [[ ! -f "$src" ]]; then
            say "  WARNING: rendered plist missing at $src - skipping"
            continue
        fi
        run ln -sf "$src" "$dst"
        run launchctl bootstrap "gui/${USER_UID}" "$dst" || true
        say "  loaded $dst"
    done
fi

section "Step 5: write migration sentinel"
if [[ "$DRY_RUN" != "true" ]]; then
    cat > "$STATE_FILE_TARGET" <<EOF
# Behalf.bot customer-state migration sentinel (issue #6).
# Written by chassis/scripts/migrate-customer-state.sh.
# Presence of this file marks the install as already migrated; re-running
# the migration script is a no-op unless this file is removed first.
migrated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
chassis_home: $CHASSIS_HOME
customer_home: $CUSTOMER_HOME
bot_name: $BOT_NAME
EOF
fi

section "Summary"
say "  Moved:"
if [[ ${#MOVED_PATHS[@]} -eq 0 ]]; then
    say "    (nothing - all targets already present, or running dry-run mode)"
else
    for p in "${MOVED_PATHS[@]}"; do
        say "    $p"
    done
fi
say "  Skipped:"
if [[ ${#SKIPPED_PATHS[@]} -eq 0 ]]; then
    say "    (none)"
else
    for p in "${SKIPPED_PATHS[@]}"; do
        say "    $p"
    done
fi

say ""
say "Post-migration follow-ups:"
say "  - Update any host-side LaunchAgents that reference $CHASSIS_HOME/scripts/"
say "    or $CHASSIS_HOME/logs/ paths and aren't covered by the chassis plist set."
say "  - Update any cron entries similarly."
say "  - Update CHASSIS_HOME-aware shell aliases / dotfile snippets to also set"
say "    CUSTOMER_HOME=$CUSTOMER_HOME."
say "  - Verify the discord-watchdog plist fires successfully:"
say "      tail -f $CUSTOMER_HOME/logs/scheduled/watchdog.log"
say "  - Verify the heartbeat-dispatcher (containerized installs: docker compose"
say "    logs chassis; bare-metal: $CUSTOMER_HOME/logs/scheduled/<date>-dispatcher.log)."
say ""

if [[ "$DRY_RUN" == "true" ]]; then
    say "DRY-RUN complete. Nothing was changed."
else
    say "Migration complete."
fi
