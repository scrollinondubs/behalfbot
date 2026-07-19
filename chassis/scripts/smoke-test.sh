#!/bin/bash
# chassis/scripts/smoke-test.sh
# ============================
# Runs chassis-core + per-plugin smoke checks post-install. Called by
# docker/entrypoint.sh smoke-test mode (or directly via shell). Each check
# is independent + idempotent; a single failure does NOT abort subsequent
# checks (so the operator sees the full picture, not the first failure).
#
# Per docs/hydration.md step 11: first-heartbeat success criterion (three
# consecutive clean morning briefings) does NOT start counting until every
# smoke test reports OK.
#
# Required env: sourced from $CHASSIS_HOME/.env at entrypoint time, so the
# script can assume INSTANCE_NAME, BRIEFINGS_WEBHOOK_URL, NOTION_API_TOKEN,
# etc. are exported. Per-check fail-soft if env missing - just reports SKIP.
#
# Usage:
#   docker compose run --rm chassis smoke-test            # all checks
#   docker compose run --rm chassis smoke-test core       # chassis core only
#   docker compose run --rm chassis smoke-test plugin bfl # one plugin
#   docker compose run --rm chassis smoke-test --json     # machine-readable output
#
# Exit codes:
#   0 - all checks PASS or SKIP
#   1 - one or more FAIL
#   2 - bad invocation / missing CHASSIS_HOME

set -uo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set}"
: "${CHASSIS_ROOT:=/app/chassis}"
: "${CHASSIS_PLUGINS_ROOT:=/app/plugins}"

JSON_OUTPUT="false"
SCOPE="all"
PLUGIN_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --json) JSON_OUTPUT="true"; shift ;;
        core)   SCOPE="core"; shift ;;
        plugin) SCOPE="plugin"; PLUGIN_ARG="${2:-}"; shift 2 ;;
        all)    SCOPE="all"; shift ;;
        *)      echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Source .env so checks can see customer-side config
if [[ -f "$CHASSIS_HOME/.env" ]]; then
    set -a; . "$CHASSIS_HOME/.env"; set +a
fi
unset ANTHROPIC_API_KEY || true

declare -a RESULTS=()
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

# ---------- Helpers ---------------------------------------------------------

ts() { printf '%(%H:%M:%S)T' -1; }

record() {
    # record <status> <check-name> <message>
    local status="$1" name="$2" msg="$3"
    RESULTS+=("$status|$name|$msg")
    case "$status" in
        PASS) PASS_COUNT=$((PASS_COUNT + 1)) ;;
        FAIL) FAIL_COUNT=$((FAIL_COUNT + 1)) ;;
        SKIP) SKIP_COUNT=$((SKIP_COUNT + 1)) ;;
    esac
    [[ "$JSON_OUTPUT" == "true" ]] && return
    printf '[%s] %-4s %-30s %s\n' "$(ts)" "$status" "$name" "$msg"
}

# ---------- Chassis core checks --------------------------------------------

check_filesystem_layout() {
    local missing=()
    for d in briefings logs/scheduled scheduled-tasks state data memory plugins temp; do
        [[ -d "$CHASSIS_HOME/$d" ]] || missing+=("$d")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        record FAIL filesystem_layout "missing: ${missing[*]}"
    else
        record PASS filesystem_layout "all expected dirs under \$CHASSIS_HOME present"
    fi
}

check_env_loaded() {
    if [[ -z "${INSTANCE_NAME:-}" ]]; then
        record FAIL env_loaded "INSTANCE_NAME not set - .env hydration likely failed"
    else
        record PASS env_loaded "INSTANCE_NAME=$INSTANCE_NAME"
    fi
}

check_postgres_connectivity() {
    if [[ -z "${CHASSIS_PG_DSN:-}" ]]; then
        record SKIP postgres_connectivity "CHASSIS_PG_DSN not set"
        return
    fi
    if python3.12 -c "
import sys, urllib.parse as up, socket
dsn = '${CHASSIS_PG_DSN}'
p = up.urlparse(dsn)
host = p.hostname or 'postgres'
port = p.port or 5432
s = socket.create_connection((host, port), timeout=3)
s.close()
" 2>/dev/null; then
        record PASS postgres_connectivity "TCP reach to postgres OK"
    else
        record FAIL postgres_connectivity "cannot reach postgres on configured DSN"
    fi
}

check_anthropic_api_key_unset() {
    # CRITICAL per heartbeat-dispatcher.sh lines 67-80: ANTHROPIC_API_KEY must
    # be unset so claude -p falls through to OAuth subscription billing.
    if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
        record FAIL anthropic_api_key_unset "ANTHROPIC_API_KEY is set - this routes claude -p to PAYG instead of OAuth subscription billing. Unset in customer .env."
    else
        record PASS anthropic_api_key_unset "ANTHROPIC_API_KEY correctly unset"
    fi
}

