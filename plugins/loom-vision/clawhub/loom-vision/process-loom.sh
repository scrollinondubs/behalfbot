#!/bin/bash
# process-loom.sh - Download a Loom video, extract key frames + transcript.
#
# Standalone (ClawHub) variant of the Behalf.bot chassis script. Canonical
# chassis source: plugins/loom-vision/skills/loom-vision/process-loom.sh.
#
# Usage:
#   bash process-loom.sh <loom-share-url>
#
# Output:
#   Creates ${OUTPUT_ROOT}/loom-<video_id>/ containing:
#     - video.mp4         (full source video, kept for re-sampling)
#     - transcript.vtt    (Loom auto-transcript with timestamps)
#     - frame_NNN.jpg     (sampled frames at FRAME_INTERVAL_SECONDS cadence)
#   Prints the output directory path to stdout. Progress messages go to stderr.
#
# Configuration (env vars):
#   OUTPUT_ROOT               default ${TMPDIR:-/tmp}/loom-vision
#   FRAME_INTERVAL_SECONDS    default 5
#   FRAME_MAX_WIDTH_PX        default 1280
#   FRAME_QUALITY             default 3 (ffmpeg -q:v, 1-31, lower = better)
#
# Dependencies:
#   - loom-dl (npm install -g loom-dl)
#   - ffmpeg  (brew install ffmpeg, or your distro's package manager)

set -euo pipefail

URL="${1:-}"
if [[ -z "$URL" ]]; then
  echo "ERROR: Usage: process-loom.sh <loom-share-url>" >&2
  exit 1
fi

OUTPUT_ROOT="${OUTPUT_ROOT:-${TMPDIR:-/tmp}/loom-vision}"
FRAME_INTERVAL_SECONDS="${FRAME_INTERVAL_SECONDS:-5}"
FRAME_MAX_WIDTH_PX="${FRAME_MAX_WIDTH_PX:-1280}"
FRAME_QUALITY="${FRAME_QUALITY:-3}"

# Sanity-check deps so the script fails loudly with a clear message instead
# of midway through the pipeline.
if ! command -v loom-dl >/dev/null 2>&1; then
  echo "ERROR: loom-dl not found in PATH. Install with: npm install -g loom-dl" >&2
  exit 2
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg not found in PATH. Install with: brew install ffmpeg" >&2
  exit 2
fi

# Extract video ID from URL - Loom share URLs are .../share/<32-hex>/...
VIDEO_ID=$(echo "$URL" | grep -oE '[a-f0-9]{32}' | head -1)
if [[ -z "$VIDEO_ID" ]]; then
  echo "ERROR: Could not extract a 32-hex video ID from URL: $URL" >&2
  echo "       Expected format: https://www.loom.com/share/<32-hex-id>/..." >&2
  exit 1
fi

OUTPUT_DIR="$OUTPUT_ROOT/loom-$VIDEO_ID"
mkdir -p "$OUTPUT_DIR"

echo "Downloading Loom video $VIDEO_ID..." >&2
loom-dl --url "$URL" --out "$OUTPUT_DIR/video.mp4" --transcript 2>&2

echo "Sampling frames (1 per ${FRAME_INTERVAL_SECONDS}s, max width ${FRAME_MAX_WIDTH_PX}px)..." >&2
ffmpeg -y -i "$OUTPUT_DIR/video.mp4" \
  -vf "fps=1/${FRAME_INTERVAL_SECONDS},scale=${FRAME_MAX_WIDTH_PX}:-1" \
  -q:v "$FRAME_QUALITY" \
  "$OUTPUT_DIR/frame_%03d.jpg" 2>/dev/null

FRAME_COUNT=$(ls "$OUTPUT_DIR"/frame_*.jpg 2>/dev/null | wc -l | tr -d ' ')
DURATION=$(ffmpeg -i "$OUTPUT_DIR/video.mp4" 2>&1 | grep Duration | awk '{print $2}' | tr -d ',' || echo "unknown")

echo "Processed: $FRAME_COUNT frames, duration $DURATION" >&2
echo "$OUTPUT_DIR"
