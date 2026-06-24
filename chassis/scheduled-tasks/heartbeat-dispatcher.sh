#!/bin/zsh
# Behalf.bot Heartbeat Dispatcher
#
# Parses HEARTBEATS.md, checks schedules, evaluates conditions, invokes Claude
# when work exists. Invoked by launchd (macOS) or systemd (Linux) on a fixed
# tick (default every 15 minutes) via com.behalfbot.heartbeat-dispatcher.plist
# or behalfbot-heartbeat-dispatcher.service.
#
# Architecture (gather-first dispatcher):
#   - Dispatcher fires on fixed cadence regardless of whether there is work.
#   - For each registered heartbeat: check schedule → run cheap gather script
#     → evaluate condition → only invoke `claude -p` when condition is true.
#   - 96 dispatcher runs/day → ~4 actual model invocations. Pay-to-play only
#     on real work. See LESSONS_FROM_V1.md #7 + #20.
#
# Required environment (set by the install runbook, NOT this script):
#   CHASSIS_HOME    — absolute path to the installer's chassis directory
#                     (e.g. /home/installer/behalfbot or /Users/sean/<v1-reference-install>)
#   HOME            — installer's home directory (for tool paths, .ssh, etc.)
#   PATH            — set up by the launchd plist / systemd unit; this script
#                     does not assume any particular Homebrew prefix.
#
# Optional environment (graceful degradation when unset):
#   OLLAMA_URL              — local Ollama for ask_model conditions. If
#                             unreachable the dispatcher fails open (always
#                             fires Claude on ask_model conditions).
#   DISCORD_WEBHOOK_URL     — for #installer notifications.
#   DISCORD_OPS_WEBHOOK_URL — for output-validator quarantine alerts.
#   ANTHROPIC_API_KEY       — see comment block below — INTENTIONALLY UNSET
#                             at top of this script so `claude -p` uses OAuth
#                             (subscription billing) not PAYG.
#
# Lessons baked in:
#   #7  gather-first dispatcher
#   #11 heartbeat must be registered in HEARTBEATS.md to fire
#   #13 destructive-read state shared across heartbeats causes races
#   #20 cheap no-op gates short-circuit before any paid API call
#   #24 trigger conditions matter more than query logic when debugging
#   #26 LaunchDaemons survive reboot; LaunchAgents pause without GUI session

set -euo pipefail

# Issue #6 customer-state split: customer-side state lives under CUSTOMER_HOME,
# chassis code under CHASSIS_HOME. For legacy installs both vars point at the
# same dir (the pre-#6 layout). New / post-migration installs separate them.
# Prefer CUSTOMER_HOME for state/log/heartbeat paths; fall back to CHASSIS_HOME
# for backward compat so existing containerized installs (where both are
# /app/customer) continue working unchanged.
: "${CHASSIS_HOME:?CHASSIS_HOME must be exported before running this dispatcher (set by launchd plist / systemd unit)}"
: "${CUSTOMER_HOME:=$CHASSIS_HOME}"
export CHASSIS_HOME CUSTOMER_HOME

# Cross-platform timeout binary. macOS-Homebrew ships gnu coreutils as
# `gtimeout`; Debian (and the chassis Linux container) ships it as `timeout`.
# Resolve once at script start so the deep claude-invocation paths below stay
# readable. Falls back to bare `timeout` so the script errors loudly with
# "command not found" if neither resolves, rather than failing silently. The
# image bakes coreutils' `timeout` at /usr/bin/timeout; on macOS Homebrew
# installs it at /opt/homebrew/bin/gtimeout. Prior versions hardcoded the
# macOS path and broke every claude invocation inside the container.
TIMEOUT_CMD="$(command -v gtimeout 2>/dev/null || command -v timeout 2>/dev/null || echo timeout)"

HEARTBEATS_FILE="$CUSTOMER_HOME/HEARTBEATS.md"
STATE_FILE="$CUSTOMER_HOME/scheduled-tasks/heartbeat-state.json"
CONSERVATION_FILE="$CUSTOMER_HOME/scheduled-tasks/conservation-mode.json"
LOCK_FILE="$CUSTOMER_HOME/logs/scheduled/dispatcher.lock"
LOG_DIR="$CUSTOMER_HOME/logs/scheduled"
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/${DATE}-dispatcher.log"
OLLAMA_MODEL="${OLLAMA_MODEL:-gemma2}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

DRY_RUN="${DRY_RUN:-false}"

mkdir -p "$LOG_DIR"

# Source env for tokens needed by gather scripts. Prefer .env.baked when
# present — host-side `scripts/bake-env.sh` expands the VW hydration block
# into literal KEY=VALUE pairs there. The raw .env has a hydration call
# that fails silently inside the container (no Keychain / bw-unlock auth),
# leaving every VW-backed secret unset. .env.baked has the literals.
if [[ -f "$CUSTOMER_HOME/.env.baked" ]]; then
    source "$CUSTOMER_HOME/.env.baked"
elif [[ -f "$CUSTOMER_HOME/.env" ]]; then
    source "$CUSTOMER_HOME/.env"
fi

# CRITICAL: unset ANTHROPIC_API_KEY so `claude -p` invocations fall through
# to OAuth (subscription-billed) instead of API key (PAYG → auto-recharge).
#
# When ANTHROPIC_API_KEY is set, `claude -p` uses it and bills PAYG. When
# unset, it falls back to OAuth stored in ~/.claude/.credentials.json or the
# OS keychain → subscription billing.
#
# In the V1 reference install (Sean's `$CHASSIS_HOME/`), the .env file's Vaultwarden
# hydration block exported ANTHROPIC_API_KEY for legitimate non-Claude-Code
# uses (OpenAI fallback shims, etc.). That silently routed every heartbeat
# through PAYG and caused a measurable cost spike — confirmed root cause of
# an Anthropic auto-recharge incident.
#
# Scripts that GENUINELY need PAYG-via-API-key (OpenAI fallback, any
# non-Claude-Code direct API call) must hydrate the key explicitly inside
# the script via a Vaultwarden fetch wrapper. This unset is intentional +
# load-bearing; do NOT remove without auditing every `claude -p` call site.
unset ANTHROPIC_API_KEY

