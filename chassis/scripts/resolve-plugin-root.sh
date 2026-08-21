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
# Composed symlinks are written RELATIVE to $CUSTOMER_HOME, not absolute.
# CUSTOMER_HOME is the same directory on both sides of the host/container
# bind mount but is reached through a different absolute path on each side,
# so an absolute symlink written on one side dangles when read from the
# other. A relative link resolves correctly through either mount path. This
# does not help the baked tree when it lives outside CUSTOMER_HOME (i.e.
# /app/plugins, container-only, never bind-mounted) - that half of the
# overlay is expected to differ per side and is recorded via built_on below.
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
# recording mode, roots, per-plugin provenance, and built_on (host or
# container - whichever side ran this resolution), so a disagreement between
# the two views of the composed tree is visible after the fact.
#
# Env seams:
#   CHASSIS_BAKED_PLUGINS_ROOT - pin the baked tree to a single explicit
#       root, bypassing the union below entirely (operator override, tests
#       with a non-standard layout).
#   CHASSIS_IMAGE_PLUGINS_ROOT - override the image-baked half of the union
#       (tests only; real installs always have this at /app/plugins, which
#       is the default). The other half is $CHASSIS_HOME/plugins, already
#       reachable via CHASSIS_HOME in tests.
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

# Populated by the baked-root probe below. Declared here (empty) so
# write_state can always expand it under `set -u`, even from the explicit
# operator-override branch, which returns before the probe runs.
BAKED_ROOTS=()

log() { printf '[resolve-plugin-root] %s\n' "$*" >&2; }

