#!/bin/bash
# transcribe-voice.sh — transcribe an audio file using whisper-cli (whisper.cpp).
#
# Usage: transcribe-voice.sh <audio-file-path>
# Supports: ogg, mp3, wav, flac, m4a (converts non-WAV to 16kHz mono WAV first)
# Returns plain text transcription on stdout.
#
# Environment overrides:
#   WHISPER_MODEL_PATH  — defaults to /opt/homebrew/share/whisper-cpp/ggml-small.bin (macOS Homebrew install)
#                         on Linux, point at /usr/local/share/whisper-cpp/<model>.bin or wherever your install put it
#   WHISPER_CLI         — defaults to `whisper-cli` from PATH

set -euo pipefail

INPUT="${1:-}"

if [[ -z "$INPUT" || ! -f "$INPUT" ]]; then
  echo "ERROR: usage: transcribe-voice.sh <audio-file-path>" >&2
  exit 1
fi

MODEL="${WHISPER_MODEL_PATH:-/opt/homebrew/share/whisper-cpp/ggml-small.bin}"
WHISPER="${WHISPER_CLI:-whisper-cli}"

if [[ ! -f "$MODEL" ]]; then
  echo "ERROR: Whisper model not found at $MODEL" >&2
  echo "Set WHISPER_MODEL_PATH to your local install path, or install whisper.cpp:" >&2
  echo "  macOS: brew install whisper-cpp && brew install whisper-cpp-models" >&2
  echo "  Linux: see https://github.com/ggerganov/whisper.cpp" >&2
  exit 1
fi

if ! command -v "$WHISPER" >/dev/null 2>&1; then
  echo "ERROR: $WHISPER not on PATH (set WHISPER_CLI to override)" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg required for non-WAV inputs" >&2
  exit 1
fi

# whisper-cli only reliably reads WAV — convert with ffmpeg
WAV_FILE="$(mktemp /tmp/whisper-XXXXXX.wav)"
trap 'rm -f "$WAV_FILE"' EXIT

ffmpeg -y -i "$INPUT" -ar 16000 -ac 1 -c:a pcm_s16le "$WAV_FILE" 2>/dev/null

"$WHISPER" \
  -m "$MODEL" \
  -f "$WAV_FILE" \
  -nt \
  --output-txt \
  -of "${WAV_FILE%.wav}" 2>/dev/null

# whisper-cli writes <basename>.txt next to the WAV; print + clean
cat "${WAV_FILE%.wav}.txt"
rm -f "${WAV_FILE%.wav}.txt"
