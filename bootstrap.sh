#!/bin/bash
# bootstrap.sh — chassis install / re-install orchestrator.
#
# Usage:
#   CHASSIS_HOME=/path/to/chassis bash bootstrap.sh [--dry-run] [--skip-deps]
#
# Idempotent: re-run after a partial install picks up where it left off.
# Every command run gets logged to ${CHASSIS_HOME}/logs/bootstrap-<date>.log
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
#   2. Hydrate .env from installer's password manager (interactive prompts for paths)
#   3. Render INSTALL_PROFILE.md + chassis.config.yaml validations
#   4. Hydrate .mcp.json from .mcp.json.template + .env values
#   5. Hydrate CLAUDE.md from CLAUDE.md.template + INSTALL_PROFILE.md values
#   6. Initialize HEARTBEATS.md (copy chassis/HEARTBEATS.md.template -> $CHASSIS_HOME/HEARTBEATS.md, append chassis-defaults + plugin-registered heartbeats)
#   7. Activate enabled plugins (per chassis.config.yaml.modules)
#   8. Seed memory entries from INSTALL_PROFILE.md (no fabrication)
#   9. Install OS-level deps (Python 3.12+, Node 20+, ffmpeg, sqlite3, jq, curl, Claude Code)
#  10. Set up launchd plist (macOS) or systemd unit (Linux) for the dispatcher
#  11. Run smoke tests (each plugin's basic functionality)
#  12. Report status — green = install complete, yellow = follow-ups needed
#
# Each step is its own function. Failures within a step exit non-zero
# with a clear message about what to fix + where to re-run from.

set -euo pipefail

# ============================================================
# 1. Setup + validation
# ============================================================

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (export before running)}"

DRY_RUN="${DRY_RUN:-false}"
SKIP_DEPS="${SKIP_DEPS:-false}"
DATE=$(date +%Y-%m-%d)
LOG_DIR="$CHASSIS_HOME/logs"
TRANSCRIPT="$LOG_DIR/bootstrap-$DATE.log"

mkdir -p "$LOG_DIR"

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
    step "1/12" "Validate environment + tool prerequisites"

    if [[ ! -d "$CHASSIS_HOME" ]]; then
        log "FAIL: CHASSIS_HOME ($CHASSIS_HOME) does not exist"
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

    if [[ ! -f "$CHASSIS_HOME/INSTALL_PROFILE.md" ]]; then
        log "FAIL: $CHASSIS_HOME/INSTALL_PROFILE.md missing — needed for hydration"
        exit 2
    fi
    if [[ ! -f "$CHASSIS_HOME/chassis.config.yaml" ]]; then
        log "FAIL: $CHASSIS_HOME/chassis.config.yaml missing — needed for hydration"
        exit 2
    fi

    log "✓ environment OK"
}

# ============================================================
# 3. Hydrate .env from installer's password manager
# ============================================================

hydrate_env() {
    step "2/12" "Hydrate .env from password manager"

    local env_file="$CHASSIS_HOME/.env"
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
    log "  GITHUB_PAT, GOOGLE_OAUTH_*, DISCORD_BOT_TOKEN, OPENAI_API_KEY"
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
    step "3/12" "Validate INSTALL_PROFILE + chassis.config.yaml"

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
    step "4/12" "Hydrate .mcp.json from template"

    log "TODO: jq-based template-substitution from .mcp.json.template"
    log "  - Read chassis.config.yaml.modules.* flags"
    log "  - Filter .mcp.json.template entries by _enable_when matching active modules"
    log "  - Substitute <PLACEHOLDER> values from .env"
    log "  - Write hydrated .mcp.json (gitignored)"
    log ""
    log "Reference: docs/mcp-setup.md"
}

hydrate_claude_md() {
    step "5/12" "Hydrate CLAUDE.md from template"

    log "TODO: sed-based template-substitution from chassis/CLAUDE.md.template"
    log "  Read INSTALL_PROFILE.md + chassis.config.yaml for {{PLACEHOLDER}} values:"
    log "    {{INSTANCE_NAME}}, {{INSTALLER_NAME}}, {{INSTALLER_PRIMARY_EMAIL}}"
    log "    {{MACHINE_DESCRIPTION}}, {{INSTALLER_PRIMARY_FOCUS}}"
    log "    {{PRIMARY_CHANNEL}}, {{OPS_CHANNEL}}, {{BRIEFINGS_CHANNEL}}"
    log "    {{SECOND_BRAIN_BACKEND}}, {{INSTALLER_GITHUB_OWNER}}, etc."
    log "  Append per-plugin '## Plugin: <name>' sections from each enabled plugin's CLAUDE-section template"
    log "  Write hydrated CLAUDE.md at \$CHASSIS_HOME/CLAUDE.md"
}

