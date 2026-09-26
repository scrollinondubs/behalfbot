#!/usr/bin/env bash
# Stage the founder-os plugin for the FounderOS image (behalfbot#215).
#
# Reads FOUNDER_OS_PIN ("<tag> <40-hex-sha>", same convention as
# chassis/PLUGINS_PIN), checks the tag still resolves to the pinned SHA,
# checks out exactly that SHA, and copies founder-os/ into
# founder-os-context/plugin/ with a .pin.json the shim reports on /healthz.
#
# Unpinned (no pin line): stages an empty plugin dir marked pinned:false so
# the image builds and sessions answer 503. That keeps Asks deploys
# unblocked while no founder-os tag exists.
#
# Everything is built in a fresh mktemp staging dir next to the output and
# swapped in only on success, so a failed run leaves the previous
# founder-os-context/ exactly as it was. Old trees go to the Trash, never
# through a recursive delete, and only after a prefix check on the path.
#
# Exit codes (mirroring fetch-plugins.sh):
#   0 - staged, or staged as unpinned
#   3 - SECURITY: malformed pin, or the tag no longer resolves to the SHA
#   4 - the pinned tree has no founder-os/ directory
#   5 - could not reach the plugins repo
#   6 - refused to move a path that failed the prefix check
#
# Separate from build.sh on purpose: build.sh wipes build-context/, and the
# plugins repo is public, so this needs no GITHUB_PAT. Runs on the build
# host (macOS ships /usr/bin/trash).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIN_FILE="${FOUNDER_OS_PIN_FILE:-${SCRIPT_DIR}/FOUNDER_OS_PIN}"
REPO_URL="${FOUNDER_OS_PLUGINS_REPO_URL:-https://github.com/scrollinondubs/behalfbot-plugins.git}"
OUT_PARENT="${FOUNDER_OS_CONTEXT_PARENT:-${SCRIPT_DIR}}"
OUT_PARENT="$(cd "$OUT_PARENT" && pwd)"
CONTEXT_DIR="${OUT_PARENT}/founder-os-context"
STAGING_PREFIX="${OUT_PARENT}/.founder-os-staging."

log() { printf '[build-founder-os] %s\n' "$*" >&2; }

command -v trash >/dev/null 2>&1 || { log "trash is required on the build host"; exit 6; }

# Moves one of this script's own directories to the Trash. Anything that is
# not exactly the context dir or a staging dir under OUT_PARENT is refused.
safe_trash() {
    local target="${1:-}"
    if [[ -z "$target" ]]; then
        log "refusing to trash an empty path"; exit 6
    fi
    if [[ "$target" != "$CONTEXT_DIR" && "$target" != "$STAGING_PREFIX"* ]]; then
        log "refusing to trash '$target': not under ${OUT_PARENT}/ with an expected name"; exit 6
    fi
    if [[ -e "$target" ]]; then
        trash "$target"
    fi
}

STAGING="$(mktemp -d "${STAGING_PREFIX}XXXXXX")"
cleanup() { [[ -n "${STAGING:-}" && -e "$STAGING" ]] && safe_trash "$STAGING"; return 0; }
trap cleanup EXIT
mkdir -p "$STAGING/plugin"

publish() {
    [[ -e "$STAGING/clone" ]] && safe_trash "$STAGING/clone"
    safe_trash "$CONTEXT_DIR"
    mv "$STAGING" "$CONTEXT_DIR"
    STAGING=""
}

pin_line=""
[[ -f "$PIN_FILE" ]] && pin_line=$(grep -vE '^\s*(#|$)' "$PIN_FILE" | head -1 || true)

if [[ -z "$pin_line" ]]; then
    printf '{"pinned": false}\n' > "$STAGING/plugin/.pin.json"
    publish
    log "FOUNDER_OS_PIN has no pin line - staged as unpinned; sessions will answer 503 plugin_unpinned"
    exit 0
fi

PIN_TAG=$(awk '{print $1}' <<<"$pin_line")
PIN_SHA=$(awk '{print $2}' <<<"$pin_line")

if [[ ! "$PIN_TAG" =~ ^v[0-9A-Za-z._-]+$ || ! "$PIN_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    log "SECURITY: pin line '$pin_line' is not '<tag> <40-hex-sha>'. Refusing."
    exit 3
fi

log "resolving tag $PIN_TAG ..."
if ! refs=$(git ls-remote "$REPO_URL" "refs/tags/$PIN_TAG^{}" "refs/tags/$PIN_TAG" 2>/dev/null); then
    log "cannot reach $REPO_URL"
    exit 5
fi
resolved=$(awk '$2 ~ /\^\{\}$/ {print $1; exit}' <<<"$refs")
[[ -z "$resolved" ]] && resolved=$(awk 'NR==1 {print $1}' <<<"$refs")

if [[ "$resolved" != "$PIN_SHA" ]]; then
    log "SECURITY: tag $PIN_TAG resolves to '${resolved:-nothing}' but the pin says $PIN_SHA. Refusing."
    exit 3
fi

git clone --quiet "$REPO_URL" "$STAGING/clone"
git -C "$STAGING/clone" -c advice.detachedHead=false checkout --quiet "$PIN_SHA"

if [[ ! -d "$STAGING/clone/founder-os" ]]; then
    log "$PIN_TAG ($PIN_SHA) has no founder-os/ directory. Refusing."
    exit 4
fi

cp -R "$STAGING/clone/founder-os/." "$STAGING/plugin/"
printf '{"pinned": true, "tag": "%s", "sha": "%s"}\n' "$PIN_TAG" "$PIN_SHA" > "$STAGING/plugin/.pin.json"
publish
log "staged founder-os @ $PIN_TAG ($PIN_SHA) into $CONTEXT_DIR/plugin"
