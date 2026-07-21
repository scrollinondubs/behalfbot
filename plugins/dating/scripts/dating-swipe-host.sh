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
#   2. Run scripts/gather-dating-swipe.sh. Skip if count=0 (emulator down).
#   3. Invoke `claude -p` with the dating-swipe-prompt against the
#      dating-context cwd. Subagent runs with host Keychain auth + access
#      to all the host-resident infra.
#   4. Log result + cost telemetry.
#
# Pause flags: the prompt itself reads HARD_PAUSE / SOFT_PAUSE flags from
# dating-context/, so we don't need to short-circuit here.
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
        ps -p $PPID -o pid=,command= 2>&1 >> "$FALLBACK_LOG" || true
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
trap 'rm -rf "$LOCK_DIR"' EXIT

log "=== dating-swipe-${SLOT} starting ==="

# Jitter 0-30 min. Matches the chassis dispatcher's jitter behavior so the
# bot-detection profile (when sessions fire relative to slot anchor) is
# preserved across the cutover.
JITTER_SEC=$((RANDOM % 1800))
log "jitter: sleeping ${JITTER_SEC}s before gather"
sleep "$JITTER_SEC"

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

log "invoking claude -p (model=sonnet, budget=$DATING_SWIPE_BUDGET, cwd=$DATING_CWD)"
cd "$DATING_CWD"
CLAUDE_OUT_FILE="$LOG_DIR/${DATE}-dating-swipe-${SLOT}.stdout.log"
if claude --print "$PROMPT" \
        --dangerously-skip-permissions \
        --max-budget-usd "$DATING_SWIPE_BUDGET" \
        > "$CLAUDE_OUT_FILE" 2>&1; then
    log "claude exit 0 (output: $CLAUDE_OUT_FILE)"
else
    rc=$?
    log "ERROR: claude exit $rc (output: $CLAUDE_OUT_FILE)"
fi

log "=== dating-swipe-${SLOT} complete ==="
