#!/usr/bin/env bash
# dating-swipe-host.sh — host-side dating session dispatcher.
#
# Dating runs from the HOST, not from inside the chassis container, because
# the dating subagent's dependencies are all host-resident:
#   - Android emulator (host process, screen-bound, adb-server on host:5037)
#   - Playwright + Chromium for the Tinder web flow
#   - CLIP scorer + taste-refs / negative-refs (ML model cache + host fs)
#   - the installer's hand-sorted picks feedback loop (rhl-picks/{like,super-like,pass}/)
#   - dating-context subagent skill bundle under ${CHASSIS_HOME}/dating-context/
#
# Moving the whole stack into the chassis container would require shipping
# Playwright + Chromium-on-arm64, headless emulator, host bind mounts for
# the installer's home directory, and substantial Dockerfile bloat. Today's host-launchd path
# matches the V1 pattern + keeps the chassis runtime clean for installers
# that don't run dating.
#
# Invoked by:
#   com.<assistant>.dating-swipe-1.plist — 10:00 local
#   com.<assistant>.dating-swipe-2.plist — 14:00 local
#   com.<assistant>.dating-swipe-3.plist — 18:00 local
# (each plist passes its slot number as arg 1)
#
# Pipeline:
#   1. Random jitter sleep 0-30 min (the chassis-side heartbeats had this
#      built into the dispatcher; mirroring it here keeps the bot-detection
#      profile the same).
#   2. Memory guard: skip the session when the host is short on free memory
#      (an emulator plus a claude session peaks around 6-7GB).
#   3. Start the emulator through the install's emulator control script, if
#      it ships one, and stop it again on exit. The emulator then only runs
#      while a session needs it.
#   4. Run scripts/gather-dating-swipe.sh. Skip if count=0 (emulator down).
#   5. Invoke `claude -p` with the dating-swipe-prompt against the
#      dating-context cwd, on a pinned model. Subagent runs with host
#      Keychain auth + access to all the host-resident infra.
#   6. Record the outcome in scheduled-tasks/dating-session-state.json.
#
# Pause flags: the prompt itself reads HARD_PAUSE / SOFT_PAUSE flags from
# dating-context/, so we don't need to short-circuit here.
#
# Control flags are different: conservation mode and halt are install-wide,
# and the chassis dispatcher only enforces them for heartbeats it runs. This
# wrapper runs from launchd, outside the dispatcher, so it checks them itself.
# Dating is background work, so either flag skips the session.
#
# See: <v1-reference-install>#698 follow-up + the installer's #<primary> decision to run
# dating-stack host-side as a V1-pattern fat-client plugin.

set -uo pipefail

SLOT="${1:-1}"
# CHASSIS_HOME is the legacy V1 install root. Plugin code prefers CHASSIS_HOME.
: "${CHASSIS_HOME:?CHASSIS_HOME must be exported (install root)}"
DATING_CWD="$CHASSIS_HOME/dating-context"
PROMPT_FILE="$CHASSIS_HOME/scheduled-tasks/dating-swipe-prompt.md"
GATHER="$CHASSIS_HOME/scripts/gather-dating-swipe.sh"
LOG_DIR="$CHASSIS_HOME/logs/scheduled"
DATE=$(date +%Y-%m-%d)
LOG_DIR_ROOT="$LOG_DIR"
mkdir -p "$LOG_DIR_ROOT"

FLAG_DIR="${CUSTOMER_HOME:-$CHASSIS_HOME}/scheduled-tasks"
CONSERVATION_FILE="$FLAG_DIR/conservation-mode.json"
HALT_FILE="$FLAG_DIR/halt.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _lib in "$SCRIPT_DIR/../../../chassis/scripts/_control-flags.sh" "$CHASSIS_HOME/scripts/_control-flags.sh"; do
    if [[ -f "$_lib" ]]; then
        # shellcheck source=/dev/null
        source "$_lib"
        break
    fi
done
unset _lib

