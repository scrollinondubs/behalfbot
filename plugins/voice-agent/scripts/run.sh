#!/usr/bin/env bash
# Start the local voice agent and expose it over HTTPS on the tailnet.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "No venv. Run: uv venv --python /opt/homebrew/bin/python3.12 .venv && uv pip install --python .venv/bin/python -e ."
  exit 1
fi

if ! curl -sf -m 3 http://127.0.0.1:11434/v1/models > /dev/null; then
  echo "Ollama is not responding on 127.0.0.1:11434. Start it first."
  exit 1
fi

# Idempotent: re-running just re-points the same mapping.
tailscale serve --bg --https=8443 http://127.0.0.1:7860 > /dev/null

mkdir -p logs
echo "Client: https://jaxs-mac-mini.tail20bf90.ts.net:8443/"
# Discord delivery is off by default in escalation.py so that no eval, benchmark
# or test run can post to a channel a human reads. The real bot is the only
# thing that turns it on, and it does it here.
HF_HUB_OFFLINE=1 VOICE_DISCORD=1 exec .venv/bin/python bot.py --host 0.0.0.0 --port 7860
