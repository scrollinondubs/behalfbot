#!/bin/bash
# bootstrap.sh — chassis install / re-install orchestrator.
#
# Usage:
#   CHASSIS_HOME=/path/to/chassis-tree \
#     CUSTOMER_HOME=/path/to/customer-state \
#     bash bootstrap.sh [--dry-run] [--skip-deps]
#
# As of issue #6 the chassis enforces a hard split between chassis-disposable
# code and customer state:
#
#   CHASSIS_HOME   - chassis tree (chassis/, plugins/, bootstrap.sh, Dockerfile).
#                    Fully disposable: rm -rf && git clone is safe.
#                    Default: $HOME/behalfbot
#   CUSTOMER_HOME  - customer state (.env, CLAUDE.md, HEARTBEATS.md, scripts/,
#                    state/, logs/, briefings/, memory/, data/). NEVER touched
#                    by reinstall. Default: $HOME/.behalfbot
#
# On first-install: this script creates $CUSTOMER_HOME and scaffolds the initial
# subdir tree, then renders the customer-side scripts from chassis templates.
# On re-bootstrap: this script verifies $CUSTOMER_HOME exists and never
# overwrites anything inside it - only chassis-side setup runs.
#
# Idempotent: re-run after a partial install picks up where it left off.
# Every command run gets logged to ${CUSTOMER_HOME}/logs/bootstrap-<date>.log
# so installer #2 (the next case study) can run a near-identical transcript.
#
# Prerequisites (installer's homework, NOT this script's job):
#   - CHASSIS_HOME directory exists, repo cloned (or tarball-shipped) into it
#     Note: if installer has no bot GitHub account yet, tarball-ship instead of git clone:
#       git archive --format=tar.gz --prefix=behalfbot/ origin/main | ssh user@host "tar xz -C ~/"
#     Re-init .git later once the bot account is provisioned (see LESSONS_FROM_V1.md #32).
#   - Linux box (Ubuntu 22.04+ recommended) or macOS (Mac Mini reference)
#   - Tailscale installed + node shared with Sean+${ASSISTANT_NAME} (per docs/installer-homework.md)
#   - SSH key handoff done (per docs/installer-homework.md)
#   - Agent-side accounts provisioned (GitHub, Google Workspace, Discord, Notion, optional Telegram)
#   - Credentials pre-staged in installer's password manager
#
# What this script does (in order):
#   1. Validate environment + tool prerequisites
#   2. Scaffold $CUSTOMER_HOME if missing (first install)
#   3. Hydrate .env from installer's password manager (interactive prompts for paths)
#   4. Render INSTALL_PROFILE.md + chassis.config.yaml validations
#   5. Hydrate .mcp.json from .mcp.json.template + .env values
#   6. Hydrate CLAUDE.md from CLAUDE.md.template + INSTALL_PROFILE.md values
#   7. Initialize HEARTBEATS.md (copy chassis/HEARTBEATS.md.template -> $CUSTOMER_HOME/HEARTBEATS.md, append chassis-defaults + plugin-registered heartbeats)
#   8. Render customer-side scripts (restart/watchdog) from chassis templates
#   9. Activate enabled plugins (per chassis.config.yaml.modules)
#  10. Seed memory entries from INSTALL_PROFILE.md (no fabrication)
#  11. Install OS-level deps (Python 3.12+, Node 20+, ffmpeg, sqlite3, jq, curl, Claude Code)
#  12. Set up launchd plist (macOS) or systemd unit (Linux) for the dispatcher
#  13. Run smoke tests (each plugin's basic functionality)
#  14. Report status — green = install complete, yellow = follow-ups needed
#
# Each step is its own function. Failures within a step exit non-zero
# with a clear message about what to fix + where to re-run from.

set -euo pipefail

# ============================================================
# 1. Setup + validation
# ============================================================

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (export before running; chassis git tree root)}"
# CUSTOMER_HOME defaults to $HOME/.behalfbot per the issue #6 hard-split.
# Pre-issue-#6 installs co-located customer state with CHASSIS_HOME; those
# installs should run chassis/scripts/migrate-customer-state.sh first.
CUSTOMER_HOME="${CUSTOMER_HOME:-$HOME/.behalfbot}"
export CHASSIS_HOME CUSTOMER_HOME

DRY_RUN="${DRY_RUN:-false}"
SKIP_DEPS="${SKIP_DEPS:-false}"

# CLI flag parsing
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true; shift ;;
        --skip-deps)  SKIP_DEPS=true; shift ;;
        -h|--help)
            sed -n '1,50p' "$0" | sed 's/^# *//'
            exit 0 ;;
        *)
            echo "unknown flag: $1" >&2
            exit 2 ;;
    esac
done

DATE=$(date +%Y-%m-%d)
LOG_DIR="$CUSTOMER_HOME/logs"
TRANSCRIPT="$LOG_DIR/bootstrap-$DATE.log"