# --- Customer-specific hooks (optional) ---
#
# Sourced AFTER .env + ANTHROPIC_API_KEY unset, BEFORE the dispatcher loop
# starts firing heartbeats. Lets installers layer install-specific
# extensions on top of the canonical dispatcher without forking it.
#
# Common uses (from the V1 reference install):
#   - Plugin-specific recovery logic (e.g. dating-plugin emulator boot
#     detection that rewinds a heartbeat's last_fired when ADB recovers).
#   - Branded Discord notification overrides (per-install bot persona).
#   - Pre-tick instrumentation hooks (latency probes, cost guards).
#
# Define functions or override variables here. The file is source'd, not
# exec'd, so anything it sets stays in the dispatcher's environment for
# the rest of the run. Safe to leave absent — chassis canonical behavior
# applies when the file doesn't exist.
HOOKS_FILE="$CUSTOMER_HOME/scheduled-tasks/dispatcher-hooks.sh"
if [[ -f "$HOOKS_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$HOOKS_FILE"
fi

# --- Logging ---

log() {
    echo "[$(date +%H:%M:%S)] $1" >> "$LOG_FILE"
}

# --- Conservation Mode ---

is_conservation_mode() {
    if [[ ! -f "$CONSERVATION_FILE" ]]; then
        return 1
    fi
    local enabled
    enabled=$(jq -r '.enabled // false' "$CONSERVATION_FILE")
    if [[ "$enabled" != "true" ]]; then
        return 1
    fi

    # Check auto-lift: if auto_lift_after is set and we're past it, disable
    local auto_lift
    auto_lift=$(jq -r '.auto_lift_after // ""' "$CONSERVATION_FILE")
    if [[ -n "$auto_lift" && "$auto_lift" != "null" ]]; then
        local lift_epoch
        lift_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$auto_lift" +%s 2>/dev/null || echo 0)
        local now_epoch=$(date +%s)
        if [[ $now_epoch -ge $lift_epoch ]]; then
            log "CONSERVATION — auto-lift triggered (past $auto_lift), disabling"
            # Disable conservation mode
            cat > "$CONSERVATION_FILE" << EOJSON
{
  "enabled": false,
  "enabled_at": null,
  "enabled_by": null,
  "auto_lift_after": null,
  "reason": null
}
EOJSON
            return 1
        fi
    fi

    return 0
}

# --- Locking ---

acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        local pid
        pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            log "LOCKED — dispatcher already running (pid $pid), exiting"
            exit 0
        fi
        log "STALE LOCK — removing (pid $pid no longer running)"
        rm -f "$LOCK_FILE"
    fi
    echo $$ > "$LOCK_FILE"
}

release_lock() {
    rm -f "$LOCK_FILE"
}
trap release_lock EXIT

# --- State Management ---

init_state() {
    if [[ ! -f "$STATE_FILE" ]]; then
        echo '{}' > "$STATE_FILE"
    fi
}

get_state() {
    local name="$1" field="$2"
    jq -r --arg n "$name" --arg f "$field" '.[$n][$f] // ""' "$STATE_FILE"
}

set_state() {
    local name="$1" field="$2" value="$3"
    local tmp="${STATE_FILE}.tmp"
    jq --arg n "$name" --arg f "$field" --arg v "$value" \
        '.[$n] = (.[$n] // {}) | .[$n][$f] = $v' "$STATE_FILE" > "$tmp" \
        && mv "$tmp" "$STATE_FILE"
}

increment_fire_count() {
    local name="$1"
    local tmp="${STATE_FILE}.tmp"
    jq --arg n "$name" \
        '.[$n] = (.[$n] // {}) | .[$n].fire_count = ((.[$n].fire_count // "0") | tonumber + 1 | tostring)' \
        "$STATE_FILE" > "$tmp" \
        && mv "$tmp" "$STATE_FILE"
}

# --- Ollama ---

ensure_ollama() {
    if curl -sf "$OLLAMA_URL/api/tags" > /dev/null 2>&1; then
        return 0
    fi
    log "Ollama not responding, attempting start..."
    # Resolve ollama via PATH so the same script works on macOS-Homebrew
    # (/opt/homebrew/bin/ollama), Linux container (image bakes it at
    # /usr/local/bin/ollama), and arbitrary installer setups. Fall back to
    # bare `ollama` so the error surfaces loudly if it's truly missing.
    local ollama_bin
    ollama_bin="$(command -v ollama 2>/dev/null || echo ollama)"
    "$ollama_bin" serve &>/dev/null &
    local i
    for i in 1 2 3 4 5; do
        sleep 2
        if curl -sf "$OLLAMA_URL/api/tags" > /dev/null 2>&1; then
            log "Ollama started successfully"
            return 0
        fi
    done
    log "ERROR: Ollama failed to start after 10s"
    return 1
}

ask_model() {
    local prompt="$1"
    local result
    # Use HTTP API for clean output (no thinking mode artifacts)
    result=$(timeout 60 curl -sf "$OLLAMA_URL/api/generate" \
        -d "$(jq -n --arg model "$OLLAMA_MODEL" --arg prompt "$prompt" \
            '{model: $model, prompt: $prompt, stream: false}')" \
        2>/dev/null | jq -r '.response // empty') || {
        log "ERROR: Ollama timed out or failed"
        echo "YES"  # fail-open
        return
    }
    if [[ -z "$result" ]]; then
        log "ERROR: Ollama returned empty response"
        echo "YES"  # fail-open
        return
    fi
    echo "$result"
}

# --- Schedule Matching ---

schedule_matches() {
    local schedule="$1" name="$2"
    local now_epoch=$(date +%s)
    local now_hour=$(date +%H | sed 's/^0//')
    local now_min=$(date +%M | sed 's/^0//')
    local now_dow=$(date +%A | tr '[:upper:]' '[:lower:]')

    if [[ "$schedule" == every\ * ]]; then
        # Interval: "every 15m" or "every 1h"
        local interval_str="${schedule#every }"
        local interval_seconds=0
        if [[ "$interval_str" == *m ]]; then
            interval_seconds=$(( ${interval_str%m} * 60 ))
        elif [[ "$interval_str" == *h ]]; then
            interval_seconds=$(( ${interval_str%h} * 3600 ))
        fi

        local last_checked
        last_checked=$(get_state "$name" "last_checked")
        if [[ -z "$last_checked" ]]; then
            return 0  # never checked, run now
        fi

        local last_epoch
        last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S" "$last_checked" +%s 2>/dev/null || echo 0)
        local delta=$(( now_epoch - last_epoch ))
        [[ $delta -ge $interval_seconds ]]
        return $?

    elif [[ "$schedule" == daily\ * ]]; then
        # Daily: "daily 08:03" or "daily 12:00 Europe/Lisbon" (timezone suffix
        # optional). awk strips the trailing TZ token before the time-component
        # split. Without the strip, `cut -d: -f2 | sed 's/^0//'` returns
        # "0 Europe/Lisbon" which kills the arithmetic comparison below under
        # bash 5 / zsh strict-mode (`[[ N -ge '0 Europe/Lis...' ]]` → "bad
        # math expression"). The bug stays latent on macOS bash 3.2 which is
        # more lenient about non-numeric tokens in `[[ ]]` math context, then
        # surfaces in the dockerized chassis container on Debian.
        #
        # Note: the TZ suffix is currently documentation, not behavior — the
        # script uses local system clock. If/when chassis grows multi-TZ
        # installs we add proper `TZ=` env-var handling here.
        local time_str=$(echo "${schedule#daily }" | awk '{print $1}')
        local sched_hour=$(echo "$time_str" | cut -d: -f1 | sed 's/^0//')
        local sched_min=$(echo "$time_str" | cut -d: -f2 | sed 's/^0//')

        # Already fired today?
        local last_fired
        last_fired=$(get_state "$name" "last_fired")
        if [[ -n "$last_fired" && "$last_fired" == ${DATE}* ]]; then
            return 1  # already ran today
        fi

        # Apply jitter if configured (deterministic per day+name)
        local jitter_str
        jitter_str=$(get_config_field "$name" "jitter")
        if [[ -n "$jitter_str" ]]; then
            local jitter_minutes=0
            if [[ "$jitter_str" == *m ]]; then
                jitter_minutes=${jitter_str%m}
            elif [[ "$jitter_str" == *h ]]; then
                jitter_minutes=$(( ${jitter_str%h} * 60 ))
            fi
            if [[ $jitter_minutes -gt 0 ]]; then
                # Deterministic random offset: hash(date + name) mod jitter_minutes.
                # md5 (BSD/macOS) and md5sum (Debian/container) print different
                # output formats — md5sum prefixes the digest with the byte
                # count, so we always read the first field. Without this, the
                # dispatcher emits `command not found: md5` on every tick when
                # running inside the chassis container. See <v1-reference-install>#698.
                local seed_hash md5_bin
                md5_bin=$(command -v md5sum 2>/dev/null || command -v md5 2>/dev/null || echo md5)
                seed_hash=$(echo -n "${DATE}${name}" | "$md5_bin" | awk '{print $1}')
                local offset_min=$(( 16#${seed_hash:0:8} % jitter_minutes ))
                # Add offset to scheduled time
                local total_min=$(( sched_hour * 60 + sched_min + offset_min ))
                sched_hour=$(( total_min / 60 ))
                sched_min=$(( total_min % 60 ))
            fi
        fi

        # Is it past the scheduled time? (within today)
        if [[ $now_hour -gt $sched_hour ]] || \
           [[ $now_hour -eq $sched_hour && $now_min -ge $sched_min ]]; then
            return 0
        fi
        return 1

    elif [[ "$schedule" == weekly\ * ]]; then
        # Weekly: "weekly sunday 18:00"
        local rest="${schedule#weekly }"
        local sched_dow=$(echo "$rest" | awk '{print $1}')
        local time_str=$(echo "$rest" | awk '{print $2}')
        local sched_hour=$(echo "$time_str" | cut -d: -f1 | sed 's/^0//')
        local sched_min=$(echo "$time_str" | cut -d: -f2 | sed 's/^0//')

        # Wrong day of week?
        if [[ "$now_dow" != "$sched_dow" ]]; then
            return 1
        fi

        # Already fired this week? Check if last_fired is today
        local last_fired
        last_fired=$(get_state "$name" "last_fired")
        if [[ -n "$last_fired" && "$last_fired" == ${DATE}* ]]; then
            return 1
        fi

        # Past the scheduled time?
        if [[ $now_hour -gt $sched_hour ]] || \
           [[ $now_hour -eq $sched_hour && $now_min -ge $sched_min ]]; then
            return 0
        fi
        return 1
    fi

    log "WARN: unknown schedule format: $schedule"
    return 1
}

# --- Condition Evaluation ---

evaluate_condition() {
    local condition="$1" gathered_data="$2" name="$3"

    if [[ "$condition" == "always" ]]; then
        echo "YES — scheduled"
        return 0
    fi

    if [[ "$condition" == threshold\ * ]]; then
        # "threshold count > 0" — count = length of JSON array
        local rest="${condition#threshold }"
        local field=$(echo "$rest" | awk '{print $1}')
        local op=$(echo "$rest" | awk '{print $2}')
        local target=$(echo "$rest" | awk '{print $3}')

        local actual=0
        # Try JSON array length, then JSON object field. Reject non-JSON:
        # the old wc -l fallback treated any single-line output (including
        # `count=0`) as actual=1, which fired briefings every 10 min when
        # a gather script used key=value instead of JSON (PR #206 incident).
        if echo "$gathered_data" | jq -e 'type == "array"' &>/dev/null; then
            actual=$(echo "$gathered_data" | jq 'length')
        elif echo "$gathered_data" | jq -e 'type == "object"' &>/dev/null; then
            actual=$(echo "$gathered_data" | jq --arg f "$field" '.[$f] // 0')
        else
            log "WARN $name — gather output is not JSON, treating as count=0 (first line: $(echo "$gathered_data" | head -1))"
            actual=0
        fi

        local result=false
        case "$op" in
            ">")  [[ $actual -gt $target ]] && result=true ;;
            ">=") [[ $actual -ge $target ]] && result=true ;;
            "<")  [[ $actual -lt $target ]] && result=true ;;
            "=")  [[ $actual -eq $target ]] && result=true ;;
        esac

        if [[ "$result" == "true" ]]; then
            echo "YES — $field=$actual (${op} ${target})"
            return 0
        else
            echo "NO — $field=$actual (not ${op} ${target})"
            return 1
        fi
    fi

    if [[ "$condition" == "ask_model" ]]; then
        if ! ensure_ollama; then
            log "FAIL-OPEN: Ollama down, firing Claude for $name"
            echo "YES — fail-open (Ollama unavailable)"
            return 0
        fi

        local condition_prompt
        condition_prompt=$(get_config_field "$name" "condition_prompt")

        local model_prompt="You are a task dispatcher. Given the data below, answer the question.
