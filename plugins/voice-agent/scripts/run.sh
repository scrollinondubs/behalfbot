#!/usr/bin/env bash
# Start the local voice agent, and optionally publish it on the tailnet.
#
# Every knob here is an environment variable with the old hardcoded value as its
# default, so an existing install behaves identically and a new one does not
# have to edit this file.
set -euo pipefail

cd "$(dirname "$0")"

# The operator's config, loaded so that anything already exported WINS over the
# file. `set -a; source` would do the opposite, and the opposite is wrong here:
# it makes `VOICE_STT_MODEL=... ./run.sh` silently do nothing, which is exactly
# the one-off you reach for when testing a model swap.
ENV_FILE="${VOICE_ENV_FILE:-$(dirname "$(pwd)")/data/voice-agent.env}"
if [[ -f "$ENV_FILE" ]]; then
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"
        key="${key//[[:space:]]/}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        # Already set in the environment? Leave it.
        #
        # ${!key+set} rather than [[ -v $key ]] on purpose: launchd invokes this
        # through /bin/bash, which on macOS is still 3.2, and -v is a bash 4
        # test. The 4-only form parses fine in a login shell and then fails only
        # under launchd, which is the worst place to find out.
        if [ -n "${!key+set}" ]; then continue; fi
        value="${line#*=}"
        value="${value%\"}"; value="${value#\"}"
        export "$key=$value"
    done < "$ENV_FILE"
else
    echo "no config at $ENV_FILE - running on defaults"
fi

PORT="${VOICE_PORT:-7860}"
# 0 skips tailscale entirely and serves on localhost only. That is the right
# setting for a laptop, and for anyone who would rather not publish a
# microphone-adjacent endpoint onto a network.
SERVE_HTTPS_PORT="${VOICE_SERVE_HTTPS_PORT:-8443}"
OLLAMA_URL="${VOICE_OLLAMA_URL:-http://127.0.0.1:11434}"

if [ ! -d .venv ]; then
  echo "No venv. Run: uv venv --python \$(which python3.12) .venv && uv pip install --python .venv/bin/python -e ."
  exit 1
fi

# Checked rather than assumed. Ollama not being up yet is the normal state a few
# seconds after a reboot, and failing loudly here is what lets launchd's
# ThrottleInterval retry instead of the process dying half-initialised.
if ! curl -sf -m 3 "$OLLAMA_URL/v1/models" > /dev/null; then
  echo "Ollama is not responding on $OLLAMA_URL. Start it first."
  exit 1
fi

if [ "$SERVE_HTTPS_PORT" != "0" ]; then
  if command -v tailscale > /dev/null 2>&1; then
    # Idempotent: re-running just re-points the same mapping.
    tailscale serve --bg --https="$SERVE_HTTPS_PORT" "http://127.0.0.1:$PORT" > /dev/null
    HOSTNAME_TS="$(tailscale status --json 2>/dev/null \
      | sed -n 's/.*"DNSName": *"\([^"]*\)\.".*/\1/p' | head -1)"
    echo "Client: https://${HOSTNAME_TS:-<your-tailnet-host>}:$SERVE_HTTPS_PORT/"
  else
    # Not fatal. Serving on localhost is a complete configuration; the tailnet
    # is a convenience for reaching it from a phone.
    echo "tailscale not found - serving on localhost only"
    echo "Client: http://127.0.0.1:$PORT/"
  fi
else
  echo "Client: http://127.0.0.1:$PORT/"
fi

mkdir -p logs
# Chat delivery is off by default in escalation.py so that no eval, benchmark or
# test run can post to a channel a human reads. The real bot is the only thing
# that turns it on, and it does it here.
HF_HUB_OFFLINE=1 VOICE_DISCORD=1 exec .venv/bin/python bot.py --host 0.0.0.0 --port "$PORT"