# Don't create the log dir on dry-run - we promised CUSTOMER_HOME is untouched
# if dry-run is on.
if [[ "$DRY_RUN" != "true" ]]; then
    mkdir -p "$LOG_DIR"
else
    # Re-route the transcript to a tmp location so logging still works.
    TRANSCRIPT="${TMPDIR:-/tmp}/bootstrap-dryrun-$DATE.log"
    : > "$TRANSCRIPT"
fi

log() {
    local msg="[$(date +%H:%M:%S)] $*"
    echo "$msg" | tee -a "$TRANSCRIPT"
}

run() {
    log "RUN: $*"
    if [[ "$DRY_RUN" == "true" ]]; then
        log "  (dry-run, skipped)"
        return 0
    fi
    "$@" 2>&1 | tee -a "$TRANSCRIPT"
}

step() {
    log ""
    log "═══════════════════════════════════════════════════════════"
    log "STEP $1: $2"
    log "═══════════════════════════════════════════════════════════"
}

# ============================================================
# 2. Validate environment + tool prerequisites
# ============================================================

validate_environment() {
    step "1/14" "Validate environment + tool prerequisites"

    if [[ ! -d "$CHASSIS_HOME" ]]; then
        log "FAIL: CHASSIS_HOME ($CHASSIS_HOME) does not exist"
        exit 2
    fi

    if [[ ! -d "$CHASSIS_HOME/chassis" ]]; then
        log "FAIL: $CHASSIS_HOME does not look like a chassis tree (no chassis/ subdir)"
        exit 2
    fi

    local missing=()
    for tool in jq curl git python3; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            missing+=("$tool")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log "FAIL: missing required tools: ${missing[*]}"
        log "Install via your package manager (apt / brew / etc.)"
        exit 2
    fi

    # INSTALL_PROFILE.md + chassis.config.yaml are PER-INSTALL artifacts. They
    # live under CUSTOMER_HOME post-#6; for legacy installs they sit at
    # CHASSIS_HOME. Accept either to make this script work mid-migration.
    local profile_src config_src
    profile_src="$(first_existing "$CUSTOMER_HOME/INSTALL_PROFILE.md" "$CHASSIS_HOME/INSTALL_PROFILE.md")"
    config_src="$(first_existing "$CUSTOMER_HOME/chassis.config.yaml" "$CHASSIS_HOME/chassis.config.yaml")"

    if [[ -z "$profile_src" ]]; then
        log "FAIL: INSTALL_PROFILE.md missing - looked under $CUSTOMER_HOME and $CHASSIS_HOME"
        exit 2
    fi
    if [[ -z "$config_src" ]]; then
        log "FAIL: chassis.config.yaml missing - looked under $CUSTOMER_HOME and $CHASSIS_HOME"
        exit 2
    fi

    log "✓ environment OK"
    log "  CHASSIS_HOME  (chassis tree)    : $CHASSIS_HOME"
    log "  CUSTOMER_HOME (customer state)  : $CUSTOMER_HOME"
    log "  INSTALL_PROFILE.md source       : $profile_src"
    log "  chassis.config.yaml source      : $config_src"
}

# first_existing <path...> - echo the first path arg that exists; empty if none.
first_existing() {
    local p
    for p in "$@"; do
        if [[ -e "$p" ]]; then
            printf '%s' "$p"
            return 0
        fi
    done
    return 0
}

# ============================================================
# 2. Scaffold CUSTOMER_HOME (first install only)
# ============================================================