usable() {
    local dir="$1"
    [[ -d "$dir" ]] || return 1
    compgen -G "$dir"/*/openclaw.plugin.json > /dev/null 2>&1
}

# relpath TARGET BASE - relative path from BASE to TARGET, POSIX-portable
# (host is macOS/BSD, container is Linux; neither `realpath --relative-to`
# nor GNU-only readlink flags can be assumed on both, so this shells out to
# python3, already a hard dependency of this script via write_state).
relpath() {
    python3 -c 'import os, sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$1" "$2"
}

# resolved_path PATH - realpath, following symlinks. Used instead of `readlink`
# so the post-compose assertion below compares fully-resolved absolute paths
# rather than raw (now relative) link targets.
resolved_path() {
    python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

# Which side is resolving right now. Same real /app/plugins probe the
# image-baked half of the union below defaults to (container-only) -
# recorded so a disagreement between the host and container view of
# state/plugins-root is visible after the fact instead of just dangling.
if [[ -d /app/plugins ]]; then
    BUILT_ON="container"
else
    BUILT_ON="host"
fi

STATE_FILE="$CUSTOMER_HOME/plugins-root.state.json"

# MODE is one of: explicit | baked | overlay
# PROVENANCE lines are "name<TAB>baked|fetched" pairs for the state file.
write_state() {
    local mode="$1" root="$2" error="${3:-}"
    MODE="$mode" ROOT="$root" ERROR="$error" \
    BAKED="${BAKED_ROOTS[0]:-}" FETCHED="${FETCHED_ROOT:-}" \
    BAKED_ROOTS_LIST="$(printf '%s\n' "${BAKED_ROOTS[@]:-}")" \
    PROVENANCE="${PROVENANCE:-}" BUILT_ON="$BUILT_ON" \
    python3 - "$STATE_FILE" <<'PY' 2>/dev/null || log "WARN: could not write $STATE_FILE"
import datetime, json, os, sys
prov = {}
for line in os.environ.get("PROVENANCE", "").splitlines():
    if "\t" in line:
        name, src = line.split("\t", 1)
        prov[name] = src
baked_roots = [r for r in os.environ.get("BAKED_ROOTS_LIST", "").splitlines() if r]
state = {
    "schema": 1,
    "mode": os.environ["MODE"],
    "resolved_root": os.environ["ROOT"],
    "baked_root": os.environ["BAKED"] or None,
    "baked_roots": baked_roots,
    "fetched_root": os.environ["FETCHED"] or None,
    "resolved_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "built_on": os.environ["BUILT_ON"],
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
    BAKED_ROOTS=() FETCHED_ROOT="${CHASSIS_PLUGINS_FETCH_ROOT:-$CUSTOMER_HOME/vendored-plugins}"
    if usable "$FETCHED_ROOT" && [[ "$CHASSIS_PLUGINS_ROOT" != "$FETCHED_ROOT" ]]; then
        log "operator override: CHASSIS_PLUGINS_ROOT=$CHASSIS_PLUGINS_ROOT (explicitly set)"
        log "note: a usable fetched tree exists at $FETCHED_ROOT and is being ignored BY EXPLICIT CHOICE"
    fi
    write_state explicit "$CHASSIS_PLUGINS_ROOT"
    printf '%s\n' "$CHASSIS_PLUGINS_ROOT"
    exit 0
fi

# --- locate the baked root(s) -----------------------------------------------
# /app/plugins (image-baked, container-only) and $CHASSIS_HOME/plugins
# (host-side legacy layout) can both exist and hold DISJOINT plugin sets.
# Picking one by elif branch order (the pre-#163 behaviour) meant the two
# sides of the same bind mount answered differently and both exited 0 -
# whichever plugin only lived in the root that lost silently vanished. Union
# both instead, same shape as the baked-then-fetched overlay below: base
# first, later root overlaid on top so a same-named plugin in the later root
# wins. CHASSIS_BAKED_PLUGINS_ROOT is an explicit operator override and stays
# a single root, not part of the union - explicit still beats inferred.
if [[ -n "${CHASSIS_BAKED_PLUGINS_ROOT:-}" && -d "${CHASSIS_BAKED_PLUGINS_ROOT}" ]]; then
    BAKED_ROOTS=("$CHASSIS_BAKED_PLUGINS_ROOT")
else
    IMAGE_BAKED_ROOT="${CHASSIS_IMAGE_PLUGINS_ROOT:-/app/plugins}"
    [[ -d "$IMAGE_BAKED_ROOT" ]] && BAKED_ROOTS+=("$IMAGE_BAKED_ROOT")
    [[ -n "${CHASSIS_HOME:-}" && -d "$CHASSIS_HOME/plugins" ]] && BAKED_ROOTS+=("$CHASSIS_HOME/plugins")
fi

FETCHED_ROOT="${CHASSIS_PLUGINS_FETCH_ROOT:-$CUSTOMER_HOME/vendored-plugins}"

# --- zero or one baked root, no usable fetched tree: pass through directly --
# Exactly the pre-#82 behaviour (no filesystem writes) for the common case.
# Two baked roots, or a usable fetched tree, both need a composed union and
# fall through to the overlay below instead.
if ! usable "$FETCHED_ROOT" && [[ "${#BAKED_ROOTS[@]}" -le 1 ]]; then
    BAKED_ROOT="${BAKED_ROOTS[0]:-}"
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
    # Base first, each later root overlaid on top - same rm-then-relink
    # pattern the fetched loop below uses, so a same-named plugin in a
    # higher-precedence baked root wins instead of colliding.
    for br in "${BAKED_ROOTS[@]:-}"; do
        [[ -n "$br" ]] || continue
        for d in "$br"/*/; do
            [[ -d "$d" ]] || continue
            name=$(basename "$d")
            rm -f "$staging/$name"
            ln -s "$(relpath "${d%/}" "$staging")" "$staging/$name"
            PROVENANCE=$(printf '%s' "$PROVENANCE" | grep -v "^$name"$'\t' || true)
            PROVENANCE+=$'\n'"$name"$'\t'"baked"$'\n'
        done
    done
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
        ln -s "$(relpath "${d%/}" "$staging")" "$staging/$name"
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
    # The union/overlay is usable but we cannot activate it. Fall back to the
    # single highest-precedence baked root and fail LOUDLY - this is the
    # exact silent-no-op class v0.2.0 shipped, so it must never pass quietly.
    fallback_root="${BAKED_ROOTS[-1]:-}"
    log "ERROR: composed plugin tree (baked union and/or fetched overlay from $FETCHED_ROOT) is usable but could NOT be activated (compose failed under $CUSTOMER_HOME/state)"
    write_state baked "$fallback_root" "compose failed - overlay present but not active"
    printf '%s\n' "$fallback_root"
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
    target=$(resolved_path "$COMPOSED_ROOT/$name" 2>/dev/null || true)
    if [[ "$target" != "$(resolved_path "${d%/}")" ]]; then
        assert_error="fetched plugin '$name' is not active in $COMPOSED_ROOT (resolves to '${target:-missing}')"
        log "ERROR: PLUGIN ROOT ASSERTION FAILED - $assert_error"
        break
    fi
done

write_state overlay "$COMPOSED_ROOT" "$assert_error"
printf '%s\n' "$COMPOSED_ROOT"
[[ -z "$assert_error" ]] || exit 5
exit 0