# flag_on <file> - true when the flag is on. Uses the dispatcher's own reader
# (auto-lift included) when it is available; otherwise reads .enabled, and an
# existing file that cannot be parsed counts as on, matching the library.
flag_on() {
    if declare -F chassis_control_flag_state >/dev/null; then
        [[ "$(chassis_control_flag_state "$1")" == "on" ]]
        return
    fi
    [[ -f "$1" ]] || return 1
    local enabled
    enabled=$(jq -r '.enabled // false' "$1" 2>/dev/null) || return 0
    [[ "$enabled" == "true" ]]
}

# control_flag_blocks - print why this session must not run, or nothing.
control_flag_blocks() {
    if flag_on "$HALT_FILE"; then
        echo "halt"
    elif flag_on "$CONSERVATION_FILE"; then
        echo "conservation mode"
    fi
}

# SLOT validation. The launchd plists pass 1/2/3 — those are the only valid
# values. If anything else is passed (e.g. claude itself invoking this script
# with a freeform name like "screenshot" — observed 2026-05-25 during slot-1's
# session where the dating subagent spawned a child task that mistakenly
# re-invoked this wrapper with SLOT="screenshot"), log + exit. The over-fire
# wasted ~$1 + risked bot-detection by triggering 3 swipe sessions in 20 min
# instead of the intended 3 sessions across 8 hours.
case "$SLOT" in
    1|2|3) ;;
    *)
        FALLBACK_LOG="$LOG_DIR_ROOT/${DATE}-dating-swipe-host-bad-slot.log"
        echo "[$(date '+%H:%M:%S')] REFUSED: invalid SLOT='$SLOT' (must be 1, 2, or 3). Called via:" >> "$FALLBACK_LOG"
        ps -p $PPID -o pid=,command= >> "$FALLBACK_LOG" 2>&1 || true
        echo "  args=$*" >> "$FALLBACK_LOG"
        exit 2
        ;;
esac

LOG="$LOG_DIR/${DATE}-dating-swipe-${SLOT}.log"

mkdir -p "$LOG_DIR"
ts() { date '+%H:%M:%S'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

# Concurrency lock — only one dating-swipe wrapper may run at a time across
# all slots, so a slot-2 fire at 14:00 never overlaps a still-running slot-1
# kickstarted at 13:30. The lock is per-host (not per-slot) because the
# emulator + Playwright session state can't be safely shared between
# concurrent claude invocations.
#
# Use `mkdir` for atomic locking — macOS doesn't ship `flock(1)` by default
# and `mkdir` is POSIX-portable. The directory's mtime + a `pid` file inside
# gives us staleness detection (if the owning pid is dead, the lock is stale
# and we steal it).
LOCK_DIR="$LOG_DIR_ROOT/dating-swipe-host.lock.d"
STATE_FILE="$FLAG_DIR/dating-session-state.json"
acquire_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "$$ slot=$SLOT" > "$LOCK_DIR/pid"
        return 0
    fi
    # Lock exists. Check if owning pid is still alive.
    OWNER=$(cat "$LOCK_DIR/pid" 2>/dev/null | awk '{print $1}')
    if [[ -n "$OWNER" ]] && kill -0 "$OWNER" 2>/dev/null; then
        # Owner alive — refuse to fire.
        log "skip: lock held by pid=$OWNER ($(cat "$LOCK_DIR/pid" 2>/dev/null)). Will not double-fire."
        return 1
    fi
    # Owner dead — steal the lock.
    log "stealing stale lock from pid=$OWNER"
    echo "$$ slot=$SLOT (stole-from $OWNER)" > "$LOCK_DIR/pid"
    return 0
}
if ! acquire_lock; then
    log "=== dating-swipe-${SLOT} complete (no-op, lock contention) ==="
    exit 0
fi