scaffold_customer_home() {
    step "2/14" "Scaffold CUSTOMER_HOME"

    # Per issue #6: never overwrite anything inside CUSTOMER_HOME on re-bootstrap.
    # Only create the dir structure if it doesn't already exist.
    if [[ -d "$CUSTOMER_HOME" && -f "$CUSTOMER_HOME/.bootstrap-marker" ]]; then
        log "  $CUSTOMER_HOME already scaffolded, skipping (re-bootstrap path)"
        return 0
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        log "  [dry-run] would create $CUSTOMER_HOME and standard subdirs"
        return 0
    fi

    local subdirs=(
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
    for d in "${subdirs[@]}"; do
        mkdir -p "$CUSTOMER_HOME/$d"
    done

    # Marker so re-runs know scaffolding already happened.
    cat > "$CUSTOMER_HOME/.bootstrap-marker" <<EOF
# Behalf.bot CUSTOMER_HOME bootstrap marker (issue #6).
# Presence signals to bootstrap.sh that this customer dir has been initialised.
# Re-bootstrap will NOT overwrite anything else inside CUSTOMER_HOME while this
# marker is present. Delete the marker to force re-scaffold (which still only
# creates absent subdirs - existing customer state is never clobbered).
bootstrapped_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
chassis_home: $CHASSIS_HOME
EOF
    log "  ✓ scaffolded $CUSTOMER_HOME"
}

# ============================================================
# 3. Hydrate .env from installer's password manager
# ============================================================

hydrate_env() {
    step "3/14" "Hydrate .env from password manager"

    local env_file="$CUSTOMER_HOME/.env"
    if [[ -f "$env_file" ]]; then
        log "  $env_file exists; idempotent re-run will skip prompts for already-set values"
    fi

    log "TODO: implement password-manager-specific hydration."
    log ""
    log "Options (installer-chosen at install kickoff):"
    log "  - Bitwarden / Vaultwarden CLI: 'bw get item <name> | jq -r .login.password'"
    log "  - 1Password CLI: 'op item get <name> --format json | jq -r .fields[].value'"
    log "  - Manual: prompt installer to paste each credential"
    log ""
    log "Required credentials (per docs/installer-homework.md):"
    log "  GITHUB_PAT, DISCORD_BOT_TOKEN, OPENAI_API_KEY"
    log "  + if modules.google.gmail / .calendar are on (headless installs need this,"
    log "    Claude's hosted Google connectors never complete over SSH):"
    log "    GMAIL_OAUTH_PATH, GMAIL_CREDENTIALS_PATH, GOOGLE_OAUTH_CREDENTIALS,"
    log "    GOOGLE_CALENDAR_MCP_TOKEN_PATH - paths to the OAuth client + consented"
    log "    tokens staged under \$CUSTOMER_HOME/secrets/google/."
    log "    Consent needs a browser: docs/installer-homework.md section 4."
    log "  + per-plugin: NOTION_API_TOKEN (if siyuan/notion=notion),"
    log "    BRAVE_API_KEY (if research enabled), STRAVA_*, OURA_TOKEN (if BFL),"
    log "    TURSO_* (if turso), etc."
    log ""
    log "  ↳ placeholder until implementation lands per #494 install-day artifact"

    if [[ "$DRY_RUN" != "true" && ! -f "$env_file" ]]; then
        cat > "$env_file" <<EOF
# Hydrated by bootstrap.sh on $DATE — re-run to refresh
# Per chassis security model: this file is gitignored (.gitignore enforces)
# Each line: <KEY>=<value>; chassis scripts source this on launch

# === REQUIRED (every install) ===
# GITHUB_PAT=<from password manager>
# OPENAI_API_KEY=<from password manager>
# DISCORD_BOT_TOKEN=<from password manager>

# === GOOGLE (only if modules.google.gmail / .calendar are true) ===
# Paths, not secrets. The secrets are the files they point at - chmod 600.
# Consent needs a browser: see docs/installer-homework.md section 4.
# GMAIL_OAUTH_PATH=/app/customer/secrets/google/gcp-oauth.keys.json
# GMAIL_CREDENTIALS_PATH=/app/customer/secrets/google/gmail-token.json
# GOOGLE_OAUTH_CREDENTIALS=/app/customer/secrets/google/gcp-oauth.keys.json
# GOOGLE_CALENDAR_MCP_TOKEN_PATH=/app/customer/secrets/google/calendar-token.json

# === PER-PLUGIN (only the ones enabled in chassis.config.yaml) ===
# NOTION_API_TOKEN=...
# BRAVE_API_KEY=...
# STRAVA_ACCESS_TOKEN=...
# STRAVA_REFRESH_TOKEN=...
# OURA_TOKEN=...
# FDC_API_KEY=...
EOF
        log "  ✓ stub $env_file created (TODO: populate from password manager)"
    fi
}

# ============================================================
# 4. Validate INSTALL_PROFILE + chassis.config.yaml
# ============================================================

validate_install_artifacts() {
    step "4/14" "Validate INSTALL_PROFILE + chassis.config.yaml"

    log "TODO: lint INSTALL_PROFILE.md for required sections"
    log "TODO: validate chassis.config.yaml against schema (yq + json-schema OR python jsonschema)"
    log ""
    log "Required INSTALL_PROFILE.md sections (per chassis V2 schema):"
    log "  identity, deployment, surfaces, modules, trust_line, second_brain, memory"
    log ""
    log "Required chassis.config.yaml fields:"
    log "  version, identity.installer_name, deployment.target,"
    log "  surfaces.primary, modules (at least one enabled), guardrails"
    log ""
    log "  ↳ placeholder until schema lands (separate issue)"
}

# ============================================================
# 5-7. Hydrate templates (.mcp.json, CLAUDE.md, HEARTBEATS.md)
# ============================================================

hydrate_mcp_json() {
    step "5/14" "Hydrate .mcp.json from template"

    local template="$CHASSIS_HOME/chassis/.mcp.json.template"
    local hydrator="$CHASSIS_HOME/chassis/scripts/hydrate-mcp-json.py"
    local config="$CUSTOMER_HOME/chassis.config.yaml"
    local env_file="$CUSTOMER_HOME/.env"
    local mcp_file="$CUSTOMER_HOME/.mcp.json"

    if [[ ! -f "$template" ]]; then
        log "  ERROR: $template missing - chassis install incomplete"
        return 1
    fi
    if [[ ! -x "$hydrator" ]]; then
        log "  ERROR: $hydrator missing or not executable"
        return 1
    fi
    if [[ ! -f "$config" ]]; then
        log "  WARN: $config missing - falling back to empty mcpServers"
        log "        Bootstrap should have produced chassis.config.yaml in an"
        log "        earlier step. Investigate before depending on MCP servers."
        if [[ "$DRY_RUN" != "true" ]] && [[ ! -f "$mcp_file" ]]; then
            printf '%s\n' '{"mcpServers": {}}' > "$mcp_file"
        fi
        return 0
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        log "  [dry-run] would run: $hydrator --config $config --template $template --env $env_file --output $mcp_file"
        return 0
    fi

    # Hydrate. The script exits 2 when placeholders are unresolved; that's a
    # warn-but-not-fatal condition (the .mcp.json is still written, with the
    # offending <TOKEN> values left in place for the installer to fill).
    local hydrate_rc=0
    local env_arg=()
    [[ -f "$env_file" ]] && env_arg=(--env "$env_file")
    python3 "$hydrator" --config "$config" --template "$template" \
        "${env_arg[@]}" --output "$mcp_file" 2>&1 | tee -a "$TRANSCRIPT" || hydrate_rc=$?

    case "$hydrate_rc" in
        0)
            log "  ✓ hydrated $mcp_file"
            ;;
        2)
            log "  ⚠ hydrated $mcp_file with unresolved <PLACEHOLDER> tokens"
            log "    Inspect the warnings above and update .env. Re-run bootstrap"
            log "    (or this step) to finalize."
            ;;
        *)
            log "  ERROR: hydrate-mcp-json.py exited $hydrate_rc - $mcp_file may be incomplete"
            return 1
            ;;
    esac
}

