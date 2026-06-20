#!/bin/bash
# bootstrap-audit.sh - Post-install verification routine.
#
# Walks the 5 known install gaps (chassis#28) and reports clean / warn / fail
# for each. Designed to run at the END of bootstrap.sh AND on a recurring
# day-7 heartbeat so silent regressions surface.
#
# Gap coverage (cross-ref: project_behalfbot_install_gaps_william_2026_06_20.md,
# project_behalfbot_install_checklist_gaps_2026_06_11.md):
#
#   Gap 1 - HEARTBEATS.md is missing an `s3-backup` row.
#           Discovered on Ben Lakoff's install 2026-06-11. S3 backup runs
#           once at bootstrap then never again, no alert.
#   Gap 2 - `git remote -v` returns no customer-owned URL.
#           Same install. Means customizations only exist on the install
#           machine - no off-machine push target.
#   Gap 3 - `.mcp.json.mcpServers.memory` block is missing OR points at a
#           non-writable MEMORY_FILE_PATH. Surfaced via William Holdeman
#           2026-06-20 amnesiac-bot incident.
#   Gap 4 - macOS LaunchDaemons not loaded. discord-restart + discord-watchdog
#           must be `launchctl print system/com.behalfbot.<name>` OK. Without
#           the Daemons loaded, the tmux session that backs Discord routing
#           is never created.
#   Gap 5 - signup interview depth - NOT covered here (lives in the
#           behalf.bot website signup flow; this audit is install-side).
#
# Exit code: 0 if all checks pass, 1 if any fail. Stdout is human-readable;
# stderr carries fix-suggestion hints. Designed to be re-run after fixes
# without state cleanup.
#
# Usage:
#   bash chassis/scripts/bootstrap-audit.sh [--customer-home PATH] [--bot-name NAME]
#
# Defaults: CUSTOMER_HOME=~/.behalfbot, BOT_NAME from chassis.config.yaml or 'bot'.

set -uo pipefail  # NOT -e: we want to continue auditing after a failed check

CUSTOMER_HOME="${CUSTOMER_HOME:-${HOME}/.behalfbot}"
BOT_NAME="${BOT_NAME:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --customer-home) CUSTOMER_HOME="$2"; shift 2 ;;
        --bot-name) BOT_NAME="$2"; shift 2 ;;
        --help|-h)
            sed -n '1,40p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Bot name resolution order:
#   1. --bot-name flag (already parsed above)
#   2. $BOT_NAME env var
#   3. .env file: BOT_NAME=...
#   4. chassis.config.yaml: identity.assistant.name (lowercased)
#   5. Loaded LaunchDaemon detection: parse the discord-restart label
#   6. Literal 'bot' as last-ditch
resolve_bot_name() {
    [[ -n "$BOT_NAME" ]] && return 0

    if [[ -f "$CUSTOMER_HOME/.env" ]]; then
        local val
        val="$(grep -E '^(export +)?BOT_NAME=' "$CUSTOMER_HOME/.env" 2>/dev/null \
              | head -1 | sed -E 's/^(export +)?BOT_NAME=//; s/^"(.*)"$/\1/; s/^.(.*).$/\1/' \
              | head -c 64)"
        [[ -n "$val" ]] && { BOT_NAME="$val"; return 0; }
    fi

    if [[ -f "$CUSTOMER_HOME/chassis.config.yaml" ]]; then
        # Try yq if installed (clean)
        if command -v yq >/dev/null 2>&1; then
            local v
            v="$(yq '.identity.assistant.name // ""' "$CUSTOMER_HOME/chassis.config.yaml" 2>/dev/null \
                | tr -d '"' | tr '[:upper:]' '[:lower:]')"
            [[ -n "$v" && "$v" != "null" ]] && { BOT_NAME="$v"; return 0; }
        fi
        # Try python+yaml (works if pyyaml installed)
        if command -v python3 >/dev/null 2>&1; then
            local v
            v="$(python3 -c "
import sys
try:
    import yaml
    with open('$CUSTOMER_HOME/chassis.config.yaml') as f:
        c = yaml.safe_load(f) or {}
    n = (c.get('identity', {}).get('assistant', {}).get('name') or '')
    sys.stdout.write(str(n).lower())
except Exception:
    pass
" 2>/dev/null)"
            [[ -n "$v" ]] && { BOT_NAME="$v"; return 0; }
        fi
        # Grep fallback: scrape `name:` line under `assistant:` block. Works for
        # the standard chassis template indentation; gives up cleanly if not.
        local v
        v="$(awk '
            /^  assistant:/ {in_block=1; next}
            in_block && /^    name: / {print $2; exit}
            in_block && /^  [a-z]/ {exit}
        ' "$CUSTOMER_HOME/chassis.config.yaml" 2>/dev/null | tr -d '"' | tr '[:upper:]' '[:lower:]')"
        [[ -n "$v" ]] && { BOT_NAME="$v"; return 0; }
    fi

    # Last resort: find a loaded discord-restart daemon and parse the bot name
    # out of its label. Fragile (only works if step 12 already ran) but useful.
    if command -v launchctl >/dev/null 2>&1; then
        local label
        label="$(launchctl list 2>/dev/null \
                | awk '$3 ~ /^com\.behalfbot\..*-discord-restart$/ {print $3; exit}')"
        if [[ -n "$label" ]]; then
            BOT_NAME="${label#com.behalfbot.}"
            BOT_NAME="${BOT_NAME%-discord-restart}"
            return 0
        fi
    fi

    BOT_NAME="bot"
}
resolve_bot_name

