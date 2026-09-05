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
#                     (e.g. /home/installer/behalfbot or /Users/<user>/behalfbot)
#   HOME            — installer's home directory (for tool paths, .ssh, etc.)
#   PATH            — set up by the launchd plist / systemd unit; this script
#                     does not assume any particular Homebrew prefix.
#
# Optional environment (graceful degradation when unset):
#   OLLAMA_URL              — local Ollama for ask_model conditions. If
#                             unreachable the dispatcher fails open (always
#                             fires Claude on ask_model conditions).
#   DISCORD_WEBHOOK_URL     — for #installer notifications.
#   DISCORD_OPS_WEBHOOK_URL — for output-validator quarantine alerts, and for
#                             the cross-heartbeat claude-failure alarm (#167).
#                             Falls back to DISCORD_WEBHOOK_URL when unset.
#   CLAUDE_GLOBAL_FAIL_THRESHOLD    — consecutive claude FIRE failures across
#                             different heartbeats before the #167 alarm
#                             fires. Default 3.
#   CLAUDE_GLOBAL_DISTINCT_THRESHOLD — how many DIFFERENT heartbeats must be
#                             among those failures before the #167 alarm
#                             fires (guards against one flaky heartbeat
#                             tripping a false "everything is down"). Default 2.
#   DEDUPE_CHURN_WARN_THRESHOLD - consecutive fires with a CHANGED dedup
#                             fingerprint before the dispatcher warns that a
#                             heartbeat's dedupe_key is reading something
#                             volatile and realert_after is therefore
#                             suppressing nothing. Default 3.
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
#   #550 (new-jaxity) budget: is per-invocation, so it cannot stop a heartbeat
#        whose condition never clears - see the fire-caps section below

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
# In the V1 reference install, the .env file's Vaultwarden
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

# Mutex around the read-modify-write cycle below (new-jaxity#412/#409).
# `mkdir` is atomic on every filesystem this script runs on (the container,
# macOS, Linux) with no external dependency (flock is not on macOS by
# default) - exactly one caller can ever create "${STATE_FILE}.lock" at a
# time, so it doubles as a zero-dependency mutex.
#
# This is NOT the same bug a unique-per-writer temp path fixes. A unique tmp
# path only stops two writers' STAGING files from colliding - it does nothing
# about two writers both reading $STATE_FILE before either has written it
# back, computing their update from that same stale snapshot, and the second
# mv silently discarding the first writer's change. That is a lost-update
# race, not a filename collision, and it reproduces reliably under concurrent
# distinct-key writers even with unique tmp paths - see
# tests/test_state_write_race.sh, which failed against a unique-tmp-path-only
# version of this fix before the locking below was added. Observed live as
# the dispatcher firing pulse-triage twice 4s apart on 2026-07-30 - the
# signature of a lost state write, most likely from two dispatcher runs
# overlapping (acquire_lock's PID-file check is itself non-atomic).
#
# Takes an optional target path so the suppression ledger (new-jaxity#550) can
# reuse the identical mutex rather than growing a second, subtly-different copy
# of it. Defaults to $STATE_FILE, so every existing call site and
# tests/test_state_write_race.sh are unchanged.
_state_lock_acquire() {
    local target="${1:-$STATE_FILE}"
    local lock_dir="${target}.lock"
    local waited_ms=0
    # Declared once, outside the loop - zsh prints "holder=<value>" to stdout
    # (like a bare `typeset holder` would) if `local holder` re-runs on a
    # variable that already holds a value from the previous iteration.
    # Verified directly: `f(){ local i=0; while ...; do local x; x=$i; done }`
    # prints "x=<value>" on every iteration after the first.
    local holder
    while ! mkdir "$lock_dir" 2>/dev/null; do
        # Stale-lock recovery: a dispatcher run that died mid-write (kill -9,
        # OOM) leaves the lock dir behind forever otherwise.
        holder=$(cat "$lock_dir/pid" 2>/dev/null || echo "")
        if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
            rmdir "$lock_dir" 2>/dev/null || true
            continue
        fi
        sleep 0.1 2>/dev/null || sleep 1
        waited_ms=$((waited_ms + 100))
        if [[ $waited_ms -ge 10000 ]]; then
            log "WARNING state lock held >10s by pid ${holder:-unknown} - proceeding unlocked, a write may be lost"
            return 1
        fi
    done
    echo $$ > "$lock_dir/pid"
    return 0
}