hydrate_claude_md() {
    step "6/14" "Hydrate CLAUDE.md from template"

    log "TODO: sed-based template-substitution from chassis/CLAUDE.md.template"
    log "  Read INSTALL_PROFILE.md + chassis.config.yaml for {{PLACEHOLDER}} values:"
    log "    {{INSTANCE_NAME}}, {{INSTALLER_NAME}}, {{INSTALLER_PRIMARY_EMAIL}}"
    log "    {{MACHINE_DESCRIPTION}}, {{INSTALLER_PRIMARY_FOCUS}}"
    log "    {{PRIMARY_CHANNEL}}, {{OPS_CHANNEL}}, {{BRIEFINGS_CHANNEL}}"
    log "    {{SECOND_BRAIN_BACKEND}}, {{INSTALLER_GITHUB_OWNER}}, etc."
    log "  Append per-plugin '## Plugin: <name>' sections from each enabled plugin's CLAUDE-section template"
    log "  Write hydrated CLAUDE.md at \$CUSTOMER_HOME/CLAUDE.md"
}

initialize_heartbeats() {
    step "7/14" "Initialize HEARTBEATS.md"

    local template="$CHASSIS_HOME/chassis/HEARTBEATS.md.template"
    local rendered="$CUSTOMER_HOME/HEARTBEATS.md"

    if [[ ! -f "$template" ]]; then
        log "  ERROR: $template missing - chassis install incomplete"
        return 1
    fi

    if [[ -f "$rendered" ]]; then
        log "  $rendered already exists, skipping template copy"
        log "  (Idempotency: re-run-safe. Delete the file + rerun to reset to template.)"
    else
        if [[ "$DRY_RUN" == "true" ]]; then
            log "  [dry-run] cp $template $rendered"
        else
            cp "$template" "$rendered"
            log "  Copied $template -> $rendered"
        fi
        log "  Installer: add heartbeats to $rendered (NEVER edit $template directly - per anti-pattern #17)"
    fi

    log "TODO: append chassis-default heartbeats:"
    log "  - morning-briefing (daily 08:00, opus, budget 5)"
    log "  - github-issue-triage (every 30m, sonnet, budget 2)"
    log "  - daily-log (daily 23:00, sonnet, budget 1)"
    log "TODO: for each enabled plugin, append its plugin-registered heartbeats:"
    log "  - bfl-ingest (every 15m if bfl enabled)"
    log "  - dating-swipe (10:00 + 14:00 if dating enabled)"
    log "  - bigpoppa-health (every 1h if devops enabled)"
    log "  - etc."
}

# ============================================================
# 8. Activate enabled plugins
# ============================================================

