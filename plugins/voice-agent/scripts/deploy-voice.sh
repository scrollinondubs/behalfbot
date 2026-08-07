#!/usr/bin/env bash
# deploy-voice.sh - put the merged voice code onto the running voice agent.
#
# Why this exists. The Pipecat voice agent runs out of ~/work/voice-phase3/voice,
# which is a working copy whose git worktree registration was deleted on
# 2026-08-07, so `git pull` fails there. Merging a PR therefore reaches the
# repository and nothing else, and a running Python process holds the modules it
# imported at startup regardless. On 2026-08-07 that combination had Sean testing
# this morning's code for eight hours while three merged PRs sat unused, and
# concluding the design was broken when it was simply not deployed.
#
# So: fetch, copy, restart, verify. One command, and the same one launchd calls.
#
# Usage:
#   bash scripts/deploy-voice.sh                 # deploy the merged branch
#   bash scripts/deploy-voice.sh --check         # report drift, change nothing
#   VOICE_BRANCH=some/branch bash scripts/deploy-voice.sh
#
# Exit codes:
#   0  deployed (or --check found no drift)
#   1  something went wrong; the old code is still running
#   2  --check found drift

set -euo pipefail

REPO="${REPO:-$HOME/.behalfbot}"
LIVE="${VOICE_LIVE_DIR:-$HOME/work/voice-phase3/voice}"
BRANCH="${VOICE_BRANCH:-feat/voice-two-brain-router}"
PORT="${VOICE_PORT:-7860}"
LABEL="com.jax.voice-agent"
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

log() { echo "$(date +%H:%M:%S) $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

[[ -d "$LIVE" ]] || fail "no live voice dir at $LIVE"
[[ -d "$REPO/.git" ]] || fail "no git repo at $REPO"

# The set of files that make up the agent. Deliberately explicit rather than a
# directory sync: the live directory also holds .venv (1.6 GB), logs, metrics
# and downloaded model assets, none of which are in git and all of which a
# well-meaning `rsync --delete` would remove.
FILES=(aliases.json)
while IFS= read -r f; do FILES+=("$f"); done < <(
    git -C "$REPO" ls-tree --name-only "origin/$BRANCH" voice/ 2>/dev/null \
        | sed 's|^voice/||' | grep -E '\.(py|json|sh|toml)$' || true
)

log "fetching origin/$BRANCH"
git -C "$REPO" fetch -q origin "$BRANCH" || fail "fetch failed"
HEAD_SHA=$(git -C "$REPO" rev-parse --short "origin/$BRANCH")

# Compare before touching anything, so --check is honest and a no-op deploy
# does not restart the service for nothing.
DRIFT=()
for f in "${FILES[@]}"; do
    if ! git -C "$REPO" show "origin/$BRANCH:voice/$f" > "/tmp/dv.$$" 2>/dev/null; then
        continue
    fi
    if ! cmp -s "/tmp/dv.$$" "$LIVE/$f"; then
        DRIFT+=("$f")
    fi
done
rm -f "/tmp/dv.$$"

if (( CHECK_ONLY )); then
    if (( ${#DRIFT[@]} == 0 )); then
        log "in sync with origin/$BRANCH ($HEAD_SHA)"
        exit 0
    fi
    log "DRIFT vs origin/$BRANCH ($HEAD_SHA): ${DRIFT[*]}"
    exit 2
fi

if (( ${#DRIFT[@]} == 0 )); then
    log "already at origin/$BRANCH ($HEAD_SHA); nothing to copy"
else
    STAMP=$(date +%Y%m%d-%H%M%S)
    BACKUP="$(dirname "$LIVE")/voice-backup-$STAMP"
    mkdir -p "$BACKUP"
    cp "$LIVE"/*.py "$LIVE"/*.json "$BACKUP"/ 2>/dev/null || true
    log "backed up current code to $BACKUP"

    for f in "${DRIFT[@]}"; do
        git -C "$REPO" show "origin/$BRANCH:voice/$f" > "$LIVE/$f" || fail "copy of $f failed"
    done
    log "copied ${#DRIFT[@]} file(s) at $HEAD_SHA: ${DRIFT[*]}"
fi

# Syntax-check before killing anything. A restart into a file that does not
# parse leaves Sean with no voice agent and a traceback in a log he is not
# watching, which is strictly worse than the stale code he had a second ago.
if ! "$LIVE/.venv/bin/python" -c "
import ast, pathlib, sys
bad = []
for p in pathlib.Path('$LIVE').glob('*.py'):
    try:
        ast.parse(p.read_text())
    except SyntaxError as e:
        bad.append(f'{p.name}: {e}')
if bad:
    print('\n'.join(bad)); sys.exit(1)
"; then
    fail "deployed code does not parse; NOT restarting - old process is still up"
fi

log "restarting"
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    # launchd owns the process. kickstart -k stops and starts it in one step and
    # keeps launchd's idea of the world consistent; killing it by hand would
    # just make KeepAlive race us.
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
else
    pkill -f "kokoro_worker.py" 2>/dev/null || true
    pkill -f "bot.py --host 0.0.0.0 --port $PORT" 2>/dev/null || true
    sleep 2
    mkdir -p "$LIVE/logs"
    nohup bash "$LIVE/run.sh" > "$LIVE/logs/run-$(date +%Y%m%d-%H%M%S).log" 2>&1 &
    disown || true
fi

# Model warmup is a few seconds, so poll rather than sleeping a guessed number.
for _ in $(seq 1 40); do
    if curl -sf -m 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
        log "up on :$PORT at $HEAD_SHA"
        exit 0
    fi
    sleep 2
done

fail "did not come up on :$PORT within 80s - check $LIVE/logs/"
