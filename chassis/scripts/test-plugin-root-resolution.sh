#!/bin/bash
# test-plugin-root-resolution.sh - Unit tests for resolve-plugin-root.sh and
# the _env.sh plugin-root wiring.
#
# The bug these lock down
# =======================
# Chassis v0.2.0 shipped "plugins move from image-baked to fetched-at-boot".
# The fetch worked; the switch never happened. _env.sh's fetched-tree
# preference sat behind `[[ -z "${CHASSIS_PLUGINS_ROOT:-}" ]]`, and both the
# Dockerfile ENV and docker/entrypoint.sh pre-set that variable to
# /app/plugins before _env.sh could ever run. Every install stayed on the
# baked tree while vendored-plugins/ sat ignored on disk. All existing tests
# and CI passed while the feature did nothing.
#
# The core assertion here is therefore behavioural: given a usable fetched
# tree, the RESOLVED root must actually serve the fetched copy of each
# fetched plugin, while plugins that exist only in the baked tree keep
# loading (overlay, not wholesale replacement).
#
# No docker daemon, no network. Everything runs against temp directories.
#
# Exit codes:
#   0 - all scenarios passed
#   1 - one or more scenarios failed
#   2 - test harness itself broke

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOLVER="${SCRIPT_DIR}/resolve-plugin-root.sh"
ENV_SH="${SCRIPT_DIR}/_env.sh"

for f in "$RESOLVER" "$ENV_SH"; do
    if [[ ! -f "$f" ]]; then
        echo "test-plugin-root-resolution: missing $f" >&2
        exit 2
    fi
done

fail=0
pass=0

check() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass=$((pass + 1))
    else
        echo "FAIL [$name] expected '$expected', got '$actual'"
        fail=$((fail + 1))
    fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

manifest() {
    # manifest <dir> <name> <version>
    mkdir -p "$1"
    printf '{"id": "%s", "version": "%s"}\n' "$2" "$3" > "$1/openclaw.plugin.json"
}

state_field() {
    # state_field <customer_home> <jq-ish path expr for python>
    python3 - "$1/plugins-root.state.json" "$2" <<'PY' 2>/dev/null
import json, sys
data = json.load(open(sys.argv[1]))
cur = data
for key in sys.argv[2].split("."):
    cur = cur.get(key) if isinstance(cur, dict) else None
print("" if cur is None else cur)
PY
}

fresh_env() {
    # fresh_env <label> -> sets CUSTOMER, BAKED globals and builds the
    # standard 7-plugin baked tree.
    CUSTOMER="$TMP/$1/customer"
    BAKED="$TMP/$1/baked"
    mkdir -p "$CUSTOMER" "$BAKED"
    local p
    for p in angel-protocol bfl dating loom-vision remarkable restaurant-booking whatsapp; do
        manifest "$BAKED/$p" "$p" "0.1.0"
    done
}

run_resolver() {
    # -> RESOLVED_ROOT, RESOLVER_RC
    RESOLVED_ROOT="$(env -u CHASSIS_PLUGINS_ROOT \
        CUSTOMER_HOME="$CUSTOMER" CHASSIS_HOME="$CUSTOMER" \
        CHASSIS_BAKED_PLUGINS_ROOT="$BAKED" \
        bash "$RESOLVER" 2>>"$TMP/resolver.log")"
    RESOLVER_RC=$?
}

# --- scenario 1: no fetched tree -> baked root, all plugins present ---------
fresh_env s1
run_resolver
check "s1 exit" "0" "$RESOLVER_RC"
check "s1 root is baked" "$BAKED" "$RESOLVED_ROOT"
check "s1 plugin count" "7" "$(ls -d "$RESOLVED_ROOT"/*/ | wc -l | tr -d ' ')"
check "s1 state mode" "baked" "$(state_field "$CUSTOMER" mode)"

# --- scenario 2: fetched tree present -> overlay, fetched copy wins ---------
# THE regression test for the v0.2.0 no-op. The fetched tree mirrors the real
# behalfbot-plugins layout: one plugin plus non-plugin top-level dirs.
fresh_env s2
FETCHED="$CUSTOMER/vendored-plugins"
manifest "$FETCHED/loom-vision" "loom-vision" "0.2.0"
mkdir -p "$FETCHED/docs" "$FETCHED/tools"
echo '{}' > "$FETCHED/registry.json"
run_resolver
check "s2 exit" "0" "$RESOLVER_RC"
check "s2 root is composed" "$CUSTOMER/state/plugins-root" "$RESOLVED_ROOT"
check "s2 plugin count" "7" "$(ls -d "$RESOLVED_ROOT"/*/ | wc -l | tr -d ' ')"
check "s2 fetched plugin is ACTIVE" "0.2.0" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$RESOLVED_ROOT/loom-vision/openclaw.plugin.json")"
check "s2 baked-only plugin still loads" "0.1.0" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$RESOLVED_ROOT/bfl/openclaw.plugin.json")"
check "s2 non-plugin dirs filtered" "no" "$([[ -e "$RESOLVED_ROOT/docs" || -e "$RESOLVED_ROOT/tools" ]] && echo yes || echo no)"
check "s2 state mode" "overlay" "$(state_field "$CUSTOMER" mode)"
check "s2 provenance loom-vision" "fetched" "$(state_field "$CUSTOMER" plugins.loom-vision)"
check "s2 provenance bfl" "baked" "$(state_field "$CUSTOMER" plugins.bfl)"
check "s2 no error recorded" "" "$(state_field "$CUSTOMER" error)"