render_customer_scripts() {
    step "8/14" "Render customer-side scripts from chassis templates"

    local renderer="$CHASSIS_HOME/chassis/scripts/bootstrap-customer-scripts.sh"
    if [[ ! -x "$renderer" ]]; then
        log "  WARN: $renderer missing or not executable - skipping script render"
        log "        Without this step, customer-side restart/watchdog scripts"
        log "        will be absent until the install is patched. See issue #6."
        return 0
    fi

    # BOT_NAME resolution lives inside the renderer; if it can't determine one
    # it falls back to literal 'bot'. Installers should set BOT_NAME explicitly
    # in their environment (or in chassis.config.yaml) for a clean render.
    if [[ "$DRY_RUN" == "true" ]]; then
        log "  [dry-run] would run: $renderer --plists"
        return 0
    fi

    if ! CUSTOMER_HOME="$CUSTOMER_HOME" CHASSIS_HOME="$CHASSIS_HOME" \
        bash "$renderer" --plists 2>&1 | tee -a "$TRANSCRIPT"; then
        log "  ERROR: bootstrap-customer-scripts.sh failed - investigate before continuing"
        return 1
    fi
    log "  ✓ customer-side scripts + plists rendered"
}

populate_discord_access() {
    # chassis#5 item 1: auto-populate the Discord plugin's access.json with the
    # install channel ID(s) + principal user_id at install time so the bot can
    # respond in its channels without a manual `/discord:access group add ...`
    # step. Sourced from .env vars (DISCORD_*_CHANNEL_ID + INSTALLER_DISCORD_USER_ID).
    #
    # No-ops cleanly if either INSTALLER_DISCORD_USER_ID or all the channel
    # vars are unset (installer's homework not done yet) - the script prints a
    # warning and returns 0 so bootstrap can continue.
    step "8b/14" "Populate Discord channel access (chassis#5 item 1)"

    local helper="$CHASSIS_HOME/chassis/scripts/bootstrap-discord-access.sh"
    if [[ ! -x "$helper" ]]; then
        log "  WARN: $helper missing or not executable - skipping discord-access bootstrap"
        return 0
    fi

    # Source .env so the channel + user_id vars are in scope.
    if [[ -f "$CUSTOMER_HOME/.env" ]]; then
        # shellcheck disable=SC1091
        set -a
        source "$CUSTOMER_HOME/.env"
        set +a
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        log "  [dry-run] would run: $helper --dry-run"
        bash "$helper" --dry-run 2>&1 | tee -a "$TRANSCRIPT" || true
        return 0
    fi

    if ! bash "$helper" 2>&1 | tee -a "$TRANSCRIPT"; then
        log "  WARN: bootstrap-discord-access.sh exited non-zero (channels may need manual allowlist)"
    fi
}

preflight_bot_identity() {
    # chassis#5 item 3: pre-flight check that the bot's outbound webhook
    # identity (INSTANCE_NAME + per-channel webhook URLs) matches the
    # configured identity.assistant.name in chassis.config.yaml BEFORE the
    # first heartbeat fires. Without this, Toby's first morning briefing
    # posted under the chassis maintainer's stale bot persona ("Captain
    # Hook") instead of Asimov's persona.
    step "8c/14" "Pre-flight: bot identity matches webhooks (chassis#5 item 3)"

    local helper="$CHASSIS_HOME/chassis/scripts/preflight-bot-identity.sh"
    if [[ ! -x "$helper" ]]; then
        log "  WARN: $helper missing or not executable - skipping pre-flight"
        return 0
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        log "  [dry-run] would run: $helper"
        return 0
    fi

    if ! CUSTOMER_HOME="$CUSTOMER_HOME" CHASSIS_HOME="$CHASSIS_HOME" \
        bash "$helper" 2>&1 | tee -a "$TRANSCRIPT"; then
        log ""
        log "  FAIL: bot-identity pre-flight blocked bootstrap completion."
        log "  Resolve the items above (likely: set INSTANCE_NAME in .env,"
        log "  configure the briefings/ops webhook URLs), then re-run bootstrap.sh."
        return 1
    fi
}

activate_plugins() {
    step "9/14" "Activate enabled plugins"

    log "TODO: for each plugin in plugins/ directory:"
    log "  - Read its openclaw.plugin.json"
    log "  - Check chassis.config.yaml.modules.<plugin>.enabled"
    log "  - If enabled: source plugin's activation hook + export its env vars"
    log "  - Write chassis-env.sh that the launchd/systemd unit sources"
    log ""
    log "Per-plugin chassis-env.sh exports (V1-known):"
    log "  whatsapp: CHASSIS_WHATSAPP_SAFE=\$CHASSIS_PLUGINS_ROOT/whatsapp/scripts/wacli-safe.sh"
    log "  bfl:      BFL_ARCHIVE_DIR=<from config>"
    log "  dating:   SOCIAL_CHANNEL_ID=<from config>"
    log "  etc."
    log ""
    log "Plugin trigger merge (per chassis/scripts/merge-plugin-triggers.sh):"
    log "  - Reads contracts.triggers from each enabled plugin's openclaw.plugin.json"
    log "  - Writes merged registry to \$CUSTOMER_HOME/triggers.yaml"
    log "  - Read by chassis/scripts/dispatch-trigger.sh on every inbound message"
    log "  - Re-run any time a plugin is enabled/disabled or its manifest changes"
}

# ============================================================
# 9. Seed memory entries
# ============================================================