OS="$(uname -s)"
PASS=0
WARN=0
FAIL=0

ok()    { printf '\033[32m  ✓\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
warn()  { printf '\033[33m  ⚠\033[0m %s\n' "$1"; WARN=$((WARN + 1)); }
fail()  { printf '\033[31m  ✗\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
hint()  { printf '    %s\n' "$1" >&2; }
group() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ============================================================
# Gap 1 - HEARTBEATS.md contains s3-backup row
# ============================================================

audit_gap_1_backup_heartbeat() {
    group "Gap 1: HEARTBEATS.md backup row"
    local hb="$CUSTOMER_HOME/HEARTBEATS.md"
    if [[ ! -f "$hb" ]]; then
        fail "HEARTBEATS.md not found at $hb"
        hint "Bootstrap step 7/14 (initialize_heartbeats) appears to have skipped."
        hint "Re-run: CUSTOMER_HOME=$CUSTOMER_HOME bash bootstrap.sh"
        return
    fi
    # Accept any heartbeat whose section header or table row contains "backup"
    # (Sean's install uses siyuan-backup, turso-backup, n8n-backup; Lakoff's
    # was s3-backup; future installs may use restic-backup, b2-backup, etc.)
    if grep -qiE '^(## |\| *)[a-z0-9_-]*backup' "$hb"; then
        local found
        found="$(grep -ioE '^(## |\| *)[a-z0-9_-]*backup[a-z0-9_-]*' "$hb" | sed -E 's/^(## |\| *)//' | sort -u | tr '\n' ' ')"
        ok "backup heartbeat(s) present in HEARTBEATS.md: $found"
    else
        fail "no backup heartbeat in HEARTBEATS.md"
        hint "Append a backup row to $hb. Reference row format:"
        hint "  | s3-backup | daily 04:00 | always | scripts/run-backup.sh | normal |"
    fi
}

# ============================================================
# Gap 2 - git remote points at customer-owned URL
# ============================================================

audit_gap_2_customer_remote() {
    group "Gap 2: customer GitHub remote wired"
    if [[ ! -d "$CUSTOMER_HOME/.git" ]]; then
        warn "$CUSTOMER_HOME is not a git repo - skip (chassis install may use a different layout)"
        return
    fi
    local origin
    origin="$(cd "$CUSTOMER_HOME" && git remote get-url origin 2>/dev/null)"
    if [[ -z "$origin" ]]; then
        fail "no 'origin' remote configured"
        hint "Create a private customer repo and wire it as origin. From $CUSTOMER_HOME:"
        hint "  gh repo create \$USER/behalfbot-\$INSTALLER --private --source=. --remote=origin --push"
        return
    fi
    # 'origin' should be the customer repo, not the chassis upstream. A
    # separate 'chassis' or 'upstream' remote pointing at scrollinondubs/behalfbot
    # is fine and expected (used for subtree pulls).
    if [[ "$origin" == *scrollinondubs/behalfbot.git* ]] || \
       [[ "$origin" == *scrollinondubs/behalfbot* && "$origin" != *new-jaxity* ]]; then
        fail "origin points at the chassis template repo (scrollinondubs/behalfbot)"
        hint "Customer state should NOT push back to chassis. Re-wire origin:"
        hint "  cd $CUSTOMER_HOME && git remote set-url origin <customer-repo-url>"
        return
    fi
    # Strip embedded auth tokens before printing
    local clean
    clean="$(echo "$origin" | sed -E 's|https://[^@]*@|https://|')"
    ok "origin: $clean"
}

# ============================================================
# Gap 3 - .mcp.json has memory server with writable path
# ============================================================

audit_gap_3_memory_mcp() {
    group "Gap 3: memory MCP wired with writable path"
    local mcp="$CUSTOMER_HOME/.mcp.json"
    if [[ ! -f "$mcp" ]]; then
        fail ".mcp.json not found at $mcp"
        hint "Re-run bootstrap step 5/14 (hydrate_mcp_json)."
        return
    fi
    if ! command -v jq >/dev/null 2>&1; then
        warn "jq not installed - skipping deep mcp.json check"
        hint "  brew install jq  (macOS) / apt-get install jq (Linux)"
        return
    fi
    local mem_block
    mem_block="$(jq -e '.mcpServers.memory' "$mcp" 2>/dev/null)"
    if [[ -z "$mem_block" || "$mem_block" == "null" ]]; then
        fail "mcpServers.memory missing from .mcp.json"
        hint "Hydrator should have written this. Re-run:"
        hint "  python3 chassis/scripts/hydrate-mcp-json.py --config $CUSTOMER_HOME/chassis.config.yaml \\"
        hint "    --template chassis/.mcp.json.template --env $CUSTOMER_HOME/.env \\"
        hint "    --output $mcp"
        return
    fi
    local mem_path
    mem_path="$(jq -r '.mcpServers.memory.env.MEMORY_FILE_PATH // empty' "$mcp" 2>/dev/null)"
    if [[ -z "$mem_path" ]]; then
        fail "mcpServers.memory.env.MEMORY_FILE_PATH not set"
        return
    fi
    # Expand ${CHASSIS_HOME} / ${CUSTOMER_HOME} for the writability check
    local expanded="$mem_path"
    expanded="${expanded//\$\{CHASSIS_HOME\}/${CHASSIS_HOME:-${HOME}/behalfbot}}"
    expanded="${expanded//\$\{CUSTOMER_HOME\}/$CUSTOMER_HOME}"
    local parent
    parent="$(dirname "$expanded")"
    if [[ ! -d "$parent" ]]; then
        warn "parent dir does not exist: $parent"
        hint "  mkdir -p \"$parent\""
        return
    fi
    if touch "$expanded" 2>/dev/null; then
        ok "memory MCP wired; MEMORY_FILE_PATH writable: $expanded"
    else
        fail "MEMORY_FILE_PATH not writable: $expanded"
        hint "  chown \$(whoami) \"$expanded\" (or fix parent permissions)"
    fi
}

# ============================================================
# Gap 4 - LaunchDaemons loaded (macOS) / units loaded (Linux)
# ============================================================

audit_gap_4_launchd() {
    group "Gap 4: LaunchDaemon / systemd unit loaded"
    if [[ "$OS" == "Darwin" ]]; then
        local labels=(
            "com.behalfbot.${BOT_NAME}-discord-restart"
            "com.behalfbot.${BOT_NAME}-discord-watchdog"
        )
        for label in "${labels[@]}"; do
            if launchctl print "system/${label}" >/dev/null 2>&1; then
                ok "$label loaded (system domain)"
            elif launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1; then
                warn "$label loaded in gui/\$UID instead of system domain"
                hint "Older install. Promote per chassis#14:"
                hint "  sudo launchctl bootstrap system /Library/LaunchDaemons/${label}.plist"
                hint "  launchctl bootout gui/\$(id -u)/${label}"
            else
                fail "$label NOT loaded"
                hint "From $CUSTOMER_HOME:"
                hint "  sudo cp launchd/${label}.plist /Library/LaunchDaemons/"
                hint "  sudo launchctl bootstrap system /Library/LaunchDaemons/${label}.plist"
            fi
        done
    elif [[ "$OS" == "Linux" ]]; then
        # systemd path - not yet implemented in bootstrap; flag as warn
        warn "Linux systemd audit not implemented yet"
        hint "Verify manually: systemctl --user status behalfbot-discord-restart.service"
    else
        warn "unrecognized OS: $OS - skip"
    fi
}

# ============================================================
# Bonus - tmux session for the bot exists
# ============================================================

audit_tmux_session() {
    group "Bonus: tmux session for the bot"
    if ! command -v tmux >/dev/null 2>&1; then
        warn "tmux not installed - install with 'brew install tmux' (macOS) or 'apt-get install tmux'"
        return
    fi
    local label="${BOT_NAME}-discord"
    if tmux has-session -t "$label" 2>/dev/null; then
        ok "tmux session '$label' is running"
    else
        warn "tmux session '$label' not running"
        hint "Will be created at next discord-restart fire (daily 05:00 + RunAtLoad)."
        hint "To create now: bash $CUSTOMER_HOME/scripts/restart-${BOT_NAME}-discord.sh"
    fi
}

# ============================================================
# Main
# ============================================================

main() {
    printf '\033[1mBootstrap audit\033[0m for %s (bot: %s)\n' "$CUSTOMER_HOME" "$BOT_NAME"

    audit_gap_1_backup_heartbeat
    audit_gap_2_customer_remote
    audit_gap_3_memory_mcp
    audit_gap_4_launchd
    audit_tmux_session

    printf '\n\033[1mSummary:\033[0m %d passed, %d warned, %d failed\n' "$PASS" "$WARN" "$FAIL"
    [[ $FAIL -gt 0 ]] && exit 1 || exit 0
}

main