# --- scenario 3: empty fetched dir must not shadow the baked tree -----------
fresh_env s3
mkdir -p "$CUSTOMER/vendored-plugins"
run_resolver
check "s3 exit" "0" "$RESOLVER_RC"
check "s3 root is baked" "$BAKED" "$RESOLVED_ROOT"

# --- scenario 4: fetched plugin without a manifest keeps the baked copy -----
fresh_env s4
FETCHED="$CUSTOMER/vendored-plugins"
manifest "$FETCHED/loom-vision" "loom-vision" "0.2.0"
mkdir -p "$FETCHED/bfl"   # half-written: dir exists, no manifest
run_resolver
check "s4 exit" "0" "$RESOLVER_RC"
check "s4 half-written plugin stays baked" "0.1.0" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$RESOLVED_ROOT/bfl/openclaw.plugin.json")"
check "s4 usable fetched plugin still wins" "0.2.0" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$RESOLVED_ROOT/loom-vision/openclaw.plugin.json")"

# --- scenario 5: fetched-only plugin appears in the resolved set ------------
fresh_env s5
FETCHED="$CUSTOMER/vendored-plugins"
manifest "$FETCHED/brand-new" "brand-new" "1.0.0"
run_resolver
check "s5 exit" "0" "$RESOLVER_RC"
check "s5 plugin count" "8" "$(ls -d "$RESOLVED_ROOT"/*/ | wc -l | tr -d ' ')"
check "s5 new plugin present" "1.0.0" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$RESOLVED_ROOT/brand-new/openclaw.plugin.json")"

# --- scenario 6: operator override wins verbatim ----------------------------
fresh_env s6
FETCHED="$CUSTOMER/vendored-plugins"
manifest "$FETCHED/loom-vision" "loom-vision" "0.2.0"
RESOLVED_ROOT="$(CHASSIS_PLUGINS_ROOT="/operator/choice" \
    CUSTOMER_HOME="$CUSTOMER" CHASSIS_HOME="$CUSTOMER" \
    CHASSIS_BAKED_PLUGINS_ROOT="$BAKED" \
    bash "$RESOLVER" 2>>"$TMP/resolver.log")"
RESOLVER_RC=$?
check "s6 exit" "0" "$RESOLVER_RC"
check "s6 override honoured" "/operator/choice" "$RESOLVED_ROOT"
check "s6 state mode" "explicit" "$(state_field "$CUSTOMER" mode)"

# --- scenario 7: rerun is idempotent and drops stale entries ----------------
fresh_env s7
FETCHED="$CUSTOMER/vendored-plugins"
manifest "$FETCHED/loom-vision" "loom-vision" "0.2.0"
run_resolver
first="$RESOLVED_ROOT"
rm -rf "$FETCHED/loom-vision"
manifest "$FETCHED/other" "other" "0.3.0"
run_resolver
check "s7 stable path" "$first" "$RESOLVED_ROOT"
check "s7 stale fetched link replaced by baked" "0.1.0" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$RESOLVED_ROOT/loom-vision/openclaw.plugin.json")"
check "s7 new fetched plugin picked up" "0.3.0" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$RESOLVED_ROOT/other/openclaw.plugin.json")"

# --- scenario 8: compose failure fails LOUDLY (exit 5), never silently ------
if [[ "$(id -u)" != "0" ]]; then
    fresh_env s8
    FETCHED="$CUSTOMER/vendored-plugins"
    manifest "$FETCHED/loom-vision" "loom-vision" "0.2.0"
    mkdir -p "$CUSTOMER/state" && chmod 500 "$CUSTOMER/state"
    run_resolver
    chmod 755 "$CUSTOMER/state"
    check "s8 loud failure exit" "5" "$RESOLVER_RC"
    check "s8 falls back to baked" "$BAKED" "$RESOLVED_ROOT"
else
    echo "SKIP [s8] running as root - permission-based failure not simulable"
fi

# --- scenario 9: _env.sh end to end exports the overlay root ----------------
# This is the integration the v0.2.0 release believed it had.
fresh_env s9
FETCHED="$CUSTOMER/vendored-plugins"
manifest "$FETCHED/loom-vision" "loom-vision" "0.2.0"
RESOLVED_ROOT="$(env -u CHASSIS_PLUGINS_ROOT -u CHASSIS_ROOT \
    CUSTOMER_HOME="$CUSTOMER" CHASSIS_HOME="$CUSTOMER" \
    CHASSIS_BAKED_PLUGINS_ROOT="$BAKED" \
    bash -c "source '$ENV_SH' && printf '%s' \"\$CHASSIS_PLUGINS_ROOT\"" 2>>"$TMP/resolver.log")"
check "s9 _env.sh resolves overlay" "$CUSTOMER/state/plugins-root" "$RESOLVED_ROOT"
check "s9 fetched active via _env.sh" "0.2.0" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$RESOLVED_ROOT/loom-vision/openclaw.plugin.json")"

# ---------------------------------------------------------------------------
echo
echo "test-plugin-root-resolution: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]] || exit 1
exit 0
