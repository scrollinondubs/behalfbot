#!/usr/bin/env bash
# fetch-plugins.sh - runtime-pull plugin fetcher (behalfbot#53, Phase 0).
#
# Fetches the behalfbot-plugins repo at the tag+SHA pinned in PLUGINS_PIN,
# verifies the SHA, and atomically installs the tree into
# $CUSTOMER_HOME/vendored-plugins, writing $CUSTOMER_HOME/plugins.lock.
#
# CANONICAL COPY. This file (tools/fetch-plugins.sh in behalfbot-plugins) is
# the source of truth for the fetcher, so it cannot drift between chassis.
# Each chassis carries a seed copy at chassis/scripts/fetch-plugins.sh - keep
# them byte-identical; chassis-side changes go through this repo first.
#
# Security model (fleet-RCE fix from the behalfbot#53 adversarial review,
# finding 3): the pin records BOTH the tag and the commit SHA. A tag is
# human-readable; the SHA is the gate. If the tag no longer resolves to the
# pinned SHA (i.e. someone force-moved the tag), this script REFUSES to fetch
# and keeps the previous tree. A moved tag never auto-refetches.
#
# Destination (finding 1 from the same review): $CUSTOMER_HOME/vendored-plugins.
# NOT $CUSTOMER_HOME/plugins - that directory already holds live customer-local
# plugins (e.g. midnight-oil) and must never be clobbered by a fetch.
# vendored-plugins/ sits on the existing customer bind mount: writable by the
# runtime uid, survives container recreate.
#
# Pin resolution order (first file found wins):
#   1. $PLUGINS_PIN_FILE (explicit override)
#   2. $CUSTOMER_HOME/chassis/chassis/PLUGINS_PIN  (chassis clone overlay - ships
#      via clone refresh, no image release needed for a pin bump)
#   3. $CHASSIS_ROOT/PLUGINS_PIN                   (image-baked fallback)
#
# Pin file format: single non-comment line "<tag> <40-hex-sha>".
# No non-comment line = unpinned = no-op (baked /app/plugins stays active).
#
# Exit codes:
#   0 - fetched, or valid no-op (already current / unpinned / frozen / offline
#       with a previous tree still in place)
#   3 - SECURITY: tag resolved to a SHA that does not match the pin. No fetch
#       performed, previous tree untouched.
#   4 - corrupt staging tree (sanity check failed). Previous tree untouched.
#
# Boot callers (entrypoint.sh) treat nonzero as WARN and continue on the
# previous/baked tree - a fetch problem must never take the bot down.
#
# Usage:
#   fetch-plugins.sh                # fetch/verify per pin
#   fetch-plugins.sh --freeze       # mark lockfile frozen; skip all future fetches
#   fetch-plugins.sh --unfreeze     # re-enter the fetch flow
#   fetch-plugins.sh --tag vX.Y.Z --sha <sha>   # test override (bypasses pin file)

set -euo pipefail

: "${PLUGINS_REPO:=scrollinondubs/behalfbot-plugins}"
: "${CUSTOMER_HOME:=${CHASSIS_HOME:-/app/customer}}"
: "${CHASSIS_ROOT:=/app/chassis}"
: "${PLUGINS_FETCH_ROOT:=$CUSTOMER_HOME/vendored-plugins}"
LOCK_FILE="$CUSTOMER_HOME/plugins.lock"

log() { printf '[fetch-plugins] %s\n' "$*" >&2; }

# --- args -------------------------------------------------------------------
OVERRIDE_TAG=""
OVERRIDE_SHA=""
ACTION="fetch"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --freeze)   ACTION="freeze" ;;
        --unfreeze) ACTION="unfreeze" ;;
        --tag)      OVERRIDE_TAG="${2:?--tag needs a value}"; shift ;;
        --sha)      OVERRIDE_SHA="${2:?--sha needs a value}"; shift ;;
        *) log "unknown arg: $1"; exit 2 ;;
    esac
    shift
done

set_frozen() {
    local val="$1"
    if [[ ! -f "$LOCK_FILE" ]]; then
        log "no $LOCK_FILE yet - nothing to $ACTION"
        exit 0
    fi
    python3 - "$LOCK_FILE" "$val" <<'PY'
import json, sys
path, val = sys.argv[1], sys.argv[2] == "true"
data = json.load(open(path))
data["frozen"] = val
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
    log "lockfile frozen=$val"
    exit 0
}
[[ "$ACTION" == "freeze"   ]] && set_frozen true
[[ "$ACTION" == "unfreeze" ]] && set_frozen false

