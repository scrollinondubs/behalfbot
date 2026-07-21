#!/usr/bin/env bash
# resolve-plugin-root.sh - resolve the effective plugin root as an OVERLAY of
# the image-baked tree and the fetched (vendored) tree.
#
# Fixes the v0.2.0 defect where the fetched-tree preference in _env.sh was
# unreachable: the Dockerfile ENV and the entrypoint both pre-set
# CHASSIS_PLUGINS_ROOT before _env.sh's [[ -z ]] guard ever ran, so every
# install silently stayed on the baked tree while vendored-plugins/ sat
# ignored on disk.
#
# Why an overlay and not a straight preference: the two trees are not
# interchangeable. The baked tree carries plugins that were never published
# to behalfbot-plugins (7 today), while the fetched tree currently publishes
# 1. Selecting one tree wholesale would either ignore fetches (the v0.2.0
# bug) or drop six working plugins (the naive fix). Resolution is therefore
# per plugin NAME: a usable fetched copy wins, anything only baked still
# loads, and a failed or partial fetch degrades per plugin instead of wiping
# the set.
#
# Mechanism: every consumer of CHASSIS_PLUGINS_ROOT treats it as a single
# directory (entrypoint install-plugin, smoke-test's `for dir in $ROOT/*`,
# plugin script paths), so the overlay is materialised as a composed
# directory of symlinks at $CUSTOMER_HOME/state/plugins-root, rebuilt from
# scratch and swapped into place whole. Consumers keep their single-root
# contract unchanged. A resolver-plus-search-path list was the alternative
# and was rejected because it would push merge logic into every consumer,
# including plain globs. Composing also filters the non-plugin dirs the
# fetched tree carries at its top level (docs/, tools/, registry.json).
#
# Operator override contract: if CHASSIS_PLUGINS_ROOT is already set when
# this script runs, that value is honoured VERBATIM - no overlay, no
# reordering. The Dockerfile and entrypoint no longer default the variable,
# precisely so that "set" reliably means "an operator set it" (compose
# environment, docker -e, or the customer .env). The chassis default path is
# "unset", which lands here.
#
# Safety property (kept from v0.2.0): a directory only counts as a usable
# plugin source if it contains at least one */openclaw.plugin.json, and a
# fetched plugin dir without a manifest never shadows the baked copy. An
# empty or half-written fetch degrades to baked, per plugin.
#
# Output: the resolved root on stdout. All logging goes to stderr. Writes
# $CUSTOMER_HOME/plugins-root.state.json (adjacent to plugins.lock)
# recording mode, roots, and per-plugin provenance.
#
# Env seams:
#   CHASSIS_BAKED_PLUGINS_ROOT - override the baked-tree location (tests,
#       host installs with a non-standard layout). Defaults to /app/plugins,
#       then $CHASSIS_HOME/plugins.
#   CHASSIS_PLUGINS_FETCH_ROOT - override the fetched-tree location.
#       Defaults to $CUSTOMER_HOME/vendored-plugins (matches fetch-plugins.sh).
#
# Exit codes:
#   0 - resolved (any mode)
#   5 - ASSERTION FAILED: a usable fetched tree exists but its plugins are
#       not active in the resolved root. The best-available root is still
#       printed and the state file records the error. Callers must surface
#       this loudly; it exists so the v0.2.0 silent no-op cannot recur.

set -uo pipefail

: "${CUSTOMER_HOME:=${CHASSIS_HOME:-/app/customer}}"

log() { printf '[resolve-plugin-root] %s\n' "$*" >&2; }