seed_memory() {
    step "10/14" "Seed memory entries from INSTALL_PROFILE"

    log "TODO: per docs/memory-seeding.md — generate seed entries from interview signals"
    log ""
    log "Always-on chassis defaults:"
    log "  feedback_never_deceive.md"
    log "  feedback_never_commit_coords.md"
    log "  feedback_humanize_copy.md"
    log "  feedback_no_em_dash.md"
    log ""
    log "Installer-derived (parsed from INSTALL_PROFILE.md):"
    log "  user_<installer>_bio.md"
    log "  user_<installer>_communication_style.md"
    log "  feedback_<installer>_voice.md"
    log "  reference_emergency_contacts.md (if angel-protocol enabled)"
    log ""
    log "Per-plugin seeds: see docs/memory-seeding.md"
    log ""
    log "  ↳ NO FABRICATION — only what installer told us OR observable from existing tooling"
}

# ============================================================
# 10. Install OS deps
# ============================================================

install_os_deps() {
    step "11/14" "Install OS-level dependencies"

    if [[ "$SKIP_DEPS" == "true" ]]; then
        log "  SKIP_DEPS=true; assuming installer pre-installed everything"
        return 0
    fi

    # installer-1 install lesson (#31): prefer uv for Python version management over deadsnakes/pyenv.
    # One-liner: curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12
    # Installs user-locally (~55s), leaves system Python untouched. Run chassis scripts via
    # "uv run --python 3.12 script.py". See LESSONS_FROM_V1.md #31.

    # installer-1 install lesson (#31): apt-install ffmpeg sqlite3 python3-yaml python3-pip up front.
    # Debian 12 base ships without them. PEP 668 on Debian 12 blocks naive "pip install"
    # outside a venv — install python3-yaml via apt to avoid it for validation scripts.

    log "TODO: install if missing:"
    log "  uv (Python version mgr) + uv python install 3.12 — preferred over deadsnakes/pyenv"
    log "  Python 3.12+, Node 20+, ffmpeg, sqlite3, jq, curl"
    log "  python3-yaml, python3-pip (Debian/Ubuntu: via apt to avoid PEP 668 venv block)"
    log "  + per-plugin: ollama (if local-models), docker (if vaultwarden self-host),"
    log "    whisper-cpp (if discord-intake voice), loom-dl (if process-loom)"
    log ""
    log "Detection:"
    log "  if uname is Darwin → use brew"
    log "  if uname is Linux → detect package manager (apt / dnf / pacman)"
    log "  Otherwise prompt installer for manual install"
    log ""
    log "Don't install Claude Code itself — installer's homework includes that."
}

# ============================================================
# 11. Set up launchd / systemd unit
# ============================================================