# --- read pin ---------------------------------------------------------------
PIN_TAG="$OVERRIDE_TAG"
PIN_SHA="$OVERRIDE_SHA"
if [[ -z "$PIN_TAG" ]]; then
    PIN_FILE=""
    for cand in \
        "${PLUGINS_PIN_FILE:-}" \
        "$CUSTOMER_HOME/chassis/chassis/PLUGINS_PIN" \
        "$CHASSIS_ROOT/PLUGINS_PIN"; do
        [[ -n "$cand" && -f "$cand" ]] && { PIN_FILE="$cand"; break; }
    done
    if [[ -z "$PIN_FILE" ]]; then
        log "no PLUGINS_PIN file found - unpinned, skipping fetch (baked plugins stay active)"
        exit 0
    fi
    pin_line=$(grep -vE '^\s*(#|$)' "$PIN_FILE" | head -1 || true)
    if [[ -z "$pin_line" ]]; then
        log "PLUGINS_PIN at $PIN_FILE has no pin line - unpinned, skipping fetch"
        exit 0
    fi
    PIN_TAG=$(awk '{print $1}' <<<"$pin_line")
    PIN_SHA=$(awk '{print $2}' <<<"$pin_line")
fi

if [[ ! "$PIN_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    log "SECURITY: pin SHA missing or malformed ('$PIN_SHA') - a bare tag is not a"
    log "valid pin (tags can be force-moved). Refusing to fetch."
    exit 3
fi

# --- frozen? already current? ----------------------------------------------
lock_field() {
    python3 - "$LOCK_FILE" "$1" <<'PY' 2>/dev/null || true
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception:
    pass
PY
}

if [[ -f "$LOCK_FILE" ]]; then
    if [[ "$(lock_field frozen)" == "True" ]]; then
        log "lockfile is frozen - skipping fetch entirely"
        exit 0
    fi
    if [[ "$(lock_field commit)" == "$PIN_SHA" && -d "$PLUGINS_FETCH_ROOT" ]]; then
        log "already current at $PIN_TAG ($PIN_SHA) - no-op"
        exit 0
    fi
fi

# --- resolve tag remotely and verify against the pin ------------------------
log "resolving $PLUGINS_REPO tag $PIN_TAG ..."
resolved=""
if refs=$(git ls-remote "https://github.com/$PLUGINS_REPO.git" \
        "refs/tags/$PIN_TAG^{}" "refs/tags/$PIN_TAG" 2>/dev/null); then
    # Prefer the peeled ref (annotated tag -> commit), fall back to the tag ref.
    resolved=$(awk '$2 ~ /\^\{\}$/ {print $1; exit}' <<<"$refs")
    [[ -z "$resolved" ]] && resolved=$(awk 'NR==1 {print $1}' <<<"$refs")
fi

if [[ -z "$resolved" ]]; then
    if [[ -d "$PLUGINS_FETCH_ROOT" && -f "$LOCK_FILE" ]]; then
        log "WARN: cannot reach $PLUGINS_REPO (offline or repo missing) - keeping previous fetched tree"
        exit 0
    fi
    log "WARN: cannot reach $PLUGINS_REPO and nothing fetched yet - baked plugins stay active"
    exit 0
fi

if [[ "$resolved" != "$PIN_SHA" ]]; then
    log "SECURITY: tag $PIN_TAG resolves to $resolved but the pin says $PIN_SHA."
    log "The tag has MOVED since it was pinned. Refusing to fetch - previous tree untouched."
    log "If the move is legitimate, update PLUGINS_PIN in a reviewed chassis PR."
    exit 3
fi

# --- download tarball at the pinned SHA -------------------------------------
parent=$(dirname "$PLUGINS_FETCH_ROOT")
mkdir -p "$parent"
staging=$(mktemp -d "$parent/.plugins-staging.XXXXXX")
cleanup() { rm -rf "$staging" 2>/dev/null || true; }
trap cleanup EXIT

log "downloading $PLUGINS_REPO @ $PIN_SHA ..."
fetched=false
if curl -fsSL --retry 2 "https://codeload.github.com/$PLUGINS_REPO/tar.gz/$PIN_SHA" \
        -o "$staging/repo.tar.gz" 2>/dev/null; then
    tar -xzf "$staging/repo.tar.gz" -C "$staging" && fetched=true
    rm -f "$staging/repo.tar.gz"
fi
if [[ "$fetched" != "true" ]]; then
    log "tarball download failed - falling back to git clone"
    if git clone --quiet "https://github.com/$PLUGINS_REPO.git" "$staging/clone" 2>/dev/null \
        && git -C "$staging/clone" checkout --quiet "$PIN_SHA" 2>/dev/null; then
        rm -rf "$staging/clone/.git"
        fetched=true
    fi
fi
if [[ "$fetched" != "true" ]]; then
    log "WARN: fetch failed - keeping previous state"
    exit 0
fi

# Locate the extracted tree root (codeload roots at <repo>-<sha>/).
tree=$(find "$staging" -mindepth 1 -maxdepth 1 -type d | head -1)
if [[ -z "$tree" || ! -f "$tree/registry.json" ]]; then
    log "ERROR: staging tree is corrupt (no registry.json) - aborting, previous state untouched"
    exit 4
fi

# --- sanity check: every registry plugin present, every manifest parses -----
if ! python3 - "$tree" <<'PY'
import json, sys
from pathlib import Path
tree = Path(sys.argv[1])
reg = json.loads((tree / "registry.json").read_text())
ok = True
for p in reg.get("plugins", []):
    pdir = tree / p.get("path", p["name"])
    manifest = pdir / "openclaw.plugin.json"
    if not pdir.is_dir():
        print(f"missing plugin dir: {pdir}", file=sys.stderr); ok = False; continue
    if not manifest.is_file():
        print(f"missing manifest: {manifest}", file=sys.stderr); ok = False; continue
    try:
        json.loads(manifest.read_text())
    except Exception as e:
        print(f"manifest parse error {manifest}: {e}", file=sys.stderr); ok = False
sys.exit(0 if ok else 1)
PY
then
    log "ERROR: sanity check failed - aborting swap, previous state untouched"
    exit 4
fi

# --- atomic swap ------------------------------------------------------------
previous=""
if [[ -d "$PLUGINS_FETCH_ROOT" ]]; then
    previous="$parent/.vendored-plugins.previous.$$"
    mv "$PLUGINS_FETCH_ROOT" "$previous"
fi
if ! mv "$tree" "$PLUGINS_FETCH_ROOT"; then
    log "ERROR: swap failed - restoring previous tree"
    [[ -n "$previous" ]] && mv "$previous" "$PLUGINS_FETCH_ROOT"
    exit 4
fi
[[ -n "$previous" ]] && rm -rf "$previous"

# --- write lockfile ---------------------------------------------------------
CHASSIS_VERSION="unknown"
for vf in "$CUSTOMER_HOME/chassis/chassis/VERSION" "$CHASSIS_ROOT/VERSION"; do
    [[ -f "$vf" ]] && { CHASSIS_VERSION=$(<"$vf"); break; }
done

PIN_TAG="$PIN_TAG" PIN_SHA="$PIN_SHA" PLUGINS_REPO="$PLUGINS_REPO" \
CHASSIS_VERSION="$CHASSIS_VERSION" FETCH_ROOT="$PLUGINS_FETCH_ROOT" \
python3 - "$LOCK_FILE" <<'PY'
import json, os, sys, datetime
from pathlib import Path
lock_path = sys.argv[1]
root = Path(os.environ["FETCH_ROOT"])
tag, sha = os.environ["PIN_TAG"], os.environ["PIN_SHA"]
plugins = {}
for manifest in sorted(root.glob("*/openclaw.plugin.json")):
    try:
        data = json.loads(manifest.read_text())
    except Exception:
        continue
    name = manifest.parent.name
    plugins[name] = {
        "version": data.get("version", "unknown"),
        "tag": tag,
        "sha": sha,
    }
lock = {
    "schema": 1,
    "repo": os.environ["PLUGINS_REPO"],
    "tag": tag,
    "commit": sha,
    "fetched_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "chassis_version": os.environ["CHASSIS_VERSION"].strip(),
    "frozen": False,
    "plugins": plugins,
}
with open(lock_path, "w") as f:
    json.dump(lock, f, indent=2)
    f.write("\n")
PY

log "fetched $PLUGINS_REPO @ $PIN_TAG ($PIN_SHA) into $PLUGINS_FETCH_ROOT"
log "lockfile written: $LOCK_FILE"