usable() {
    local dir="$1"
    [[ -d "$dir" ]] || return 1
    compgen -G "$dir"/*/openclaw.plugin.json > /dev/null 2>&1
}

STATE_FILE="$CUSTOMER_HOME/plugins-root.state.json"

# MODE is one of: explicit | baked | overlay
# PROVENANCE lines are "name<TAB>baked|fetched" pairs for the state file.
write_state() {
    local mode="$1" root="$2" error="${3:-}"
    MODE="$mode" ROOT="$root" ERROR="$error" \
    BAKED="${BAKED_ROOT:-}" FETCHED="${FETCHED_ROOT:-}" \
    PROVENANCE="${PROVENANCE:-}" \
    python3 - "$STATE_FILE" <<'PY' 2>/dev/null || log "WARN: could not write $STATE_FILE"
import datetime, json, os, sys
prov = {}
for line in os.environ.get("PROVENANCE", "").splitlines():
    if "\t" in line:
        name, src = line.split("\t", 1)
        prov[name] = src
state = {
    "schema": 1,
    "mode": os.environ["MODE"],
    "resolved_root": os.environ["ROOT"],
    "baked_root": os.environ["BAKED"] or None,
    "fetched_root": os.environ["FETCHED"] or None,
    "resolved_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "plugins": prov,
    "error": os.environ["ERROR"] or None,
}
with open(sys.argv[1], "w") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
PY
}

# --- operator override: set means set, honour verbatim ----------------------
if [[ -n "${CHASSIS_PLUGINS_ROOT:-}" ]]; then
    BAKED_ROOT="" FETCHED_ROOT="${CHASSIS_PLUGINS_FETCH_ROOT:-$CUSTOMER_HOME/vendored-plugins}"
    if usable "$FETCHED_ROOT" && [[ "$CHASSIS_PLUGINS_ROOT" != "$FETCHED_ROOT" ]]; then
        log "operator override: CHASSIS_PLUGINS_ROOT=$CHASSIS_PLUGINS_ROOT (explicitly set)"
        log "note: a usable fetched tree exists at $FETCHED_ROOT and is being ignored BY EXPLICIT CHOICE"
    fi
    write_state explicit "$CHASSIS_PLUGINS_ROOT"
    printf '%s\n' "$CHASSIS_PLUGINS_ROOT"
    exit 0
fi

# --- locate the two source trees --------------------------------------------
BAKED_ROOT=""
if [[ -n "${CHASSIS_BAKED_PLUGINS_ROOT:-}" && -d "${CHASSIS_BAKED_PLUGINS_ROOT}" ]]; then
    BAKED_ROOT="$CHASSIS_BAKED_PLUGINS_ROOT"
elif [[ -d /app/plugins ]]; then
    BAKED_ROOT="/app/plugins"
elif [[ -n "${CHASSIS_HOME:-}" && -d "$CHASSIS_HOME/plugins" ]]; then
    BAKED_ROOT="$CHASSIS_HOME/plugins"
fi

FETCHED_ROOT="${CHASSIS_PLUGINS_FETCH_ROOT:-$CUSTOMER_HOME/vendored-plugins}"

# --- no usable fetched tree: baked only, exactly the pre-#82 behaviour ------
if ! usable "$FETCHED_ROOT"; then
    PROVENANCE=""
    if [[ -n "$BAKED_ROOT" ]]; then
        for d in "$BAKED_ROOT"/*/; do
            [[ -d "$d" ]] || continue
            PROVENANCE+="$(basename "$d")"$'\t'"baked"$'\n'
        done
    fi
    write_state baked "$BAKED_ROOT"
    printf '%s\n' "$BAKED_ROOT"
    exit 0
fi

# --- compose the overlay -----------------------------------------------------
COMPOSED_ROOT="$CUSTOMER_HOME/state/plugins-root"
compose_failed=""

staging=""
if mkdir -p "$CUSTOMER_HOME/state" 2>/dev/null; then
    staging=$(mktemp -d "$CUSTOMER_HOME/state/.plugins-root.XXXXXX" 2>/dev/null) || staging=""
fi

PROVENANCE=""
if [[ -n "$staging" ]]; then
    if [[ -n "$BAKED_ROOT" ]]; then
        for d in "$BAKED_ROOT"/*/; do
            [[ -d "$d" ]] || continue
            ln -s "${d%/}" "$staging/$(basename "$d")"
            PROVENANCE+="$(basename "$d")"$'\t'"baked"$'\n'
        done
    fi
    for d in "$FETCHED_ROOT"/*/; do
        [[ -d "$d" ]] || continue
        name=$(basename "$d")
        if [[ ! -f "$d/openclaw.plugin.json" ]]; then
            # docs/, tools/, or a half-written plugin: never shadows baked.
            if [[ -e "$staging/$name" ]]; then
                log "fetched $name has no manifest - keeping the baked copy"
            fi
            continue
        fi
        rm -f "$staging/$name"
        ln -s "${d%/}" "$staging/$name"
        PROVENANCE=$(printf '%s' "$PROVENANCE" | grep -v "^$name"$'\t' || true)
        PROVENANCE+=$'\n'"$name"$'\t'"fetched"$'\n'
    done
    # Swap whole so consumers never see a half-built root.
    old=""
    if [[ -e "$COMPOSED_ROOT" ]]; then
        old="$CUSTOMER_HOME/state/.plugins-root.old.$$"
        mv "$COMPOSED_ROOT" "$old" 2>/dev/null || { rm -rf "$staging"; compose_failed=yes; }
    fi
    if [[ -z "$compose_failed" ]]; then
        if mv "$staging" "$COMPOSED_ROOT" 2>/dev/null; then
            [[ -n "$old" ]] && rm -rf "$old"
        else
            [[ -n "$old" ]] && mv "$old" "$COMPOSED_ROOT" 2>/dev/null
            rm -rf "$staging"
            compose_failed=yes
        fi
    fi
else
    compose_failed=yes
fi

if [[ -n "$compose_failed" ]]; then
    # Fetched tree is usable but we cannot activate it. Fall back to baked
    # (the larger set) and fail LOUDLY - this is the exact silent-no-op class
    # v0.2.0 shipped, so it must never pass quietly.
    log "ERROR: fetched plugin tree at $FETCHED_ROOT is usable but could NOT be activated (compose failed under $CUSTOMER_HOME/state)"
    write_state baked "$BAKED_ROOT" "compose failed - fetched tree present but not active"
    printf '%s\n' "$BAKED_ROOT"
    exit 5
fi

# --- boot-time assertion: every usable fetched plugin must be active --------
# This check exists because v0.2.0's fetched-tree preference shipped fully
# inert while every artifact looked correct and CI stayed green. If it fires,
# the resolver itself has regressed.
assert_error=""
for d in "$FETCHED_ROOT"/*/; do
    [[ -f "$d/openclaw.plugin.json" ]] || continue
    name=$(basename "$d")
    target=$(readlink "$COMPOSED_ROOT/$name" 2>/dev/null || true)
    if [[ "$target" != "${d%/}" ]]; then
        assert_error="fetched plugin '$name' is not active in $COMPOSED_ROOT (resolves to '${target:-missing}')"
        log "ERROR: PLUGIN ROOT ASSERTION FAILED - $assert_error"
        break
    fi
done

write_state overlay "$COMPOSED_ROOT" "$assert_error"
printf '%s\n' "$COMPOSED_ROOT"
[[ -z "$assert_error" ]] || exit 5
exit 0