# Model pinned on purpose. Unpinned, the session ran on whatever the CLI
# default was that day, so opener quality could move with no change on our
# side and no record of which model wrote what.
DATING_SWIPE_MODEL="${DATING_SWIPE_MODEL:-claude-opus-4-7}"
SESSION_STARTED=$(date +%s)
# Exported so the session's own tools (the opener ledger in particular) can
# record which session, slot and model produced each send.
export DATING_SESSION_ID="${DATE}-slot${SLOT}-${SESSION_STARTED}"
export DATING_SESSION_SLOT="$SLOT"
export DATING_SESSION_MODEL="$DATING_SWIPE_MODEL"

# Emulator lifecycle. When the install provides a control script, the
# emulator is started for this session and stopped when it ends, instead of
# a daemon keeping it up all day. Without one, the emulator is assumed to be
# managed elsewhere (the always-on model) and is left alone.
EMULATOR_CTL="${DATING_EMULATOR_CTL:-$CHASSIS_HOME/scripts/dating-emulator-session.sh}"
EMULATOR_STARTED=0
cleanup() {
    if [[ "$EMULATOR_STARTED" == "1" ]]; then
        log "emulator: stopping ($("$EMULATOR_CTL" stop 2>&1 | tail -1))"
    fi
    rm -rf "$LOCK_DIR"
}
trap cleanup EXIT
trap 'exit 143' TERM INT

# record_session <outcome> - write the outcome where the pipeline-health
# check reads it. last_ran only moves on a session that reached claude, so a
# run of deliberate skips still shows up there as staleness.
record_session() {
    local now cur prev_ran tmp
    now=$(date +%s)
    cur=$(jq -n --arg slot "$SLOT" --arg id "$DATING_SESSION_ID" \
        --arg model "$DATING_SWIPE_MODEL" --arg outcome "$1" \
        --argjson started "$SESSION_STARTED" --argjson finished "$now" \
        '{slot: $slot, session_id: $id, model: $model, outcome: $outcome,
          started_at: $started, finished_at: $finished}') || return 0
    prev_ran=$(jq -c '.last_ran // null' "$STATE_FILE" 2>/dev/null) || prev_ran=null
    [[ -n "$prev_ran" ]] || prev_ran=null
    if [[ "$1" == "ran" || "$1" == "claude_error" ]]; then
        prev_ran="$cur"
    fi
    mkdir -p "$(dirname "$STATE_FILE")"
    tmp="$STATE_FILE.tmp.$$"
    jq -n --argjson last "$cur" --argjson ran "$prev_ran" \
        '{last_session: $last, last_ran: $ran}' > "$tmp" && mv "$tmp" "$STATE_FILE"
}

# memory_free_pct - macOS free-memory percentage, or nothing if unreadable.
# Bare `memory_pressure` only: its flags simulate pressure rather than read it.
memory_free_pct() {
    command -v memory_pressure >/dev/null 2>&1 || return 0
    memory_pressure 2>/dev/null \
        | awk -F': *' '/System-wide memory free percentage/ {gsub(/%/, "", $2); print $2 + 0; exit}'
}

log "=== dating-swipe-${SLOT} starting ==="

# Checked before and after the jitter, because a flag can flip during the
# sleep of up to 30 minutes.
skip_if_flagged() {
    local why
    why=$(control_flag_blocks)
    [[ -n "$why" ]] || return 0
    log "skip: $why is on ($1). The next slot after it lifts runs normally."
    log "=== dating-swipe-${SLOT} complete (no-op, $why) ==="
    record_session "skipped_${why// /_}"
    exit 0
}
skip_if_flagged "before jitter"

# Jitter 0-30 min. Matches the chassis dispatcher's jitter behavior so the
# bot-detection profile (when sessions fire relative to slot anchor) is
# preserved across the cutover.
JITTER_SEC=$((RANDOM % 1800))
log "jitter: sleeping ${JITTER_SEC}s before gather"
sleep "$JITTER_SEC"

skip_if_flagged "after jitter"