check_claude_cli() {
    if ! command -v claude >/dev/null 2>&1; then
        record FAIL claude_cli "claude CLI not on PATH"
        return
    fi
    local v
    if v=$(claude --version 2>&1 | head -1); then
        record PASS claude_cli "$v"
    else
        record FAIL claude_cli "claude --version failed"
    fi
}

check_heartbeat_dispatcher_script() {
    local script="$CHASSIS_ROOT/scheduled-tasks/heartbeat-dispatcher.sh"
    if [[ ! -x "$script" ]]; then
        record FAIL heartbeat_dispatcher "$script not executable"
        return
    fi
    if /usr/bin/zsh -n "$script" 2>/dev/null; then
        record PASS heartbeat_dispatcher "dispatcher script parses cleanly"
    else
        record FAIL heartbeat_dispatcher "dispatcher script has syntax error"
    fi
}

check_heartbeats_md_present() {
    if [[ ! -f "$CHASSIS_HOME/HEARTBEATS.md" ]]; then
        record FAIL heartbeats_md "HEARTBEATS.md missing - dispatcher will report no heartbeats"
        return
    fi
    local count
    count=$(grep -c '^## ' "$CHASSIS_HOME/HEARTBEATS.md" 2>/dev/null || echo 0)
    if [[ "$count" -gt 0 ]]; then
        record PASS heartbeats_md "HEARTBEATS.md has $count heartbeat section(s)"
    else
        record FAIL heartbeats_md "HEARTBEATS.md present but no ## sections - schema docs only"
    fi
}

check_mcp_json_present() {
    if [[ ! -f "$CHASSIS_HOME/.mcp.json" ]]; then
        record FAIL mcp_json ".mcp.json missing - claude --channels intake will not work"
        return
    fi
    if jq empty "$CHASSIS_HOME/.mcp.json" 2>/dev/null; then
        local servers
        servers=$(jq -r '.mcpServers | keys | join(",")' "$CHASSIS_HOME/.mcp.json" 2>/dev/null || echo "")
        record PASS mcp_json "valid JSON, servers: $servers"
    else
        record FAIL mcp_json ".mcp.json has invalid JSON"
    fi
}

check_briefings_dispatch_helper() {
    if [[ ! -x "$CHASSIS_ROOT/scripts/post-to-channel.sh" ]]; then
        record FAIL briefings_dispatch "$CHASSIS_ROOT/scripts/post-to-channel.sh not executable"
        return
    fi
    record PASS briefings_dispatch "post-to-channel.sh present + executable"
}

check_telegram_intake_helper() {
    # Telegram adapter follow-up - check it exists OR record SKIP for now
    local helper="$CHASSIS_ROOT/scripts/post-to-telegram.sh"
    if [[ -x "$helper" ]]; then
        record PASS telegram_intake "post-to-telegram.sh present"
    else
        record SKIP telegram_intake "post-to-telegram.sh not implemented yet (chassis follow-up)"
    fi
}

check_slack_intake_helper() {
    local helper="$CHASSIS_ROOT/scripts/post-to-slack.sh"
    if [[ -x "$helper" ]]; then
        record PASS slack_intake "post-to-slack.sh present"
    else
        record SKIP slack_intake "post-to-slack.sh not implemented yet (chassis follow-up)"
    fi
}

check_second_brain_backend_reachable() {
    # Dispatches per configured backend - see check-second-brain-backend.py for
    # what each one verifies. Two things changed here 2026-07-19 (Stage 2):
    #
    #   - the check name is `second_brain_read`, not `notion_read`. That is the
    #     name chassis.config.yaml already lists under
    #     success_criteria.smoke_tests, so until now the criterion referenced a
    #     check that never appeared in the output on ANY backend.
    #   - Obsidian and SiYuan get real checks instead of a SKIP.
    #
    # The logic lives in Python rather than inline here because it has to read
    # chassis.config.yaml (bash cannot, without a fourth hand-rolled YAML
    # parser) and because per-backend branches need unit tests.
    local helper="$CHASSIS_ROOT/scripts/check-second-brain-backend.py"
    if [[ ! -f "$helper" ]]; then
        record SKIP second_brain_read "$helper not found"
        return
    fi
    local out status msg
    out=$(python3 "$helper" 2>/dev/null | tail -1)
    if [[ -z "$out" ]]; then
        record FAIL second_brain_read "check-second-brain-backend.py produced no output"
        return
    fi
    status="${out%%|*}"
    msg="${out#*|}"
    case "$status" in
        PASS|FAIL|SKIP) record "$status" second_brain_read "$msg" ;;
        *) record FAIL second_brain_read "unparseable helper output: $out" ;;
    esac
}