_state_lock_release() {
    local target="${1:-$STATE_FILE}"
    rm -rf "${target}.lock"
}

set_state() {
    local name="$1" field="$2" value="$3"
    local tmp="${STATE_FILE}.$$.${name}.tmp"
    # `if` rather than a bare call so the timeout ("held >10s, proceeding
    # unlocked") path can't trip `set -e` (new-jaxity#394's process_heartbeat
    # lesson: a nonzero return from a bare statement under set -e kills the
    # whole run, not just this write) - and so _state_lock_release only runs
    # when THIS call actually holds the lock. Releasing unconditionally on
    # the timeout path would rm -rf a lock dir some other writer still owns,
    # letting a second writer in underneath it and recreating the exact race
    # this is here to close.
    if _state_lock_acquire; then
        jq --arg n "$name" --arg f "$field" --arg v "$value" \
            '.[$n] = (.[$n] // {}) | .[$n][$f] = $v' "$STATE_FILE" > "$tmp" \
            && mv "$tmp" "$STATE_FILE"
        _state_lock_release
    else
        jq --arg n "$name" --arg f "$field" --arg v "$value" \
            '.[$n] = (.[$n] // {}) | .[$n][$f] = $v' "$STATE_FILE" > "$tmp" \
            && mv "$tmp" "$STATE_FILE"
    fi
}

increment_fire_count() {
    local name="$1"
    local tmp="${STATE_FILE}.$$.${name}.tmp"
    # Same reasoning as set_state above.
    if _state_lock_acquire; then
        jq --arg n "$name" \
            '.[$n] = (.[$n] // {}) | .[$n].fire_count = ((.[$n].fire_count // "0") | tonumber + 1 | tostring)' \
            "$STATE_FILE" > "$tmp" \
            && mv "$tmp" "$STATE_FILE"
        _state_lock_release
    else
        jq --arg n "$name" \
            '.[$n] = (.[$n] // {}) | .[$n].fire_count = ((.[$n].fire_count // "0") | tonumber + 1 | tostring)' \
            "$STATE_FILE" > "$tmp" \
            && mv "$tmp" "$STATE_FILE"
    fi
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

        # Try JSON array length, then JSON object field. Reject non-JSON:
        # the old wc -l fallback treated any single-line output (including
        # `count=0`) as actual=1, which fired briefings every 10 min when
        # a gather script used key=value instead of JSON (PR #206 incident).
        #
        # The read itself lives in threshold_actual() so the dedup fingerprint
        # (new-jaxity#550) hashes the exact number this decision was made on,
        # rather than a second, independently-drifting reading of the payload.
        local actual=0
        if ! actual=$(threshold_actual "$field" "$gathered_data"); then
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

# Post an operational alert to the ops webhook. Falls back to the main
# install webhook so an unset ops webhook degrades to "noisier" rather than
# "silent". Ported from new-jaxity's customer-side dispatcher (#167) - this
# used to be inlined only in run_output_validator's quarantine path; the
# cross-heartbeat claude-failure alarm below needs the same POST from a
# second call site, so it is a helper now.
alert_ops() {
    local msg="$1"
    local ops_webhook="${DISCORD_OPS_WEBHOOK_URL:-${DISCORD_WEBHOOK_URL:-}}"
    [[ -z "$ops_webhook" ]] && { log "WARN: no ops webhook set, alert dropped: $msg"; return; }
    curl -sf -X POST "$ops_webhook" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg content "$msg" '{content: $content}')" \
        >> "$LOG_FILE" 2>&1 || log "WARN: ops alert POST failed"
}

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

    alert_ops "**Five Failure Modes validator blocked ${name}** — mode: \`${mode_val}\`\n${reason_val}\nArtifact at: \`${quarantine_file}\`"

    return 1
}