initialize_heartbeats() {
    step "6/12" "Initialize HEARTBEATS.md"

    local template="$CHASSIS_HOME/chassis/HEARTBEATS.md.template"
    local rendered="$CHASSIS_HOME/HEARTBEATS.md"

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

activate_plugins() {
    step "7/12" "Activate enabled plugins"

    log "TODO: for each plugin in plugins/ directory:"
    log "  - Read its openclaw.plugin.json"
    log "  - Check chassis.config.yaml.modules.<plugin>.enabled"
    log "  - If enabled: source plugin's activation hook + export its env vars"
    log "  - Write chassis-env.sh that the launchd/systemd unit sources"
    log ""
    log "Per-plugin chassis-env.sh exports (V1-known):"
    log "  whatsapp: CHASSIS_WHATSAPP_SAFE=\$CHASSIS_HOME/plugins/whatsapp/scripts/wacli-safe.sh"
    log "  bfl:      BFL_ARCHIVE_DIR=<from config>"
    log "  dating:   SOCIAL_CHANNEL_ID=<from config>"
    log "  etc."
    log ""
    log "Plugin trigger merge (per chassis/scripts/merge-plugin-triggers.sh):"
    log "  - Reads contracts.triggers from each enabled plugin's openclaw.plugin.json"
    log "  - Writes merged registry to \$CHASSIS_HOME/chassis/triggers.yaml"
    log "  - Read by chassis/scripts/dispatch-trigger.sh on every inbound message"
    log "  - Re-run any time a plugin is enabled/disabled or its manifest changes"
}

# ============================================================
# 9. Seed memory entries
# ============================================================

seed_memory() {
    step "8/12" "Seed memory entries from INSTALL_PROFILE"

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
    step "9/12" "Install OS-level dependencies"

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

setup_dispatcher_unit() {
    step "10/12" "Set up dispatcher launchd / systemd unit"

    log "TODO: detect OS (macOS vs Linux) + emit appropriate unit:"
    log ""
    log "macOS — write /Library/LaunchDaemons/com.behalfbot.heartbeat-dispatcher.plist"
    log "  (LaunchDaemons survive reboots per lesson #26; LaunchAgents need GUI session)"
    log "  UserName=<installer>"
    log "  StartInterval=900 (every 15 min)"
    log "  EnvironmentVariables: CHASSIS_HOME, INSTANCE_NAME, plus chassis-env.sh sourcing"
    log "  ProgramArguments: bash -c 'source \$CHASSIS_HOME/chassis-env.sh && exec \$CHASSIS_HOME/chassis/scheduled-tasks/heartbeat-dispatcher.sh'"
    log "  StandardOutPath / StandardErrorPath: \$CHASSIS_HOME/logs/scheduled/launchd-stdout.log"
    log ""
    log "Linux — write /etc/systemd/system/behalfbot-heartbeat-dispatcher.{service,timer}"
    log "  (system-scope, NOT --user, per lesson #26)"
    log "  service ExecStart sources chassis-env.sh + invokes dispatcher.sh"
    # installer-1 install lesson (#35): systemd strips PATH — uv and user-local binaries won't resolve
    # without an explicit Environment= line. Embed:
    #   Environment=PATH=/home/<user>/.local/bin:/usr/local/bin:/usr/bin:/bin
    # in the .service file. Without it every heartbeat invoking "uv run" silently fails.
    log "  IMPORTANT: embed Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"
    log "    in the .service file — systemd strips PATH, uv run silently fails without it (#35)"
    log "  timer OnCalendar=*:0/15 (every 15 min)"
    log "  Restart=on-failure; ConditionPathExists=\$CHASSIS_HOME"
    log ""
    log "Both: load + enable + start; verify with first-tick log entry"
}

# ============================================================
# 12. Smoke tests
# ============================================================

run_smoke_tests() {
    step "11/12" "Run smoke tests"

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
    step "12/12" "Install summary"

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
    log "Behalf.bot bootstrap starting — CHASSIS_HOME=$CHASSIS_HOME"
    log "Dry-run: $DRY_RUN | Skip-deps: $SKIP_DEPS"
    log "Transcript: $TRANSCRIPT"

    validate_environment
    hydrate_env
    validate_install_artifacts
    hydrate_mcp_json
    hydrate_claude_md
    initialize_heartbeats
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
