#!/usr/bin/env bash
# install.sh - stand up the voice agent on this host from the plugin directory.
#
# Idempotent. Safe to re-run after changing config, pulling a new plugin
# version, or when something has drifted and you want it put back.
#
# What it does, in order:
#   1. refuses to run anywhere it cannot work
#   2. creates the venv and installs the package
#   3. seeds data/ from examples/ without ever overwriting your edits
#   4. installs and loads the launchd agent
#   5. waits for the port and says where the client is
#
# Usage:
#   bash scripts/install.sh
#   VOICE_PLUGIN_HOME=/path/to/plugins/voice-agent bash scripts/install.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_HOME="${VOICE_PLUGIN_HOME:-$(dirname "$HERE")}"
LABEL="com.behalfbot.voice-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

log() { echo "$(date +%H:%M:%S) $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

# --- 1. refuse early, and say why -------------------------------------------
#
# Every one of these is a hard requirement, and each fails in a confusing way
# later if it is not checked here. The MLX one especially: pip will install
# happily on Intel and then the first transcription dies inside a native
# extension with nothing that names the real problem.

[[ "$(uname -s)" == "Darwin" ]] || fail "macOS only - the speech models are MLX"
[[ "$(uname -m)" == "arm64" ]] || fail "Apple Silicon only - MLX does not run on Intel"

command -v curl >/dev/null || fail "curl not found"
command -v claude >/dev/null || \
    fail "claude not found on PATH - escalation shells out to \`claude -p\`, so this plugin needs Claude Code installed on the host"

OLLAMA_URL="${VOICE_OLLAMA_URL:-http://127.0.0.1:11434}"
curl -sf -m 3 "$OLLAMA_URL/v1/models" >/dev/null || \
    fail "Ollama not answering on $OLLAMA_URL - start it, then re-run"

PY312="$(command -v python3.12 || true)"
[[ -n "$PY312" ]] || fail "python3.12 not found - pipecat 1.7 wants it"

# --- 2. venv ----------------------------------------------------------------

cd "$HERE"
if [[ ! -d .venv ]]; then
    log "creating venv"
    if command -v uv >/dev/null; then
        uv venv --python "$PY312" .venv
        uv pip install --python .venv/bin/python -e .
    else
        "$PY312" -m venv .venv
        # Several GB of models get pulled on first run regardless; the wheel
        # download here is the small part.
        .venv/bin/pip install -q --upgrade pip
        .venv/bin/pip install -q -e .
    fi
else
    log "venv exists, leaving it alone"
fi

# --- 3. data files, never clobbering --------------------------------------
#
# These are the operator's own content. Re-running install must never overwrite
# a sensitive-terms list somebody spent an hour on.

mkdir -p "$PLUGIN_HOME/data"
for pair in "aliases.example.json:aliases.json" \
            "sensitive-terms.example.json:sensitive-terms.json" \
            "voice-agent.env.example:voice-agent.env"; do
    src="$PLUGIN_HOME/examples/${pair%%:*}"
    dst="$PLUGIN_HOME/data/${pair##*:}"
    if [[ -f "$dst" ]]; then
        log "keeping existing $(basename "$dst")"
    elif [[ -f "$src" ]]; then
        cp "$src" "$dst"
        log "seeded $(basename "$dst") from the example"
    fi
done

echo
echo "  Before this is useful, edit:"
echo "    $PLUGIN_HOME/data/voice-agent.env          <- name, variants, projects"
echo "    $PLUGIN_HOME/data/sensitive-terms.json     <- what must never be sent"
echo
echo "  The shipped sensitive-terms list is an EXAMPLE from somebody else's life."
echo "  Copied unread it refuses turns about things you do not have and stays"
echo "  silent on the things you do."
echo

# --- 4. launchd -------------------------------------------------------------

TEMPLATE="$PLUGIN_HOME/launchd/$LABEL.plist.template"
[[ -f "$TEMPLATE" ]] || fail "missing $TEMPLATE"

sed "s|__VOICE_HOME__|$HERE|g" "$TEMPLATE" > "$PLIST"
log "wrote $PLIST"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    sleep 1
fi
launchctl bootstrap "gui/$(id -u)" "$PLIST"
log "launchd agent loaded"

# --- 5. wait ----------------------------------------------------------------

PORT="${VOICE_PORT:-7860}"
for _ in $(seq 1 45); do
    if curl -sf -m 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
        log "up on :$PORT"
        echo
        echo "  Client: http://127.0.0.1:$PORT/"
        exit 0
    fi
    sleep 2
done

fail "did not come up on :$PORT within 90s - check $HERE/logs/"