# migrate_stale_launchdaemons <label>...
#
# Installs made between chassis#14 (2026-06-03) and the gui-agent fix shipped
# these jobs as LaunchDaemons in /Library/LaunchDaemons/. A leftover daemon does
# not just sit there harmlessly: it runs the same restart script on the same
# schedule, from launchd's Background session, and recreates a Background-born
# tmux server. tmux runs one server per user socket, so that single daemon
# re-poisons the server for every session on it and fights the new agent
# forever. Both cannot coexist - the daemon has to go.
#
# Removing it needs sudo, which this installer refuses to take behind your back.
# Interactive runs get the exact commands plus a y/N prompt (sudo itself asks
# for the password). Non-interactive runs fail loudly with the commands to run.
# Override the prompt with BOOTSTRAP_ASSUME_YES=1.
migrate_stale_launchdaemons() {
    local labels=("$@")
    local stale=()

    local label
    for label in "${labels[@]}"; do
        if [[ -f "/Library/LaunchDaemons/${label}.plist" ]]; then
            stale+=("$label")
        fi
    done

    if [[ ${#stale[@]} -eq 0 ]]; then
        return 0
    fi

    log ""
    log "  MIGRATION: found ${#stale[@]} stale LaunchDaemon(s) from a pre-fix install:"
    for label in "${stale[@]}"; do
        log "    /Library/LaunchDaemons/${label}.plist"
    done
    log "  These run the tmux-spawning restart script in launchd's Background"
    log "  session, where the login keychain is unreachable (security error 36)."
    log "  They must be removed before the gui LaunchAgents can take over, or"
    log "  they will keep recreating a keychain-blind tmux server. See"
    log "  docs/launchd-domains.md."
    log ""
    log "  Commands (require sudo):"
    for label in "${stale[@]}"; do
        log "    sudo launchctl bootout system/${label}"
        log "    sudo rm -f /Library/LaunchDaemons/${label}.plist"
    done
    log ""

    if [[ "$DRY_RUN" == "true" ]]; then
        log "  [dry-run] would run the commands above, then install the gui agents"
        return 0
    fi

    if [[ "${BOOTSTRAP_ASSUME_YES:-0}" != "1" ]]; then
        if [[ ! -t 0 ]]; then
            log "  ERROR: stale LaunchDaemons present and this is a non-interactive run."
            log "         Run the commands above (or re-run with BOOTSTRAP_ASSUME_YES=1),"
            log "         then re-run bootstrap.sh. Refusing to leave a daemon and an"
            log "         agent fighting over the same tmux server."
            return 1
        fi
        local reply=""
        read -r -p "  Run them now with sudo? [y/N] " reply
        if [[ ! "$reply" =~ ^[Yy]$ ]]; then
            log "  ERROR: declined. The stale daemons must be removed before the agents"
            log "         can work. Run the commands above, then re-run bootstrap.sh."
            return 1
        fi
    fi

    for label in "${stale[@]}"; do
        log "  Booting out + removing daemon: $label"
        sudo launchctl bootout "system/${label}" >/dev/null 2>&1 || true
        sudo rm -f "/Library/LaunchDaemons/${label}.plist"
        if [[ -f "/Library/LaunchDaemons/${label}.plist" ]]; then
            log "    ERROR: could not remove /Library/LaunchDaemons/${label}.plist"
            return 1
        fi
        log "    ✓ removed $label from the system domain"
    done

    return 0
}

setup_dispatcher_unit() {
    step "12/14" "Set up dispatcher launchd / systemd unit"

    local os
    os="$(uname -s)"

    if [[ "$os" == "Darwin" ]]; then
        # Activating the restart agent fires it immediately (RunAtLoad), and its
        # first act on an unmigrated box is `tmux kill-server` - the only way to
        # rebuild a Background-born tmux server under Aqua. If this installer is
        # itself running inside tmux, that kill takes the bootstrap down with it.
        if [[ -n "${TMUX:-}" && "$DRY_RUN" != "true" && "${BOOTSTRAP_ALLOW_TMUX:-0}" != "1" ]]; then
            log "  ERROR: bootstrap.sh is running inside tmux."
            log "         Activating the discord-restart agent rebuilds the tmux server"
            log "         (kill-server), which would kill this shell mid-install."
            log "         Re-run outside tmux, or set BOOTSTRAP_ALLOW_TMUX=1 if you"
            log "         know this session is expendable."
            return 1
        fi
        # On macOS the chassis ships discord-restart + discord-watchdog as
        # gui-domain LaunchAgents in ~/Library/LaunchAgents/. They're rendered
        # into $CUSTOMER_HOME/launchd/ during step 8 by
        # bootstrap-customer-scripts.sh; this step symlinks them into
        # ~/Library/LaunchAgents/ and bootstraps gui/$(id -u). No sudo.
        #
        # They must NOT be LaunchDaemons. chassis#14 promoted them on the false
        # premise that they "only docker exec the chassis container" - in fact
        # they spawn a host tmux session running `claude`, and a LaunchDaemon
        # runs in launchd's Background session, which cannot reach the user's
        # login keychain. That broke every Vaultwarden-sourced credential on
        # every macOS install from 2026-06-03 to 2026-07-11. Decision rule and
        # the auto-login tradeoff: docs/launchd-domains.md.
        #
        # heartbeat-dispatcher is DEPRECATED in containerized installs
        # (docker-compose handles cadence) so we skip it by default. Set
        # BOOTSTRAP_DISPATCHER_LEGACY_PLIST=1 to opt in for bare-metal layouts.

        local agents=(
            "com.behalfbot.${BOT_NAME:-bot}-discord-restart"
            "com.behalfbot.${BOT_NAME:-bot}-discord-watchdog"
        )
        if [[ "${BOOTSTRAP_DISPATCHER_LEGACY_PLIST:-0}" == "1" ]]; then
            agents+=("com.behalfbot.heartbeat-dispatcher")
        fi

        migrate_stale_launchdaemons "${agents[@]}" || return 1

        local uid la_dir installed=0
        uid="$(id -u)"
        la_dir="$HOME/Library/LaunchAgents"

        for label in "${agents[@]}"; do
            local src="$CUSTOMER_HOME/launchd/${label}.plist"
            local dst="$la_dir/${label}.plist"
            if [[ ! -f "$src" ]]; then
                log "  WARN: $src not rendered - skip $label (step 8 may have failed)"
                continue
            fi
            if [[ "$DRY_RUN" == "true" ]]; then
                log "  [dry-run] would: ln -sf $src $dst"
                log "  [dry-run] would: launchctl bootout gui/$uid/$label (if loaded)"
                log "  [dry-run] would: launchctl bootstrap gui/$uid $dst"
                continue
            fi
            installed=1
            log "  Installing LaunchAgent: $label"
            mkdir -p "$la_dir"
            ln -sf "$src" "$dst"
            # Bootout first so re-runs replace cleanly; ignore stderr when not loaded.
            launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
            if launchctl bootstrap "gui/$uid" "$dst"; then
                log "    ✓ $label loaded into gui/$uid"
            else
                log "    ERROR: bootstrap failed for $label (see launchctl output above)"
                log "    If you are on ssh with no GUI login, gui/$uid does not exist."
                log "    Log into the Mac's desktop (or enable auto-login) and re-run."
                return 1
            fi
        done

        if [[ "$installed" == "0" && "$DRY_RUN" != "true" ]]; then
            log "  no agents to install (nothing rendered in $CUSTOMER_HOME/launchd/)"
        fi
        return 0
    fi

    if [[ "$os" == "Linux" ]]; then
        log "TODO: Linux systemd unit emission not yet implemented"
        log "  - write /etc/systemd/system/behalfbot-heartbeat-dispatcher.{service,timer}"
        log "    (system-scope, NOT --user, per lesson #26)"
        log "  - service ExecStart sources chassis-env.sh + invokes dispatcher.sh"
        # installer-1 install lesson (#35): systemd strips PATH — uv and user-local binaries won't resolve
        # without an explicit Environment= line. Embed:
        #   Environment=PATH=/home/<user>/.local/bin:/usr/local/bin:/usr/bin:/bin
        # in the .service file. Without it every heartbeat invoking "uv run" silently fails.
        log "  - embed Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"
        log "    in the .service file - systemd strips PATH, uv run silently fails without it (#35)"
        log "  - timer OnCalendar=*:0/15 (every 15 min)"
        log "  - Restart=on-failure; ConditionPathExists=\$CUSTOMER_HOME"
        return 0
    fi

    log "  WARN: unsupported OS '$os' - skip unit setup"
}

# ============================================================
# 12. Smoke tests
# ============================================================

run_bootstrap_audit() {
    # Post-install audit. Runs the 5-gap verification routine and surfaces
    # any silent regression before declaring the install complete.
    local auditor="$CHASSIS_HOME/chassis/scripts/bootstrap-audit.sh"
    if [[ ! -x "$auditor" ]]; then
        log "  WARN: $auditor missing - skipping post-install audit"
        return 0
    fi
    if [[ "$DRY_RUN" == "true" ]]; then
        log "  [dry-run] would run: CUSTOMER_HOME=$CUSTOMER_HOME BOT_NAME=${BOT_NAME:-bot} bash $auditor"
        return 0
    fi
    # Audit script exits non-zero on failures; surface but don't abort the
    # bootstrap (the installer should see the report and fix, then re-run).
    CUSTOMER_HOME="$CUSTOMER_HOME" BOT_NAME="${BOT_NAME:-bot}" \
        bash "$auditor" 2>&1 | tee -a "$TRANSCRIPT" || true
}

run_smoke_tests() {
    step "13/14" "Run smoke tests + post-install audit"

    run_bootstrap_audit

    log ""
    log "TODO: per-plugin smoke checks:"
    log "  - chassis-core: dispatcher fires once + dry-runs every registered heartbeat"
    log "  - github MCP: gh auth status returns agent-side identity"
    log "  - second-brain: SiYuan/Notion API returns workspace metadata"
    log "  - discord (plugin): bot responds to a test ping"
    log "  - briefing: md-to-briefing-html.py renders a sample markdown without error"
    log "  - guardrails: hook fires + correctly blocks rm -rf / + correctly allows allowlisted hosts"
    log "  - per-plugin: each enabled plugin runs its own smoke check"
    log ""
    log "Failures here block install completion. Better to catch a misconfigured"
    log "plugin than ship it and wonder why heartbeats don't fire."
}

# ============================================================
# 13. Report status
# ============================================================

report_status() {
    step "14/14" "Install summary"

    log ""
    log "Bootstrap transcript: $TRANSCRIPT"
    log ""
    log "First-heartbeat success criterion (per <v1-reference-install> #494):"
    log "  Daily-briefing message lands in the installer's briefings channel"
    log "  for 3 consecutive mornings, built from a stubbed gather of recent activity."
    log ""
    log "Until that 3-day clean run completes, leave Sean+${ASSISTANT_NAME} SSH access in place."
    log "Per the V1 ownership-transfer pattern, transfer happens AFTER the soak."
}

# ============================================================
# Main
# ============================================================

main() {
    log "Behalf.bot bootstrap starting"
    log "  CHASSIS_HOME (chassis tree)   : $CHASSIS_HOME"
    log "  CUSTOMER_HOME (customer state): $CUSTOMER_HOME"
    log "  Dry-run: $DRY_RUN | Skip-deps: $SKIP_DEPS"
    log "  Transcript: $TRANSCRIPT"

    validate_environment
    scaffold_customer_home
    hydrate_env
    validate_install_artifacts
    hydrate_mcp_json
    hydrate_claude_md
    initialize_heartbeats
    render_customer_scripts
    populate_discord_access
    preflight_bot_identity
    activate_plugins
    seed_memory
    install_os_deps
    setup_dispatcher_unit
    run_smoke_tests
    report_status

    log ""
    log "Bootstrap complete (skeleton; see TODOs above for implementation gaps)."
    log "Re-run with --dry-run to preview steps without execution."
}

main "$@"
