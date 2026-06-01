#!/usr/bin/env bash
# send-to-remarkable.sh — canonical OTA delivery to Sean's reMarkable.
#
# All outbound files (briefings, .epubs, .pdfs) go through this script.
# Replaces the legacy "sideload via reMarkable desktop / USB" pattern
# documented in older skill files. Uses the ddvk fork of `rmapi`
# (https://github.com/ddvk/rmapi) for cloud-sync delivery — the tablet
# picks up the file within seconds of upload when on wifi.
#
# Why ddvk and not juruen:
# ========================
# The original `juruen/rmapi` was the canonical for years but the API
# has drifted; juruen's calls now return HTTP 400 against current
# reMarkable cloud endpoints. ddvk's fork tracks the live API. As of
# 2026-05-25 ddvk v0.0.34 auths cleanly with a one-time pair code from
# https://my.remarkable.com/device/desktop/connect and the resulting
# usertoken refreshes itself automatically.
#
# Usage:
#   bash scripts/send-to-remarkable.sh <local-file> [<remote-path>]
#
# Examples:
#   bash scripts/send-to-remarkable.sh briefings/2026-05-25-foo.epub
#       → uploads to reMarkable cloud root as "2026-05-25-foo"
#   bash scripts/send-to-remarkable.sh briefings/foo.epub /Briefings/
#       → uploads into the "Briefings" folder on the tablet
#
# Exit codes:
#   0 = upload succeeded
#   1 = local file missing
#   2 = rmapi binary missing
#   3 = rmapi auth broken (config missing or stale; needs re-pair)
#   4 = upload failed for other reason (network, API, etc.)
#
# Re-pair runbook (when exit 3 fires):
#   1. On any device with a browser, visit
#      https://my.remarkable.com/device/desktop/connect
#   2. Sign in (Sean's reMarkable account)
#   3. Copy the 8-char one-time code
#   4. `rm ~/.rmapi && echo "<code>" | rmapi ls`
#   5. The first `ls` call exchanges the code for fresh tokens and
#      writes them to ~/.rmapi. Subsequent calls work without further
#      interaction.
#
# This script is also wrapped by:
#   - scripts/gather-remarkable-health.sh (heartbeat: alerts when auth fails)
#   - Eventually: perplexity-research skill (Section 5 delivery step)

set -uo pipefail

RMAPI="${RMAPI_BIN:-$(command -v rmapi 2>/dev/null || echo rmapi)}"

LOCAL_FILE="${1:-}"
REMOTE_PATH="${2:-/}"

if [[ -z "$LOCAL_FILE" ]]; then
    echo "Usage: $0 <local-file> [<remote-path>]" >&2
    exit 1
fi

if [[ ! -f "$LOCAL_FILE" ]]; then
    echo "ERR: local file not found: $LOCAL_FILE" >&2
    exit 1
fi

if [[ ! -x "$RMAPI" ]]; then
    echo "ERR: rmapi binary not found at $RMAPI" >&2
    echo "  Install with:" >&2
    echo "    curl -sSL -o /tmp/rmapi.zip \\" >&2
    echo "      https://github.com/ddvk/rmapi/releases/download/v0.0.34/rmapi-macos-arm64.zip" >&2
    echo "    cd /tmp && unzip rmapi.zip && mv rmapi $RMAPI && chmod +x $RMAPI" >&2
    exit 2
fi

# Sanity-check auth before uploading. `rmapi ls /` is cheap (single
# HTTP GET) and exposes any 4xx auth issues before we waste the upload.
if ! "$RMAPI" ls / >/dev/null 2>&1; then
    echo "ERR: rmapi auth check (ls /) failed. Re-pair required:" >&2
    echo "  1. Visit https://my.remarkable.com/device/desktop/connect" >&2
    echo "  2. Sign in, copy the 8-char one-time code" >&2
    echo "  3. rm ~/.rmapi && echo \"<code>\" | $RMAPI ls" >&2
    exit 3
fi

# Upload. rmapi `put <local> <remote-dir>` — remote-dir must already
# exist. Default to root "/". Filename on tablet derives from the local
# stem (rmapi strips the extension).
if "$RMAPI" put "$LOCAL_FILE" "$REMOTE_PATH" >&2; then
    echo "OK: uploaded $(basename "$LOCAL_FILE") to $REMOTE_PATH" >&2
    exit 0
fi
echo "ERR: rmapi upload failed (network / API / unknown)" >&2
exit 4