Reply with exactly YES or NO on the first line, followed by a one-sentence reason.

QUESTION: ${condition_prompt}

DATA:
${gathered_data}"

        local response
        response=$(ask_model "$model_prompt")
        local first_line
        first_line=$(echo "$response" | head -1 | tr '[:lower:]' '[:upper:]')

        if [[ "$first_line" == YES* ]]; then
            echo "YES — model: $(echo "$response" | head -1)"
            return 0
        else
            echo "NO — model: $(echo "$response" | head -1)"
            return 1
        fi
    fi

    log "WARN: unknown condition type: $condition"
    return 1
}

# --- HEARTBEATS.md Parser ---

# Extract a field value from a heartbeat's YAML block (skips HTML-commented sections)
get_config_field() {
    local name="$1" field="$2"
    awk -v name="$name" -v field="$field" '
        /^<!--/ { commenting = 1 }
        /-->/ { commenting = 0; next }
        commenting { next }
        /^## / { current = $2 }
        current == name && /^```yaml/ { in_block = 1; next }
        current == name && /^```/ && in_block { in_block = 0 }
        in_block && $0 ~ "^" field ":" {
            sub("^" field ": *", ""); print; exit
        }
    ' "$HEARTBEATS_FILE"
}

# Extract a multiline field (for gather scripts, skips HTML-commented sections)
get_config_multiline() {
    local name="$1" field="$2"
    awk -v name="$name" -v field="$field" '
        /^<!--/ { commenting = 1 }
        /-->/ { commenting = 0; next }
        commenting { next }
        /^## / { current = $2 }
        current == name && /^```yaml/ { in_block = 1; next }
        current == name && /^```/ && in_block { in_block = 0 }
        in_block && $0 ~ "^" field ": " {
            # Single-line value
            sub("^" field ": *", ""); print; exit
        }
        in_block && $0 ~ "^" field ": *\\|" {
            # Multiline block scalar
            capturing = 1; next
        }
        capturing && /^[a-z_]/ { capturing = 0 }
        capturing { print }
    ' "$HEARTBEATS_FILE"
}

