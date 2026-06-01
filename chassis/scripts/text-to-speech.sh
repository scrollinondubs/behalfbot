#!/bin/bash
# text-to-speech.sh — convert text to MP3 via OpenAI TTS.
#
# Usage: text-to-speech.sh "Text to speak" [output.mp3]
#
# Engine: OpenAI TTS (canonical voice = Onyx). Per the V1 reference install
# (LESSONS_FROM_V1.md context + memory `feedback_jax_canonical_tts_voice_onyx`),
# the chassis does NOT fall back to macOS `say` — Onyx is the chassis voice
# for any installer that activates this skill. If OpenAI is unreachable,
# we fail loud rather than ship a robotic-sounding fallback.
#
# Hard limit: OpenAI TTS rejects inputs >4096 chars. Caller must chunk.
#
# Environment:
#   OPENAI_API_KEY       — required. Hydrate from your password manager before invocation.
#   OPENAI_TTS_VOICE     — default "onyx". Override only if installer explicitly chooses a different voice.
#   OPENAI_TTS_MODEL     — default "tts-1-hd". "tts-1" is half the cost, lower fidelity.
#
# Returns the output file path on stdout.

set -euo pipefail

TEXT="${1:?usage: text-to-speech.sh \"Text to speak\" [output.mp3]}"
OUTPUT="${2:-/tmp/behalfbot-tts-$(date +%s).mp3}"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: OPENAI_API_KEY not set. Hydrate from your password manager (e.g. via the Vaultwarden CLI)." >&2
  exit 1
fi

if [[ ${#TEXT} -gt 4096 ]]; then
  echo "ERROR: input is ${#TEXT} chars, OpenAI TTS hard limit is 4096. Chunk before calling." >&2
  exit 1
fi

VOICE="${OPENAI_TTS_VOICE:-onyx}"
MODEL="${OPENAI_TTS_MODEL:-tts-1-hd}"

JSON_PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'model': sys.argv[1],
    'input': sys.argv[2],
    'voice': sys.argv[3],
    'response_format': 'mp3'
}))
" "$MODEL" "$TEXT" "$VOICE")

curl -sf "https://api.openai.com/v1/audio/speech" \
  -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$JSON_PAYLOAD" \
  -o "$OUTPUT"

echo "$OUTPUT"