# --- Daily fire caps, alert dedup, suppression ledger (new-jaxity#550) ---
#
# 2026-09-01 to 09-05, reference install: `repo-drift` fired 514 times in five
# days for $45.79, 66% of all scheduled spend, and posted 50+ near-identical
# alerts. Nothing here was broken. The gather correctly reported a dirty
# working tree, the condition correctly evaluated true, and the auto-heal
# correctly refused to touch a dirty tree. The condition simply could not
# self-clear without a human, and the human was off-grid.
#
# `budget:` did not help: it is a per-invocation cap, so 514 runs at ~$0.089
# each never came near it. Two things were missing and are added here.
#
#   1. A daily ceiling per heartbeat - `max_fires_per_day`, `max_usd_per_day`.
#      Absolute: once hit, no more model runs for that heartbeat until the date
#      rolls, and a changed condition does not lift it. A cap with an escape
#      hatch is not a cap.
#   2. An identity for the condition being alerted on - `realert_after`, plus
#      an optional `dedupe_key` - so the same unresolved condition alerts once
#      per window instead of once per tick.
#
# Both are opt-in. A heartbeat carrying none of these keys behaves exactly as
# it did before, which is the backwards-compatibility contract for chassis
# config fields.
#
# The fingerprint must not move while the underlying condition holds still.
# This is the whole ballgame and it is easy to get wrong: repo-drift's own
# alerts read "unresolved for 4 days", then "5 days", then "131h", then "180
# hours", and its gather emits `condition_age_hours`, `dirty_oldest_age_hours`
# and `ahead_newest_age_hours` alongside the actual finding. Hashing the gather
# output, or the rendered alert prose, produces a fingerprint that never
# matches itself and a cooldown that silently never fires. So the default
# identity is the value the dispatcher's own threshold condition tested - the
# number it made its fire/no-fire decision on - and anything more precise is
# supplied explicitly as a jq expression. There is deliberately no
# strip-the-volatile-looking-field-names heuristic: guessing which keys are
# volatile from their names is the same bug wearing a different hat.

DEDUPE_FINGERPRINT=""
DEDUPE_REASON=""
CAP_REASON=""

SUPPRESSION_LEDGER="$CUSTOMER_HOME/scheduled-tasks/heartbeat-suppressions.json"

# sha256 differs by platform: Debian/container ships `sha256sum`, macOS ships
# `shasum -a 256`. Same resolution dance as the md5 lookup in schedule_matches.
# With neither available the fingerprint comes back empty and every caller
# treats that as "cannot dedupe, fire anyway" - the fail-open direction.
_sha256_stdin() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 | awk '{print $1}'
    else
        cat >/dev/null
        echo ""
    fi
}

# Read a state field as a non-negative integer. get_state returns "" for an
# absent field, and zsh arithmetic on "" is a fatal "bad math expression"
# under set -e, so every counter read goes through this.
_state_int() {
    local v
    v=$(get_state "$1" "$2")
    [[ "$v" =~ ^[0-9]+$ ]] || v=0
    echo "$v"
}

# Numeric >= for decimal dollar amounts. [[ -ge ]] is integer-only and would
# silently compare "0.6" as a syntax error rather than a number.
_num_ge() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 >= b + 0) }'
}

# "24h" / "90m" / "2d" / "300s" / bare seconds -> seconds. Non-zero exit on
# anything unparseable so the caller can warn rather than silently treating a
# typo as "no window".
parse_duration_seconds() {
    local s="$1" n
    if [[ -z "$s" ]]; then
        echo 0
        return 1
    fi
    n="${s%[smhd]}"
    if [[ ! "$n" =~ ^[0-9]+$ ]]; then
        echo 0
        return 1
    fi
    case "$s" in
        *s) echo $(( n )) ;;
        *m) echo $(( n * 60 )) ;;
        *h) echo $(( n * 3600 )) ;;
        *d) echo $(( n * 86400 )) ;;
        *)  echo $(( n )) ;;
    esac
    return 0
}

# The number a `threshold` condition tests. Shared with evaluate_condition so
# the fire decision and the dedup identity can never drift apart on how a
# gather payload is read. Non-zero exit means the payload was not JSON.
threshold_actual() {
    local field="$1" data="$2"
    if echo "$data" | jq -e 'type == "array"' &>/dev/null; then
        echo "$data" | jq 'length'
        return 0
    fi
    if echo "$data" | jq -e 'type == "object"' &>/dev/null; then
        echo "$data" | jq --arg f "$field" '.[$f] // 0'
        return 0
    fi
    return 1
}