check_gmail_attachment_credentials() {
    # Same helper contract as check_second_brain_backend_reachable: one
    # STATUS|message line on stdout, always exit 0.
    #
    # Env only, no IMAP login. This runs at every boot, and repeated failed
    # logins against Google get the account rate-limited and eventually
    # flagged. The failure worth catching here is the half-configured install -
    # hydration pulled the username field and not the password, which reads as
    # configured but cannot authenticate.
    local helper="$CHASSIS_ROOT/scripts/gmail-attachment.py"
    if [[ ! -f "$helper" ]]; then
        record SKIP gmail_attachment_credentials "$helper not found"
        return
    fi
    local out status msg
    out=$(python3 "$helper" check 2>/dev/null | tail -1)
    if [[ -z "$out" ]]; then
        record FAIL gmail_attachment_credentials "gmail-attachment.py check produced no output"
        return
    fi
    status="${out%%|*}"
    msg="${out#*|}"
    case "$status" in
        PASS|FAIL|SKIP) record "$status" gmail_attachment_credentials "$msg" ;;
        *) record FAIL gmail_attachment_credentials "unparseable helper output: $out" ;;
    esac
}

check_discord_webhook_reachable() {
    # Reach the briefings webhook with a HEAD-equivalent (no message sent) to
    # confirm DNS + connectivity. Actual post would be intrusive; skip.
    if [[ -z "${BRIEFINGS_WEBHOOK_URL:-}" ]]; then
        record SKIP discord_webhook "BRIEFINGS_WEBHOOK_URL not set"
        return
    fi
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" -X GET "$BRIEFINGS_WEBHOOK_URL" 2>/dev/null || echo "000")
    case "$code" in
        200|405) record PASS discord_webhook "webhook URL reachable (HTTP $code)" ;;
        *)       record FAIL discord_webhook "webhook URL returned HTTP $code" ;;
    esac
}

run_core_checks() {
    check_filesystem_layout
    check_env_loaded
    check_anthropic_api_key_unset
    check_claude_cli
    check_heartbeat_dispatcher_script
    check_heartbeats_md_present
    check_mcp_json_present
    check_briefings_dispatch_helper
    check_telegram_intake_helper
    check_slack_intake_helper
    check_postgres_connectivity
    check_second_brain_backend_reachable
    check_gmail_attachment_credentials
    check_discord_webhook_reachable
}

# ---------- Per-plugin checks ----------------------------------------------

run_plugin_check() {
    local name="$1"
    local plugin_dir="$CHASSIS_PLUGINS_ROOT/$name"
    if [[ ! -d "$plugin_dir" ]]; then
        record FAIL "plugin_$name" "$plugin_dir not found"
        return
    fi
    local validator="$plugin_dir/validate.sh"
    if [[ -x "$validator" ]]; then
        if CHASSIS_HOME="$CHASSIS_HOME" bash "$validator" >/tmp/plugin-validate-$name.log 2>&1; then
            record PASS "plugin_$name" "validate.sh passed"
        else
            record FAIL "plugin_$name" "validate.sh failed - see /tmp/plugin-validate-$name.log"
        fi
    else
        record SKIP "plugin_$name" "no validate.sh in $plugin_dir"
    fi
}

run_all_plugin_checks() {
    if [[ ! -d "$CHASSIS_PLUGINS_ROOT" ]]; then
        record SKIP plugins_dir "$CHASSIS_PLUGINS_ROOT not found"
        return
    fi
    for plugin_dir in "$CHASSIS_PLUGINS_ROOT"/*; do
        [[ -d "$plugin_dir" ]] || continue
        local name
        name=$(basename "$plugin_dir")
        run_plugin_check "$name"
    done
}

# ---------- Dispatch -------------------------------------------------------

case "$SCOPE" in
    core)
        run_core_checks
        ;;
    plugin)
        if [[ -z "$PLUGIN_ARG" ]]; then
            echo "smoke-test plugin requires a name" >&2
            exit 2
        fi
        run_plugin_check "$PLUGIN_ARG"
        ;;
    all)
        run_core_checks
        run_all_plugin_checks
        ;;
esac

# ---------- Output summary -------------------------------------------------

if [[ "$JSON_OUTPUT" == "true" ]]; then
    {
        printf '{"summary":{"pass":%d,"fail":%d,"skip":%d},"results":[' \
            "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"
        first=1
        for r in "${RESULTS[@]}"; do
            IFS='|' read -r status name msg <<<"$r"
            [[ $first -eq 1 ]] && first=0 || printf ','
            printf '{"status":"%s","name":"%s","message":%s}' \
                "$status" "$name" "$(printf '%s' "$msg" | jq -Rs .)"
        done
        printf ']}\n'
    }
else
    printf '\n'
    printf 'Summary: %d PASS, %d FAIL, %d SKIP\n' "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"
fi

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
exit 0
