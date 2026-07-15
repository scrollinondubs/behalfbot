#!/bin/bash
# setup.sh - Install loom-vision system dependencies.
#
# Idempotent: re-running is a no-op when deps are already in PATH.
#
# Run once on a fresh chassis install. Invoked automatically by
# chassis/bootstrap.sh when modules.loom-vision.enabled == true.

set -euo pipefail

echo "[loom-vision] checking deps..."

# node - runs the transcript JSON -> VTT conversion in process-loom.sh, and is
# required by loom-dl anyway. Installing loom-dl below needs npm (hence node),
# so this is mostly a clear, early failure message.
if command -v node >/dev/null 2>&1; then
  echo "[loom-vision] node present ($(node --version 2>/dev/null || echo 'unknown version'))"
else
  if command -v brew >/dev/null 2>&1; then
    echo "[loom-vision] installing node via Homebrew..."
    brew install node
  else
    echo "[loom-vision] ERROR: node not found and Homebrew unavailable. Install Node manually." >&2
    exit 1
  fi
fi

# loom-dl - Node CLI, not on Homebrew
if command -v loom-dl >/dev/null 2>&1; then
  echo "[loom-vision] loom-dl present ($(loom-dl --version 2>/dev/null || echo 'unknown version'))"
else
  if ! command -v npm >/dev/null 2>&1; then
    echo "[loom-vision] ERROR: npm not found. Install Node first (brew install node)." >&2
    exit 1
  fi
  echo "[loom-vision] installing loom-dl via npm..."
  npm install -g loom-dl
fi

# ffmpeg - frame sampling
if command -v ffmpeg >/dev/null 2>&1; then
  echo "[loom-vision] ffmpeg present ($(ffmpeg -version 2>/dev/null | head -1))"
else
  if ! command -v brew >/dev/null 2>&1; then
    echo "[loom-vision] ERROR: Homebrew not found. Install ffmpeg manually for your platform." >&2
    exit 1
  fi
  echo "[loom-vision] installing ffmpeg via Homebrew..."
  brew install ffmpeg
fi

echo "[loom-vision] setup complete."