# Memory guard, checked right before anything heavy starts. Nothing used to
# check before launching the emulator and a claude session on top of
# whatever else the host was running.
MIN_FREE_PCT="${DATING_MIN_FREE_PCT:-35}"
FREE_PCT=$(memory_free_pct)
if [[ -z "$FREE_PCT" ]]; then
    log "memory guard: free memory unreadable here, proceeding"
elif (( FREE_PCT < MIN_FREE_PCT )); then
    log "skip: memory too tight - ${FREE_PCT}% free, need ${MIN_FREE_PCT}% (DATING_MIN_FREE_PCT)"
    log "=== dating-swipe-${SLOT} complete (no-op, low memory) ==="
    record_session "skipped_memory"
    exit 0
else
    log "memory guard: ${FREE_PCT}% free (min ${MIN_FREE_PCT}%)"
fi

if [[ -x "$EMULATOR_CTL" ]]; then
    log "emulator: starting for this session"
    # Set before the call: a start that fails part-way can leave a qemu
    # behind, and cleanup should stop it either way.
    EMULATOR_STARTED=1
    if EMU_OUT=$("$EMULATOR_CTL" start 2>&1); then
        log "emulator: $(echo "$EMU_OUT" | tail -1)"
    else
        EMU_RC=$?
        log "skip: emulator start failed (rc=$EMU_RC): $(echo "$EMU_OUT" | tail -1)"
        log "=== dating-swipe-${SLOT} complete (no-op, emulator) ==="
        record_session "emulator_failed"
        exit 0
    fi
fi

# Gather: only fire claude if the emulator is actually ready. Without this
# we'd waste a sonnet invocation when the emulator is down, the gather is
# cheap (just adb devices).
GATHER_OUT=$(bash "$GATHER" 2>&1)
GATHER_RC=$?
log "gather rc=$GATHER_RC: $GATHER_OUT"

COUNT=$(echo "$GATHER_OUT" | jq -r '.count // 0' 2>/dev/null)
if [[ "$COUNT" != "1" ]]; then
    log "skip: gather returned count=$COUNT (emulator not ready or already-done check failed)"
    log "=== dating-swipe-${SLOT} complete (no-op) ==="
    record_session "not_ready"
    exit 0
fi

# Fire claude with the dating-swipe prompt. cwd=dating-context so the
# subagent picks up its CLAUDE.md (lives at $CHASSIS_HOME/dating-context/).
# Budget matches the chassis HEARTBEATS.md entry (6 USD ceiling for a full
# 3-platform session).
# Per-session budget ceiling. Default 6 USD covers a 3-platform session.
# Customers can override via DATING_SWIPE_BUDGET in their .env (re-baked into
# .env.baked + read by launchd via the plist EnvironmentVariables block).
DATING_SWIPE_BUDGET="${DATING_SWIPE_BUDGET:-6}"

if [[ ! -f "$PROMPT_FILE" ]]; then
    log "ERROR: prompt file missing — $PROMPT_FILE"
    exit 1
fi
if [[ ! -d "$DATING_CWD" ]]; then
    log "ERROR: dating-context dir missing — $DATING_CWD"
    exit 1
fi

PROMPT=$(<"$PROMPT_FILE")

log "invoking claude -p (model=$DATING_SWIPE_MODEL, budget=$DATING_SWIPE_BUDGET, cwd=$DATING_CWD, session=$DATING_SESSION_ID)"
cd "$DATING_CWD" || exit 1
CLAUDE_OUT_FILE="$LOG_DIR/${DATE}-dating-swipe-${SLOT}.stdout.log"
if claude --print "$PROMPT" \
        --model "$DATING_SWIPE_MODEL" \
        --dangerously-skip-permissions \
        --max-budget-usd "$DATING_SWIPE_BUDGET" \
        > "$CLAUDE_OUT_FILE" 2>&1; then
    log "claude exit 0 (output: $CLAUDE_OUT_FILE)"
    record_session "ran"
else
    rc=$?
    log "ERROR: claude exit $rc (output: $CLAUDE_OUT_FILE)"
    record_session "claude_error"
fi

log "=== dating-swipe-${SLOT} complete ==="
