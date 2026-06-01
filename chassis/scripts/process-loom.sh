#!/bin/bash
# process-loom.sh — download a Loom video, extract key frames + transcript.
#
# Usage: process-loom.sh <loom-share-url>
# Output: creates ${CHASSIS_HOME}/temp/loom-<video-id>/ with:
#   - video.mp4
#   - frame_001.jpg, frame_002.jpg, ... (every 5 seconds)
#   - transcript JSON (from Loom's built-in captions, if available)
# Returns the output directory path on stdout.
#
# Dependencies:
#   loom-dl   — Loom video downloader (npm install -g loom-dl, or your equivalent)
#   ffmpeg    — for frame extraction
#
# Environment:
#   CHASSIS_HOME  (required) — chassis root
#   LOOM_DL       (optional, default "loom-dl") — override the downloader binary

set -euo pipefail

URL="${1:?usage: process-loom.sh <loom-share-url>}"

CHASSIS_HOME="${CHASSIS_HOME:?CHASSIS_HOME must be set}"
TEMP_DIR="${CHASSIS_HOME}/temp"
LOOM_DL="${LOOM_DL:-loom-dl}"

# Extract video ID from URL (Loom share URLs contain a 32-char hex ID)
VIDEO_ID=$(echo "$URL" | grep -oE '[a-f0-9]{32}' | head -1)
if [[ -z "$VIDEO_ID" ]]; then
  echo "ERROR: could not extract video ID from URL: $URL" >&2
  exit 1
fi

OUTPUT_DIR="$TEMP_DIR/loom-$VIDEO_ID"
mkdir -p "$OUTPUT_DIR"

# Download video + transcript
echo "Downloading video..." >&2
"$LOOM_DL" --url "$URL" --out "$OUTPUT_DIR/video.mp4" --transcript 2>&2

# Extract key frames (1 frame every 5 seconds, 1280px wide)
echo "Extracting key frames..." >&2
ffmpeg -y -i "$OUTPUT_DIR/video.mp4" \
  -vf "fps=1/5,scale=1280:-1" \
  -q:v 3 \
  "$OUTPUT_DIR/frame_%03d.jpg" 2>/dev/null

FRAME_COUNT=$(ls "$OUTPUT_DIR"/frame_*.jpg 2>/dev/null | wc -l | tr -d ' ')
DURATION=$(ffmpeg -i "$OUTPUT_DIR/video.mp4" 2>&1 | grep Duration | awk '{print $2}' | tr -d ',' || echo "unknown")

echo "Processed: $FRAME_COUNT frames, duration $DURATION" >&2
echo "$OUTPUT_DIR"
