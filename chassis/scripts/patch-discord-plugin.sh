#!/usr/bin/env bash
# patch-discord-plugin.sh - apply chassis-shipped patches to the official
# Claude Code Discord plugin (discord@claude-plugins-official) in the plugin
# cache, so the next `claude --channels` launch runs the patched server.
#
# Why: plugin 0.0.4 delivers a forwarded Discord message as empty. Discord
# stores a forward as a message snapshot on an otherwise empty message, and
# server.ts reads only the message's own content and attachments. The patch
# (chassis/patches/discord-forwarded-messages.patch) reads the snapshots for
# inbound notifications, fetch_messages and download_attachment. See
# chassis/patches/README.md for when to drop it.
#
# Called by the rendered restart-<bot>-discord.sh just before it starts claude,
# so it runs on the host, on every restart, on macOS and Linux alike. The
# plugin cache is ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache/ on both;
# the container only sees it through the ~/.claude bind mount and never runs
# the Discord plugin itself.
#
# Safety contract - this runs ahead of the Discord session, so it must never
# be the reason the session does not start:
#   - It only touches a server.ts whose sha256 is in the known-pristine list.
#     Anything else (an unknown upstream version, a hand-edited copy) is
#     skipped with a WARN line and left exactly as found.
#   - A file that already carries the patch marker is a no-op, so re-running
#     is safe.
#   - The patch is applied to a temp copy with zero fuzz, verified, then moved
#     into place in one rename. A failed apply leaves the original untouched.
#   - The pristine file is kept as server.ts.orig (written once, never
#     overwritten).
#   - Exit code is always 0. Problems are WARN lines in the log.
#   - Logs paths and hashes only, never file content.
#
# Usage:
#   bash chassis/scripts/patch-discord-plugin.sh
#
# Env overrides (tests use these; operators normally set none):
#   DISCORD_PLUGIN_CACHE_DIR    dir holding <version>/server.ts
#                               (default: ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache/claude-plugins-official/discord)
#   DISCORD_FORWARD_PATCH       patch file (default: ../patches/discord-forwarded-messages.patch)
#   DISCORD_FORWARD_PRISTINE    space-separated known-pristine sha256 list
#   DISCORD_FORWARD_MARKER      marker string the patch adds

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CACHE_DIR="${DISCORD_PLUGIN_CACHE_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache/claude-plugins-official/discord}"
PATCH_FILE="${DISCORD_FORWARD_PATCH:-$SCRIPT_DIR/../patches/discord-forwarded-messages.patch}"
MARKER="${DISCORD_FORWARD_MARKER:-behalfbot-patch: discord-forwarded-messages}"

# server.ts of discord@claude-plugins-official 0.0.4. Upstream shipped two
# different files under that same version number:
#   6edc17d9... anthropics/claude-plugins-official@48aa435178 (2026-04-14, current)
#   f634be31... anthropics/claude-plugins-official@4b1e2a28ce (2026-03-24)
# The patch applies to both with zero fuzz. Add a hash here only after
# checking the patch applies cleanly to that exact upstream file.
PRISTINE_SHAS="${DISCORD_FORWARD_PRISTINE:-6edc17d9e9d04930967361ba51c8e03b8f8508647a1dfc11d79ee6a0eb1010b7 f634be3115c873b6181960de014181d01e28c2be556e0d41736733a941f5d451}"

log() { printf '[patch-discord-plugin] %s\n' "$*"; }

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        return 1
    fi
}

is_pristine() {
    local want
    for want in $PRISTINE_SHAS; do
        [[ "$1" == "$want" ]] && return 0
    done
    return 1
}

# Apply $PATCH_FILE (a/server.ts -> b/server.ts) inside $1, which holds a
# copy named server.ts. Zero fuzz: an offset is fine, a fuzzy match is not.
apply_in_dir() {
    if command -v patch >/dev/null 2>&1; then
        (cd "$1" && patch -s -p1 -F 0 -N < "$PATCH_FILE") >/dev/null 2>&1
    elif command -v git >/dev/null 2>&1; then
        (cd "$1" && git apply -p1 "$PATCH_FILE") >/dev/null 2>&1
    else
        log "WARN: neither patch nor git on PATH; cannot apply"
        return 1
    fi
}

patch_one() {
    local target="$1" sha tmpd staged
    if grep -qF "$MARKER" "$target" 2>/dev/null; then
        log "already patched: $target"
        return 0
    fi
    if ! sha="$(sha256_of "$target")" || [[ -z "$sha" ]]; then
        log "WARN: no sha256 tool (sha256sum or shasum); skipping $target"
        return 0
    fi
    if ! is_pristine "$sha"; then
        if grep -qF 'messageSnapshots' "$target" 2>/dev/null; then
            log "WARN: skipping $target (sha256 $sha unknown). It already mentions messageSnapshots - upstream may handle forwards now; check and drop chassis/patches/discord-forwarded-messages.patch."
        else
            log "WARN: skipping $target (sha256 $sha is not a known pristine 0.0.4 server.ts). Forwarded Discord messages will arrive empty until the patch is refreshed for this version."
        fi
        return 0
    fi

    tmpd="$(mktemp -d "${TMPDIR:-/tmp}/discord-fwd-patch.XXXXXX")" || { log "WARN: mktemp failed; skipping $target"; return 0; }
    cp "$target" "$tmpd/server.ts"
    if ! apply_in_dir "$tmpd" || ! grep -qF "$MARKER" "$tmpd/server.ts"; then
        log "WARN: patch did not apply cleanly to $target (sha256 $sha); left untouched"
        rm -rf "$tmpd"
        return 0
    fi

    [[ -e "$target.orig" ]] || cp -p "$target" "$target.orig" || {
        log "WARN: could not write $target.orig; left untouched"
        rm -rf "$tmpd"
        return 0
    }

    # Stage beside the target so the final mv is a same-filesystem rename,
    # and copy the mode first so the plugin file keeps its permissions.
    staged="$target.patching.$$"
    if cp -p "$target" "$staged" && cat "$tmpd/server.ts" > "$staged" && mv -f "$staged" "$target"; then
        log "patched: $target (pristine sha256 $sha -> $(sha256_of "$target"))"
    else
        rm -f "$staged"
        log "WARN: could not move the patched file into place; $target left as it was"
    fi
    rm -rf "$tmpd"
    return 0
}

main() {
    if [[ ! -f "$PATCH_FILE" ]]; then
        log "WARN: patch file missing at $PATCH_FILE; nothing applied"
        return 0
    fi
    if [[ ! -d "$CACHE_DIR" ]]; then
        log "no Discord plugin cache at $CACHE_DIR; nothing to patch"
        return 0
    fi
    local found=0 f
    for f in "$CACHE_DIR"/*/server.ts; do
        [[ -f "$f" ]] || continue
        found=1
        patch_one "$f"
    done
    [[ "$found" -eq 1 ]] || log "no server.ts under $CACHE_DIR/*/; nothing to patch"
    return 0
}

main "$@"
exit 0