# Stable identity of the condition this heartbeat is alerting on.
#
# Precedence:
#   1. `dedupe_key: <jq expression>` - explicit projection of the gather
#      output. Use this whenever "the same problem" means something more
#      specific than "the count is still N", e.g.
#      `dedupe_key: .needs_sean | map({repo, reason})`.
#   2. `condition: threshold <field> <op> <value>` - the tested value.
#   3. Nothing. `always` and `ask_model` have no machine-readable identity,
#      so dedup is refused loudly instead of guessed at.
heartbeat_fingerprint() {
    local name="$1" condition="$2" data="$3"
    local key_expr="" identity="" rest="" field="" actual=""

    key_expr=$(get_config_field "$name" "dedupe_key")

    if [[ -n "$key_expr" ]]; then
        # -S sorts object keys so two payloads that differ only in key order
        # hash the same; -c keeps it one line.
        identity=$(printf '%s' "$data" | jq -S -c "$key_expr" 2>/dev/null) || identity=""
        if [[ -z "$identity" ]]; then
            return 1
        fi
        printf 'key:%s' "$identity" | _sha256_stdin
        return 0
    fi

    if [[ "$condition" == threshold\ * ]]; then
        rest="${condition#threshold }"
        field=$(echo "$rest" | awk '{print $1}')
        if actual=$(threshold_actual "$field" "$data"); then
            printf 'threshold:%s=%s' "$field" "$actual" | _sha256_stdin
            return 0
        fi
        return 1
    fi

    return 1
}

# Returns 0 to SUPPRESS this fire, 1 to let it through. Sets
# DEDUPE_FINGERPRINT (stored by the caller only on a fire that succeeded) and
# DEDUPE_REASON (for the log line).
dedupe_check() {
    local name="$1" condition="$2" data="$3"
    local realert_after="" window=0 fp="" last_fp="" last_at=0 now=0 elapsed=0 churn=0

    DEDUPE_FINGERPRINT=""
    DEDUPE_REASON=""

    realert_after=$(get_config_field "$name" "realert_after")
    [[ -z "$realert_after" ]] && return 1

    if ! window=$(parse_duration_seconds "$realert_after") || [[ $window -le 0 ]]; then
        log "WARN $name - realert_after '$realert_after' is not a duration (expected 24h, 90m, 2d). Not deduping."
        return 1
    fi

    if ! fp=$(heartbeat_fingerprint "$name" "$condition" "$data") || [[ -z "$fp" ]]; then
        log "WARN $name - realert_after is set but this heartbeat has no stable condition identity (condition: $condition). Firing. Add a dedupe_key: <jq expression> to dedupe it."
        return 1
    fi
    DEDUPE_FINGERPRINT="$fp"

    last_fp=$(get_state "$name" "dedupe_fingerprint")
    last_at=$(_state_int "$name" "dedupe_fired_at")
    now=$(date +%s)

    if [[ "$last_fp" == "$fp" ]]; then
        # Same condition as the last alert. The fingerprint held still, so
        # whatever churn we were tracking is over.
        set_state "$name" "dedupe_churn" "0"
        elapsed=$(( now - last_at ))
        if [[ $elapsed -lt $window ]]; then
            DEDUPE_REASON="identical condition, last alerted $(( elapsed / 60 ))min ago, realert_after=$realert_after"
            return 0
        fi
        return 1
    fi

    # Fingerprint moved. That is normal once; every tick is a misconfiguration.
    # A dedupe_key that reads an age, a timestamp or a monotonic counter never
    # matches itself, so the cooldown never fires and nothing says so - which
    # is exactly the silent failure this feature exists to prevent, rebuilt.
    if [[ -n "$last_fp" ]]; then
        churn=$(( $(_state_int "$name" "dedupe_churn") + 1 ))
        set_state "$name" "dedupe_churn" "$churn"
        if [[ $churn -ge ${DEDUPE_CHURN_WARN_THRESHOLD:-3} ]]; then
            log "WARN $name - dedupe fingerprint changed on $churn consecutive fires, so realert_after has suppressed nothing. The dedupe_key is reading a value that moves every tick (an age, a timestamp, a counter). See docs/heartbeat-dispatcher.md."
        fi
    fi
    return 1
}

# Model invocations this heartbeat has spent today. Counted per fire decision,
# not per retry attempt, and reset by date rather than by a timer.
fires_today() {
    local name="$1"
    if [[ "$(get_state "$name" "fires_date")" != "$DATE" ]]; then
        echo 0
        return 0
    fi
    _state_int "$name" "fires_today"
}

note_fire() {
    local name="$1" n=1
    if [[ "$(get_state "$name" "fires_date")" == "$DATE" ]]; then
        n=$(( $(_state_int "$name" "fires_today") + 1 ))
    else
        set_state "$name" "fires_date" "$DATE"
    fi
    set_state "$name" "fires_today" "$n"
}