# List all heartbeat names (skips HTML-commented sections)
list_heartbeats() {
    awk '
        /^<!--/ { commenting = 1 }
        /-->/ { commenting = 0; next }
        !commenting && /^## / { sub(/^## /, ""); print }
    ' "$HEARTBEATS_FILE"
}

# --- Claude Invocation ---

invoke_claude() {
    local claude_input="$1" output_file="$2" model="$3" budget="$4" heartbeat_name="${5:-unknown}" cwd="${6:-$CUSTOMER_HOME}"
    # Timeout: 20 minutes per invocation (dating/briefing sessions need time for ADB/Playwright)
    # cwd: optional working directory for claude — heartbeats can scope to a sub-context
    # like dating-context/ which has its own narrow CLAUDE.md. Defaults to $CUSTOMER_HOME
    # (where the per-install CLAUDE.md and .mcp.json live, post issue #6).
    local telemetry_dir="$CUSTOMER_HOME/logs/telemetry"
    local telemetry_file="$telemetry_dir/$DATE-usage.jsonl"
    local tmp_json="$LOG_DIR/.claude-out-$$.json"
    local start_ts exit_code end_ts wall_secs ts_iso

    mkdir -p "$telemetry_dir"
    start_ts=$(date +%s)

    # chassis#5 item 6: guard against a missing .mcp.json. If the customer file
    # is absent (post-migration, fresh install before bootstrap finished, etc.),
    # passing --mcp-config <missing-path> crashes `claude -p` immediately and
    # the heartbeat fails silently. Drop the flag when the file isn't there;
    # `claude -p` falls back to its default MCP config search (~/.claude/...).
    # bootstrap.sh writes an empty-{} .mcp.json so the file should exist on a
    # clean install - this is defense in depth for the partial-restore case.
    local mcp_config_path="$CUSTOMER_HOME/.mcp.json"
    local mcp_flag=""
    if [[ -f "$mcp_config_path" ]]; then
        mcp_flag="--mcp-config $mcp_config_path"
    else
        log "WARN $heartbeat_name - $mcp_config_path missing, invoking claude without --mcp-config"
    fi

    $TIMEOUT_CMD 1200 /bin/zsh -c '
        cd "$7" && echo "$1" | claude -p \
            --dangerously-skip-permissions \
            --model "$2" \
            ${=3} \
            --max-budget-usd "$4" \
            --output-format json \
            > "$5" 2>> "$6"
    ' -- "$claude_input" "$model" "$mcp_flag" "$budget" "$tmp_json" "$LOG_FILE" "$cwd"
    exit_code=$?

    end_ts=$(date +%s)
    wall_secs=$((end_ts - start_ts))
    ts_iso=$(date +%Y-%m-%dT%H:%M:%S)

    if [[ -f "$tmp_json" && -s "$tmp_json" ]]; then
        # Extract result text to the actual output file
        if jq -e '.result' "$tmp_json" > /dev/null 2>&1; then
            jq -r '.result' "$tmp_json" > "$output_file"
        else
            cp "$tmp_json" "$output_file"
        fi

        # Append telemetry entry
        jq -c \
            --arg name "$heartbeat_name" \
            --arg model "$model" \
            --arg ts "$ts_iso" \
            --argjson wall "$wall_secs" \
            --argjson exit_code "$exit_code" \
            '{
                ts: $ts,
                heartbeat: $name,
                model: $model,
                cost_usd: (.cost_usd // .total_cost_usd // 0),
                input_tokens: (.usage.input_tokens // 0),
                output_tokens: (.usage.output_tokens // 0),
                cache_read_tokens: (.usage.cache_read_input_tokens // 0),
                cache_create_tokens: (.usage.cache_creation_input_tokens // 0),
                wall_seconds: $wall,
                exit_code: $exit_code
            }' "$tmp_json" >> "$telemetry_file" 2>> "$LOG_FILE" \
            && log "TELEMETRY $heartbeat_name — cost logged" \
            || log "WARN: telemetry parse failed for $heartbeat_name"

        rm -f "$tmp_json"
    else
        # Failed invocation — log zero-cost entry so gaps are visible
        printf '%s\n' "{\"ts\":\"$ts_iso\",\"heartbeat\":\"$heartbeat_name\",\"model\":\"$model\",\"cost_usd\":0,\"input_tokens\":0,\"output_tokens\":0,\"cache_read_tokens\":0,\"cache_create_tokens\":0,\"wall_seconds\":$wall_secs,\"exit_code\":$exit_code,\"error\":\"no_json_output\"}" >> "$telemetry_file"
        rm -f "$tmp_json"
    fi

    return $exit_code
}

# --- Discord Notifications ---

send_discord_notification() {
    local heartbeat_name="$1" summary="$2" output_file="$3"

    if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
        log "WARN: DISCORD_WEBHOOK_URL not set, skipping notification"
        return
    fi

    # INSTANCE_NAME defaults to "Behalf.bot" but installers commonly set it
    # to the installer's name (e.g. "${ASSISTANT_NAME}", "Marc-bot") for personality.
    local payload
    payload=$(jq -n \
        --arg name "$heartbeat_name" \
        --arg summary "$summary" \
        --arg file "$(basename "$output_file")" \
        --arg instance "${INSTANCE_NAME:-Behalf.bot}" \
        '{
            content: ("🤖 **" + $instance + " | " + $name + "**\n" + $summary + "\n> " + $file)
        }')

    curl -sf -X POST "$DISCORD_WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d "$payload" >> "$LOG_FILE" 2>&1 \
        || log "WARN: Discord notification failed for $heartbeat_name"
}

check_and_notify() {
    local name="$1" output_file="$2"

    # Look for structured signal in output: notify: true / summary: ...
    if ! head -20 "$output_file" | grep -q '^notify: *true'; then
        return
    fi

    local summary
    summary=$(head -20 "$output_file" | grep '^summary: *' | sed 's/^summary: *//' | head -1)
    if [[ -z "$summary" ]]; then
        summary="Heartbeat completed with actions taken."
    fi

    log "NOTIFY $name — $summary"
    send_discord_notification "$name" "$summary" "$output_file"
}

# --- Post-Output Validator (Five Failure Modes check) ---
#
# After a successful claude -p invocation for artifact-producing heartbeats
# (those with output_validator: true in HEARTBEATS.md), run a haiku-powered
# Five Failure Modes check against the output before it's "published."
#
# On pass: artifact stays, normal flow continues.
# On fail: artifact is quarantined (renamed .quarantined), the ops webhook
# (DISCORD_OPS_WEBHOOK_URL) gets an alert.
#
# Validator runs as an async haiku call, NOT blocking the main invocation.
# If haiku itself fails, fail-open (artifact ships). Monitor cost; rollback
# if it jumps materially.

VALIDATOR_PROMPT='You are a code and content quality gate. Given the artifact text below, run the Five Failure Modes check and emit a single JSON object on stdout (no other text):

{"pass": true, "mode": null, "reason": null}
or
{"pass": false, "mode": "<one of: action_hallucination|assertion_correctness|hallucinated_actions|scope_creep|cascading_errors|context_loss|tool_misuse|drift_symptoms>", "reason": "<one sentence>"}

Five Failure Modes to check:
1. Hallucinated actions — tool calls or writes referencing values that were not verified against reality (invented file paths, env vars, IDs)
2. Scope creep — artifact modifies things outside the stated change boundary
3. Cascading errors — a workaround that papers over a root error and creates a deeper one
4. Context loss — re-asking established questions, contradicting earlier decisions, forgetting completed steps
5. Tool misuse — wrong tool, wrong parameters, or ignoring tool output (e.g. truncating a file that already had correct content)

If the artifact is a briefing or summary, also check: does it contain placeholder text, truncation markers ("..."), or summary-of-a-summary patterns that suggest the agent summarised its own prior output rather than producing the original artifact?

Fail on the most severe mode if multiple fire. Pass only if none fire.

---
ARTIFACT:
'

run_output_validator() {
    local name="$1" output_file="$2"

    # Read validator-opt-in flag from HEARTBEATS.md
    local validate_flag
    validate_flag=$(get_config_field "$name" "output_validator")
    if [[ "$validate_flag" != "true" ]]; then
        return 0
    fi

    log "VALIDATOR $name — running Five Failure Modes check on output"

    local artifact_content
    artifact_content=$(cat "$output_file" 2>/dev/null || echo "")
    if [[ -z "$artifact_content" ]]; then
        log "VALIDATOR $name — output file empty, skipping"
        return 0
    fi

    # Truncate artifact to 4000 chars to keep haiku cost bounded
    local truncated_content
    truncated_content=$(echo "$artifact_content" | head -c 4000)
    local validator_input="${VALIDATOR_PROMPT}${truncated_content}"

    local validator_out="$LOG_DIR/.validator-out-$$.json"
    local validator_result

    # Mirror chassis#5 item 6 mcp-config absence guard from invoke_claude. If
    # the customer .mcp.json is missing, drop the flag rather than crashing the
    # validator subprocess.
    local mcp_config_path="$CUSTOMER_HOME/.mcp.json"
    local mcp_flag=""
    if [[ -f "$mcp_config_path" ]]; then
        mcp_flag="--mcp-config $mcp_config_path"
    fi

    # Run haiku validator; fail-open if it errors
    $TIMEOUT_CMD 120 /bin/zsh -c '
        echo "$1" | claude -p \
            --dangerously-skip-permissions \
            --model haiku \
            ${=2} \
            --max-budget-usd 0.05 \
            --output-format json \
            > "$3" 2>> "$4"
    ' -- "$validator_input" "$mcp_flag" "$validator_out" "$LOG_FILE" || {
        log "VALIDATOR $name — haiku call failed or timed out, fail-open"
        rm -f "$validator_out"
        return 0
    }

    # Log validator cost to telemetry as a separate entry
    if [[ -f "$validator_out" && -s "$validator_out" ]]; then
        local ts_iso cost_usd
        ts_iso=$(date +%Y-%m-%dT%H:%M:%S)
        cost_usd=$(jq -r '.cost_usd // .total_cost_usd // 0' "$validator_out" 2>/dev/null || echo 0)
        printf '%s\n' "{\"ts\":\"$ts_iso\",\"heartbeat\":\"${name}-validator\",\"model\":\"haiku\",\"cost_usd\":$cost_usd,\"input_tokens\":0,\"output_tokens\":0,\"cache_read_tokens\":0,\"cache_create_tokens\":0,\"wall_seconds\":0,\"exit_code\":0}" \
            >> "$CUSTOMER_HOME/logs/telemetry/$DATE-usage.jsonl" 2>> "$LOG_FILE" || true

        validator_result=$(jq -r '.result // ""' "$validator_out" 2>/dev/null || echo "")
    fi
    rm -f "$validator_out"

    if [[ -z "$validator_result" ]]; then
        log "VALIDATOR $name — could not parse result, fail-open"
        return 0
    fi

    # Extract pass/mode/reason from the JSON the validator emitted
    local pass_val mode_val reason_val
    pass_val=$(echo "$validator_result" | jq -r '.pass // true' 2>/dev/null || echo "true")
    mode_val=$(echo "$validator_result" | jq -r '.mode // "unknown"' 2>/dev/null || echo "unknown")
    reason_val=$(echo "$validator_result" | jq -r '.reason // ""' 2>/dev/null || echo "")

    if [[ "$pass_val" == "true" ]]; then
        log "VALIDATOR $name — PASS"
        return 0
    fi

    # FAIL: quarantine the artifact, alert the ops webhook
    local quarantine_file="${output_file}.quarantined"
    mv "$output_file" "$quarantine_file" 2>> "$LOG_FILE" || {
        log "VALIDATOR $name — FAIL but could not quarantine artifact (mv failed)"
        return 1
    }

    log "VALIDATOR $name — FAIL mode=$mode_val reason=$reason_val — artifact quarantined at $quarantine_file"

    local ops_webhook="${DISCORD_OPS_WEBHOOK_URL:-${DISCORD_WEBHOOK_URL:-}}"
    if [[ -n "$ops_webhook" ]]; then
        local alert_msg="**Five Failure Modes validator blocked ${name}** — mode: \`${mode_val}\`\n${reason_val}\nArtifact at: \`${quarantine_file}\`"
        curl -sf -X POST "$ops_webhook" \
            -H "Content-Type: application/json" \
            -d "$(jq -n --arg content "$alert_msg" '{content: $content}')" \
            >> "$LOG_FILE" 2>&1 || true
    fi

    return 1
}

# --- Plugin Recovery Hooks ---
#
# Plugins (dating, bfl, etc.) can register recovery hooks here that run on
# every dispatcher tick to detect external-state transitions and rewind
# heartbeat schedules accordingly. Reference implementation lives in
# `plugins/dating/scripts/recovery-hook.sh` (Android emulator boot detection
# → rewinds dating-inbox-check after >=2h downtime). Chassis core does not
# ship any recovery hooks; install-time activation is per-plugin.
#
# Source any installer-installed recovery hook scripts here.
for _hook in "$CUSTOMER_HOME/scheduled-tasks/recovery-hooks.d/"*.sh(N); do
    # shellcheck disable=SC1090
    source "$_hook"
done
unset _hook

# --- Main Dispatch Loop ---

main() {
    acquire_lock
    init_state
    log "=== Dispatcher run started ==="

    # macOS only: wake the display briefly so the Keychain unlocks before
    # any gather script tries to read Vaultwarden secrets that depend on it.
    # After macOS sleep the Keychain can stay locked for the first second
    # or two of wake, causing silent hydration failures. `caffeinate` is
    # macOS-only; the `|| true` lets this no-op cleanly on Linux installers.
    caffeinate -u -t 5 >/dev/null 2>&1 || true

    # Pre-loop checks. Plugins source recovery hooks via the loop above
    # (see "Plugin Recovery Hooks" section). Each hook is responsible for
    # adjusting state that the loop reads (e.g. force-fire after detecting
    # an external-state recovery).
    for _hook_fn in $(typeset +f | grep -E '^chassis_recovery_'); do
        "$_hook_fn" || log "WARN: recovery hook $_hook_fn returned non-zero"
    done
    unset _hook_fn

    # Check conservation mode once at the start of each run
    local conservation_active=false
    if is_conservation_mode; then
        conservation_active=true
        local cons_reason
        cons_reason=$(jq -r '.reason // "unspecified"' "$CONSERVATION_FILE")
        log "CONSERVATION MODE ACTIVE — reason: $cons_reason — skipping normal/background heartbeats"
    fi

    local heartbeats
    heartbeats=($(list_heartbeats))

    if [[ ${#heartbeats[@]} -eq 0 ]]; then
        log "No heartbeats found in $HEARTBEATS_FILE"
        return
    fi

    for name in "${heartbeats[@]}"; do
        local schedule="" condition="" prompt_file="" model="" budget="" cwd="" criticality=""

        schedule=$(get_config_field "$name" "schedule")
        condition=$(get_config_field "$name" "condition")
        prompt_file=$(get_config_field "$name" "prompt")
        model=$(get_config_field "$name" "model")
        budget=$(get_config_field "$name" "budget")
        cwd=$(get_config_field "$name" "cwd")
        criticality=$(get_config_field "$name" "criticality")

        # Explicit "disabled" convention: a heartbeat can be intentionally
        # parked by setting `schedule: disabled` in its yaml block. This is
        # the recommended pattern when a heartbeat needs to stay catalogued
        # in HEARTBEATS.md (docs, rationale, prior config preserved) but
        # must not dispatch. Distinguishes intentional from broken config —
        # the previous behavior was a noisy "missing required fields" SKIP
        # that looked like a real error. (<v1-reference-install>#700 / 2026-05-30 cleanup.)
        if [[ "$schedule" == "disabled" ]]; then
            log "DISABLED $name — schedule:disabled (intentional)"
            continue
        fi

        if [[ -z "$schedule" || -z "$condition" || -z "$prompt_file" ]]; then
            log "SKIP $name — missing required fields (schedule/condition/prompt)"
            continue
        fi

        # Default model, budget, cwd, and criticality
        model=${model:-opus}
        budget=${budget:-5}
        cwd=${cwd:-$CUSTOMER_HOME}
        criticality=${criticality:-normal}

        # Conservation mode: skip non-critical heartbeats
        if [[ "$conservation_active" == "true" && "$criticality" != "critical" ]]; then
            log "SKIP $name — conservation mode (criticality=$criticality)"
            continue
        fi

        # Check schedule
        if ! schedule_matches "$schedule" "$name"; then
            log "SKIP $name — not scheduled"
            continue
        fi

        log "CHECK $name — schedule matched"

        # Gather data (if gather command specified)
        local gather_cmd="" gathered_data=""
        gather_cmd=$(get_config_multiline "$name" "gather")
        if [[ -n "$gather_cmd" ]]; then
            log "GATHER $name — running: $gather_cmd"
            # Gather scripts execute with cwd=$CUSTOMER_HOME (state files,
            # briefings, logs all live there). Scripts that need the chassis
            # tree (chassis/scripts/...) reference $CHASSIS_HOME explicitly.
            gathered_data=$(cd "$CUSTOMER_HOME" && eval "$gather_cmd" 2>> "$LOG_FILE") || {
                log "ERROR $name — gather script failed"
                set_state "$name" "last_checked" "$(date +%Y-%m-%dT%H:%M:%S)"
                set_state "$name" "last_result" "gather_failed"
                continue
            }
        fi

        set_state "$name" "last_checked" "$(date +%Y-%m-%dT%H:%M:%S)"

        # Evaluate condition
        local decision
        decision=$(evaluate_condition "$condition" "$gathered_data" "$name") && should_fire=true || should_fire=false

        set_state "$name" "last_decision" "$decision"
        log "DECISION $name — $decision"

        if [[ "$should_fire" != "true" ]]; then
            log "PASS $name — no work"
            # Clear any stale `last_result` from a prior failure. Without this,
            # a heartbeat that failed once (gather_failed,
            # claude_failed_after_retry, circuit_open, prompt_missing,
            # validator_blocked) keeps that stale label indefinitely once it
            # recovers — operators reading heartbeat-state.json believe the
            # heartbeat is still broken when dispatcher.log shows clean PASSes.
            # Burned us during 2026-05-30 #698 triage: 5 heartbeats appeared
            # broken in state but were green in logs. Writing `success` on a
            # clean PASS is mildly semantically loose (no FIRE happened) but
            # matches the existing "success" semantics elsewhere in this loop
            # and is the minimal-change fix Sean approved 2026-05-30.
            set_state "$name" "last_result" "success"
            continue
        fi

        # Fire Claude
        # prompt_file paths in HEARTBEATS.md may reference either chassis-side
        # prompts (chassis/scheduled-tasks/*-prompt.md) or customer-side
        # prompts (scheduled-tasks/*.md). Try CHASSIS_HOME first since most
        # canonical prompts ship from chassis; fall back to CUSTOMER_HOME for
        # per-install custom prompts.
        local full_prompt_path
        if [[ -f "$CHASSIS_HOME/$prompt_file" ]]; then
            full_prompt_path="$CHASSIS_HOME/$prompt_file"
        elif [[ -f "$CUSTOMER_HOME/$prompt_file" ]]; then
            full_prompt_path="$CUSTOMER_HOME/$prompt_file"
        else
            full_prompt_path="$CHASSIS_HOME/$prompt_file"
        fi
        if [[ ! -f "$full_prompt_path" ]]; then
            log "ERROR $name — prompt file not found: $full_prompt_path"
            set_state "$name" "last_result" "prompt_missing"
            continue
        fi

        # Circuit-breaker: if a heartbeat has been failing claude -p calls
        # repeatedly, skip the FIRE for a (exponentially-growing) cooldown
        # window. Without this, ONE broken auth state (e.g. stale
        # ANTHROPIC_API_KEY in container env) can block the entire
        # dispatcher cycle for hours: each claude -p call eats up to 20min
        # internal timeout + 20s wait + 20min retry = 40+ minutes per
        # failing FIRE. N failing heartbeats × 40min serializes the
        # dispatcher. See scrollinondubs/behalfbot#103 for the
        # 2026-05-22 outage that prompted this.
        local circuit_open_until=""
        circuit_open_until=$(get_state "$name" "circuit_open_until")
        if [[ -n "$circuit_open_until" ]]; then
            local now_epoch_for_circuit
            now_epoch_for_circuit=$(date +%s)
            if [[ $now_epoch_for_circuit -lt $circuit_open_until ]]; then
                local remaining_min=$(( (circuit_open_until - now_epoch_for_circuit) / 60 ))
                log "CIRCUIT-OPEN $name — claude has been failing; skipping FIRE for ${remaining_min}min more"
                set_state "$name" "last_result" "circuit_open"
                continue
            fi
            # Cooldown elapsed; clear the gate (claude_fail_streak persists so
            # one transient success doesn't fully reset — that happens only
            # on actual claude success path below).
            set_state "$name" "circuit_open_until" ""
        fi

        log "FIRE $name — invoking claude -p (model=$model, budget=$budget)"

        if [[ "$DRY_RUN" == "true" ]]; then
            log "DRY-RUN $name — would invoke claude -p (skipping)"
            set_state "$name" "last_result" "dry_run"
            continue
        fi

        local output_dir="$CUSTOMER_HOME/briefings"
        local output_file="$output_dir/${DATE}-${name}.md"
        mkdir -p "$output_dir"

        # Build claude command — pass gathered data as context if available.
        #
        # `local claude_input=""` (NOT bare `local claude_input`) is load-bearing
        # under zsh: bare `local` on subsequent loop iterations is a no-op when
        # the var is already in scope from the previous iteration. Without the
        # explicit `=""` initializer, the variable retains the prior heartbeat's
        # value, and IF the current branch's `else` arm fires (no gathered_data)
        # but the assignment $(cat) somehow errors or short-circuits, the
        # previous heartbeat's prompt can bleed into THIS heartbeat's claude
        # invocation. Concrete failure mode: dating-swipe's gathered prompt
        # leaking into bfl-ingest's fire on the same dispatcher tick.
        # See scrollinondubs/behalfbot#88.
        local claude_input=""
        if [[ -n "$gathered_data" ]]; then
            claude_input="$(cat "$full_prompt_path")

---
## Gathered Data (from dispatcher)
\`\`\`json
${gathered_data}
\`\`\`"
        else
            claude_input=$(cat "$full_prompt_path")
        fi

        # Retry policy: 3 attempts total (initial + 2 retries) with
        # exponential backoff (20s, 60s). Bumped from 1 retry on
        # 2026-06-24 after morning-briefing failed twice in 4s each with
        # zero tokens consumed — a transient early-init failure
        # (likely OAuth/MCP load hiccup) that the 1-retry policy didn't
        # ride through. Same retry succeeded cleanly on the next tick.
        local invoke_success=false
        local attempt
        for attempt in 1 2 3; do
            if invoke_claude "$claude_input" "$output_file" "$model" "$budget" "$name" "$cwd"; then
                invoke_success=true
                break
            fi
            if [[ $attempt -lt 3 ]]; then
                # 20s after attempt 1, 60s after attempt 2 → total ~80s before final failure
                local wait_s=$(( 20 * (3 ** (attempt - 1)) ))
                log "RETRY $name — claude failed (attempt $attempt/3), waiting ${wait_s}s..."
                sleep "$wait_s"
            fi
        done

        if [[ "$invoke_success" != "true" ]]; then
            log "FAILED $name — claude failed after 3 attempts"
            set_state "$name" "last_result" "claude_failed_after_3_attempts"

            # Circuit-breaker bookkeeping. Increment the per-heartbeat
            # claude_fail_streak. If it crosses the threshold, open the
            # circuit with exponential-backoff cooldown (15-min tick *
            # 2^streak, capped at 32 ticks = 8h). This means a chronically
            # failing heartbeat won't keep wasting 40 min of dispatcher
            # cycle on each tick.
            local fail_streak
            fail_streak=$(get_state "$name" "claude_fail_streak")
            fail_streak=$(( ${fail_streak:-0} + 1 ))
            set_state "$name" "claude_fail_streak" "$fail_streak"

            local circuit_threshold="${CLAUDE_FAIL_CIRCUIT_THRESHOLD:-2}"
            if [[ $fail_streak -ge $circuit_threshold ]]; then
                local backoff_factor=$(( fail_streak - circuit_threshold + 1 ))
                if [[ $backoff_factor -gt 5 ]]; then backoff_factor=5; fi
                local backoff_ticks=$(( 1 << (backoff_factor - 1) ))
                if [[ $backoff_ticks -gt 32 ]]; then backoff_ticks=32; fi
                # Tick interval defaults to 900s (15min) — entrypoint.sh
                # sets DISPATCHER_INTERVAL_SECONDS in its shell but doesn't
                # export it, so we mirror the default here.
                local tick_sec="${DISPATCHER_INTERVAL_SECONDS:-900}"
                local backoff_sec=$(( backoff_ticks * tick_sec ))
                local open_until=$(( $(date +%s) + backoff_sec ))
                set_state "$name" "circuit_open_until" "$open_until"
                log "CIRCUIT-OPENED $name — streak=$fail_streak, skipping FIRE for $backoff_ticks ticks (~$((backoff_sec/60))min)"
            fi

            continue
        fi

        log "SUCCESS $name — output at $output_file"
        set_state "$name" "last_fired" "$(date +%Y-%m-%dT%H:%M:%S)"
        set_state "$name" "last_result" "success"
        increment_fire_count "$name"
        # Successful claude FIRE resets the circuit-breaker state so the next
        # transient failure starts fresh from streak=1.
        set_state "$name" "claude_fail_streak" "0"
        set_state "$name" "circuit_open_until" ""

        # Five Failure Modes post-output validator (#332)
        # Runs only when output_validator: true in HEARTBEATS.md for this heartbeat.
        # On fail: artifact is quarantined, ops webhook alerted, check_and_notify skipped.
        if ! run_output_validator "$name" "$output_file"; then
            set_state "$name" "last_result" "validator_blocked"
            continue
        fi

        # Check for Discord notification signal in output
        check_and_notify "$name" "$output_file"
    done

    log "=== Dispatcher run complete ==="
}

# Redirect all output to log; Claude output goes to its own file via >
main "$@" >> "$LOG_FILE" 2>&1