# Dollars this heartbeat has spent today, read from the telemetry the
# dispatcher already writes rather than from a counter of our own. Telemetry
# is the surface an operator audits after the fact, so a cap that disagrees
# with it would be worse than no cap. Includes the heartbeat's output-validator
# rows: that spend is caused by this heartbeat. Missing file, malformed lines
# and null costs all read as 0.
usd_today() {
    local name="$1"
    local tfile="$CUSTOMER_HOME/logs/telemetry/$DATE-usage.jsonl"
    if [[ ! -f "$tfile" ]]; then
        echo 0
        return 0
    fi
    jq -R -s --arg n "$name" '
        [ split("\n")[]
          | select(length > 0)
          | (fromjson? // empty)
          | select(.heartbeat == $n or .heartbeat == ($n + "-validator"))
          | (.cost_usd // 0) ]
        | add // 0
    ' "$tfile" 2>/dev/null || echo 0
}

# Returns 0 when this heartbeat is CAPPED (do not fire), 1 when it may fire.
# Sets CAP_REASON.
fire_cap_check() {
    local name="$1"
    local max_fires="" max_usd="" fired=0 spent=0

    CAP_REASON=""
    max_fires=$(get_config_field "$name" "max_fires_per_day")
    max_usd=$(get_config_field "$name" "max_usd_per_day")
    [[ -z "$max_fires" && -z "$max_usd" ]] && return 1

    if [[ -n "$max_fires" ]]; then
        if [[ ! "$max_fires" =~ ^[0-9]+$ ]]; then
            log "WARN $name - max_fires_per_day '$max_fires' is not a whole number. Ignoring."
        else
            fired=$(fires_today "$name")
            if [[ $fired -ge $max_fires ]]; then
                CAP_REASON="max_fires_per_day reached: $fired of $max_fires model invocations already spent today"
                return 0
            fi
        fi
    fi

    if [[ -n "$max_usd" ]]; then
        if [[ ! "$max_usd" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
            log "WARN $name - max_usd_per_day '$max_usd' is not a number. Ignoring."
        else
            spent=$(usd_today "$name")
            if _num_ge "$spent" "$max_usd"; then
                CAP_REASON="max_usd_per_day reached: \$$spent of \$$max_usd already spent today"
                return 0
            fi
        fi
    fi
    return 1
}

# --- Suppression ledger ---
#
# A suppressed alert that exists only as a log line is the failure this whole
# change is a response to: the OAuth bridge logged the same warning 288 times
# over four days into a file nobody reads. So every suppression is also
# recorded as one updatable record per heartbeat in a small JSON object beside
# heartbeat-state.json, cheap for a weekly rollup to read with no model call,
# and REMOVED the moment the condition clears - so "quiet because it was
# fixed" and "quiet because it was muted" are distinguishable.
#
# Schema, keyed by heartbeat name:
#   reason                  fire_cap | usd_cap | dedup
#   detail                  human-readable one-liner (the cap arithmetic, or
#                           the dedup window)
#   fingerprint             condition identity, "" when dedup is not in play
#   first_suppressed_at     ISO local time of the first suppressed tick in
#                           this unbroken run of suppressions
#   first_suppressed_epoch  same instant, for arithmetic
#   last_suppressed_at      ISO local time of the most recent suppressed tick
#   last_suppressed_epoch   same instant, for arithmetic
#   suppressed_ticks        how many checks have been suppressed in this run
#   last_decision           the condition verdict at the last suppressed tick
#
# The two _epoch fields exist because `date -j -f` is macOS-only; a consumer
# that has to parse the ISO strings to do date maths is not portable.

ledger_note() {
    local name="$1" reason="$2" detail="$3" fingerprint="$4" decision="$5"
    local tmp="${SUPPRESSION_LEDGER}.$$.${name}.tmp"
    local now_epoch now_iso
    now_epoch=$(date +%s)
    now_iso=$(date +%Y-%m-%dT%H:%M:%S)

    mkdir -p "$(dirname "$SUPPRESSION_LEDGER")"
    [[ -f "$SUPPRESSION_LEDGER" ]] || echo '{}' > "$SUPPRESSION_LEDGER"

    # Same read-modify-write mutex the state file uses, for the same reason.
    if _state_lock_acquire "$SUPPRESSION_LEDGER"; then
        jq --arg n "$name" --arg r "$reason" --arg d "$detail" \
           --arg fp "$fingerprint" --arg dec "$decision" \
           --arg iso "$now_iso" --arg ep "$now_epoch" '
            .[$n] = ((.[$n] // {}) as $prev | {
                reason: $r,
                detail: $d,
                fingerprint: $fp,
                first_suppressed_at: ($prev.first_suppressed_at // $iso),
                first_suppressed_epoch: ($prev.first_suppressed_epoch // $ep),
                last_suppressed_at: $iso,
                last_suppressed_epoch: $ep,
                suppressed_ticks: (($prev.suppressed_ticks // 0) + 1),
                last_decision: $dec
            })' "$SUPPRESSION_LEDGER" > "$tmp" && mv "$tmp" "$SUPPRESSION_LEDGER"
        _state_lock_release "$SUPPRESSION_LEDGER"
    else
        jq --arg n "$name" --arg r "$reason" --arg d "$detail" \
           --arg fp "$fingerprint" --arg dec "$decision" \
           --arg iso "$now_iso" --arg ep "$now_epoch" '
            .[$n] = ((.[$n] // {}) as $prev | {
                reason: $r,
                detail: $d,
                fingerprint: $fp,
                first_suppressed_at: ($prev.first_suppressed_at // $iso),
                first_suppressed_epoch: ($prev.first_suppressed_epoch // $ep),
                last_suppressed_at: $iso,
                last_suppressed_epoch: $ep,
                suppressed_ticks: (($prev.suppressed_ticks // 0) + 1),
                last_decision: $dec
            })' "$SUPPRESSION_LEDGER" > "$tmp" && mv "$tmp" "$SUPPRESSION_LEDGER"
    fi

    jq -r --arg n "$name" '.[$n].suppressed_ticks // 1' "$SUPPRESSION_LEDGER" 2>/dev/null || echo 1
}

# Drop a heartbeat's ledger entry. Called when the condition evaluates false
# and when a fire actually goes through, so a fixed problem stops showing up in
# the weekly rollup without anyone having to clear it by hand.
ledger_clear() {
    local name="$1"
    local tmp="${SUPPRESSION_LEDGER}.$$.${name}.clr"
    [[ -f "$SUPPRESSION_LEDGER" ]] || return 0
    jq -e --arg n "$name" 'has($n)' "$SUPPRESSION_LEDGER" >/dev/null 2>&1 || return 0

    if _state_lock_acquire "$SUPPRESSION_LEDGER"; then
        jq --arg n "$name" 'del(.[$n])' "$SUPPRESSION_LEDGER" > "$tmp" \
            && mv "$tmp" "$SUPPRESSION_LEDGER"
        _state_lock_release "$SUPPRESSION_LEDGER"
    else
        jq --arg n "$name" 'del(.[$n])' "$SUPPRESSION_LEDGER" > "$tmp" \
            && mv "$tmp" "$SUPPRESSION_LEDGER"
    fi
    return 0
}

# One notice per heartbeat per day when a cap trips. The notice itself has to
# be capped or it recreates the 50-alert problem in miniature.
cap_alert_once() {
    local name="$1" reason="$2" ticks="$3" decision="$4"
    local remedy=""
    [[ "$(get_state "$name" "cap_alert_date")" == "$DATE" ]] && return 0
    set_state "$name" "cap_alert_date" "$DATE"

    # Clearing fires_today lifts a fire-count cap. It does nothing to a dollar
    # cap, which is measured from telemetry rather than from state, so naming
    # it there would send an operator to a file that cannot help them.
    if [[ "$reason" == max_usd_per_day* ]]; then
        remedy="To lift it now, raise \`max_usd_per_day\` for \`${name}\` in HEARTBEATS.md. Today's spend is measured from \`logs/telemetry/${DATE}-usage.jsonl\`, so clearing state will not reset it."
    else
        remedy="To lift it now, clear \`fires_today\` for \`${name}\` in \`scheduled-tasks/heartbeat-state.json\`, or raise \`max_fires_per_day\` in HEARTBEATS.md."
    fi

    alert_ops "🔇 **\`${name}\` hit its daily cap. It will not invoke the model again today.**
${reason}

The condition is still true and the gather still runs every tick, so this is the only notice you get today. A change in the condition does not lift a cap; only the date rolling does.

Last verdict: \`${decision}\`
Checks suppressed so far today: ${ticks}

${remedy} Everything currently suppressed is listed in \`scheduled-tasks/heartbeat-suppressions.json\`."
    return 0
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
            # and is the minimal-change fix approved 2026-05-30.
            set_state "$name" "last_result" "success"
            # The condition cleared, so the dedup identity of the last alert is
            # stale: an identical condition recurring next week is news again
            # and must not be swallowed by a cooldown from the previous
            # episode. Dropping the ledger entry here is what makes "quiet
            # because it was fixed" distinguishable from "quiet because it was
            # capped" in the weekly rollup (new-jaxity#550).
            set_state "$name" "dedupe_fingerprint" ""
            set_state "$name" "dedupe_churn" "0"
            ledger_clear "$name"
            continue
        fi

        # --- Alert dedup, then daily fire caps (new-jaxity#550) ---
        #
        # Both run BEFORE the DRY_RUN branch so a dry run reports exactly what
        # a live run would suppress, and both suppress the FIRE rather than
        # just the Discord post: the model invocation is the expensive half,
        # and an alert nobody is allowed to see is not worth paying for.
        #
        # Dedup first. It asks "is this the same problem we already reported",
        # which is a cheaper and more informative reason to stay quiet than
        # "we are out of budget"; when both would suppress, the dedup reason is
        # the one an operator wants in the ledger.
        local suppressed_ticks=""
        if dedupe_check "$name" "$condition" "$gathered_data"; then
            log "DEDUPED $name - $DEDUPE_REASON"
            suppressed_ticks=$(ledger_note "$name" "dedup" "$DEDUPE_REASON" "$DEDUPE_FINGERPRINT" "$decision")
            set_state "$name" "last_result" "alert_deduped"
            continue
        fi

        if fire_cap_check "$name"; then
            local cap_kind="fire_cap"
            [[ "$CAP_REASON" == max_usd_per_day* ]] && cap_kind="usd_cap"
            suppressed_ticks=$(ledger_note "$name" "$cap_kind" "$CAP_REASON" "$DEDUPE_FINGERPRINT" "$decision")
            log "CAPPED $name - $CAP_REASON (checks suppressed today: $suppressed_ticks)"
            set_state "$name" "last_result" "fire_capped"
            cap_alert_once "$name" "$CAP_REASON" "$suppressed_ticks" "$decision"
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

        # One count per fire decision, not per retry attempt: the three
        # attempts in the loop below are one piece of work, and a heartbeat
        # whose claude calls fail still spends tokens, so a failed fire counts
        # against the daily ceiling too (new-jaxity#550).
        note_fire "$name"

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

            # -----------------------------------------------------------------
            # Cross-heartbeat failure detection (#167)
            # -----------------------------------------------------------------
            #
            # The per-heartbeat breaker above is the right answer to ONE flaky
            # heartbeat. It is the wrong answer to claude being unreachable at
            # all, where it guarantees silence exactly when something is badly
            # wrong.
            #
            # 2026-08-17 to 2026-08-19 (V1 reference install): every FIRE failed for 60
            # hours, first on a missing .mcp.json and then on a zeroed OAuth
            # credential. Eleven heartbeats opened their breakers and went
            # quiet. The gathers kept running and kept reporting "no work", so
            # from outside the install looked healthy. Nobody was told. The
            # operator found it by asking about something else entirely.
            #
            # So: count failures across DIFFERENT heartbeats, and once that
            # crosses the threshold, alert directly and bypass every breaker.
            # Claude's own stderr is appended to the dispatcher log, and in
            # both causes above it named the problem exactly, so the alert
            # carries the last error line rather than making someone go read
            # the log to find out what broke.
            local global_streak
            global_streak=$(get_state "_dispatcher" "claude_global_fail_streak")
            global_streak=$(( ${global_streak:-0} + 1 ))
            set_state "_dispatcher" "claude_global_fail_streak" "$global_streak"
            set_state "_dispatcher" "claude_last_failed_heartbeat" "$name"

            # Distinct-name tracking, so "everything is down" means what it
            # says. A raw count alone can be reached by ONE flaky heartbeat
            # failing three times with nothing else firing in between -
            # plausible overnight, when other heartbeats are inside their
            # breaker cooldowns - and an alarm that cries total outage over a
            # single bad prompt gets muted, which costs more than it saves. So
            # the alert needs failures spread across at least two different
            # heartbeats as well as the raw count.
            local failed_names
            failed_names=$(get_state "_dispatcher" "claude_global_failed_names")
            case " $failed_names " in
                *" $name "*) : ;;
                *) failed_names="${failed_names:+$failed_names }$name" ;;
            esac
            set_state "_dispatcher" "claude_global_failed_names" "$failed_names"
            local distinct_count
            distinct_count=$(printf '%s\n' ${=failed_names} | grep -c . || true)

            local global_threshold="${CLAUDE_GLOBAL_FAIL_THRESHOLD:-3}"
            local distinct_threshold="${CLAUDE_GLOBAL_DISTINCT_THRESHOLD:-2}"
            if [[ $global_streak -ge $global_threshold && $distinct_count -ge $distinct_threshold ]]; then
                local now_epoch last_alert alert_cooldown
                now_epoch=$(date +%s)
                last_alert=$(get_state "_dispatcher" "claude_global_alerted_at")
                alert_cooldown="${CLAUDE_GLOBAL_ALERT_COOLDOWN_SECONDS:-14400}"

                if [[ -z "$last_alert" ]] || (( now_epoch - last_alert >= alert_cooldown )); then
                    # claude does not prefix everything with "Error:". The two
                    # causes of the 60-hour new-jaxity outage read:
                    #
                    #   Error: Invalid MCP configuration:
                    #   MCP config file not found: /app/customer/.mcp.json
                    #
                    #   Failed to authenticate: OAuth session expired and could
                    #   not be refreshed
                    #
                    # Matching only /^Error/ would have carried the first and
                    # missed the second entirely, which is the one that leaves
                    # you staring at "(no error found)" while the install is
                    # down. Match the openers claude actually uses, and take
                    # the last two lines because the useful detail is often on
                    # the continuation line.
                    local err_line
                    err_line=$(grep -aE '^(Error|error|Failed|Invalid|MCP config)' "$LOG_FILE" 2>/dev/null | tail -2)
                    [[ -z "$err_line" ]] && err_line="(no recognisable error line in $(basename "$LOG_FILE") - read the log directly, the stderr of every attempt is appended there)"
                    alert_ops "🚨 **claude -p is failing for every heartbeat.** ${global_streak} consecutive FIRE attempts have failed across different heartbeats, most recently \`${name}\`.

Each one is now opening its own circuit breaker, which means this is the only notice you get - the install will look quiet rather than broken.

Last error claude reported:
\`\`\`
${err_line}
\`\`\`
Nothing scheduled will run until this is fixed. Re-alerts at most every $((alert_cooldown / 3600))h."
                    set_state "_dispatcher" "claude_global_alerted_at" "$now_epoch"
                fi
            fi

            continue
        fi

        log "SUCCESS $name — output at $output_file"
        # The dedup cooldown starts at the alert that actually went out, not at
        # the attempt: a fire that failed reached nobody, so starting the
        # window there would mute the first real alert (new-jaxity#550).
        if [[ -n "$DEDUPE_FINGERPRINT" ]]; then
            set_state "$name" "dedupe_fingerprint" "$DEDUPE_FINGERPRINT"
            set_state "$name" "dedupe_fired_at" "$(date +%s)"
        fi
        ledger_clear "$name"
        set_state "$name" "last_fired" "$(date +%Y-%m-%dT%H:%M:%S)"
        set_state "$name" "last_result" "success"
        increment_fire_count "$name"
        # Successful claude FIRE resets the circuit-breaker state so the next
        # transient failure starts fresh from streak=1.
        set_state "$name" "claude_fail_streak" "0"
        set_state "$name" "circuit_open_until" ""

        # A single success proves claude is reachable, so the cross-heartbeat
        # counter resets here too (#167). If we had alerted, say so - an alarm
        # that never clears trains you to ignore it.
        if [[ -n "$(get_state "_dispatcher" "claude_global_alerted_at")" ]]; then
            alert_ops "**claude -p is working again.** First success after the outage: \`$name\`. Circuit breakers opened during the outage stay open until their cooldown expires or someone clears them in \`scheduled-tasks/heartbeat-state.json\` (fields \`circuit_open_until\` and \`claude_fail_streak\`)."
            set_state "_dispatcher" "claude_global_alerted_at" ""
        fi
        set_state "_dispatcher" "claude_global_fail_streak" "0"
        set_state "_dispatcher" "claude_global_failed_names" ""

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
#
# Guarded so tests/test_state_write_race.sh can `source` this file to reach
# set_state/increment_fire_count in isolation without kicking off a full
# dispatcher run. Unset in every real invocation (launchd/systemd), so
# production behaviour is unchanged.
if [[ "${DISPATCHER_TEST_SOURCE:-0}" != "1" ]]; then
    main "$@" >> "$LOG_FILE" 2>&1
fi
