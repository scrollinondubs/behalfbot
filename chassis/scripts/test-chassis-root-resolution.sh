#!/bin/bash
# test-chassis-root-resolution.sh - Unit tests for resolve-chassis-root.sh and
# the _env.sh chassis-root wiring.
#
# The bug these lock down
# =======================
# A containerized install carries two chassis trees: the image-baked copy at
# /app/chassis and the operator's live clone bind-mounted (or vendored) at
# $CUSTOMER_HOME/chassis/chassis. CHASSIS_ROOT was baked as Dockerfile ENV
# pointing at the baked copy, so `git pull` on the host updated the disk while
# the runtime kept executing stale code - stale .mcp.json.template, stale
# scripts, stale dispatcher - and reported nothing. Same defect class as the
# v0.2.0 plugins no-op locked down by test-plugin-root-resolution.sh.
#
# The core assertion is behavioural: given a usable live tree, the RESOLVED
# root must BE that tree, the out-of-band symlink must point at it, and a torn
# or cross-MAJOR live tree must force baked LOUDLY (exit 5), never silently.
#
# No docker daemon, no network. Everything runs against temp directories.
#
# Exit codes:
#   0 - all scenarios passed
#   1 - one or more scenarios failed
#   2 - test harness itself broke

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOLVER="${SCRIPT_DIR}/resolve-chassis-root.sh"
ENV_SH="${SCRIPT_DIR}/_env.sh"

for f in "$RESOLVER" "$ENV_SH"; do
    if [[ ! -f "$f" ]]; then
        echo "test-chassis-root-resolution: missing $f" >&2
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

mk_tree() {
    # mk_tree <dir> <version> - a minimal tree that passes usable()
    mkdir -p "$1/scripts" "$1/scheduled-tasks"
    printf '%s\n' "$2" > "$1/VERSION"
    printf '#!/bin/bash\n' > "$1/scheduled-tasks/heartbeat-dispatcher.sh"
}

state_field() {
    # state_field <customer_home> <dot.path>
    python3 - "$1/chassis-root.state.json" "$2" <<'PY' 2>/dev/null
import json, sys
data = json.load(open(sys.argv[1]))
cur = data
for key in sys.argv[2].split("."):
    cur = cur.get(key) if isinstance(cur, dict) else None
print("" if cur is None else cur)
PY
}

fresh_env() {
    # fresh_env <label> [baked-version] -> sets CUSTOMER, BAKED, LIVE globals
    CUSTOMER="$TMP/$1/customer"
    BAKED="$TMP/$1/baked"
    LIVE="$CUSTOMER/chassis/chassis"
    mkdir -p "$CUSTOMER"
    mk_tree "$BAKED" "${2:-0.3.0}"
}

run_resolver() {
    # -> RESOLVED_ROOT, RESOLVER_RC
    RESOLVED_ROOT="$(env -u CHASSIS_ROOT \
        CUSTOMER_HOME="$CUSTOMER" CHASSIS_HOME="$CUSTOMER" \
        CHASSIS_BAKED_TREE_ROOT="$BAKED" \
        bash "$RESOLVER" 2>>"$TMP/resolver.log")"
    RESOLVER_RC=$?
}

link_target() {
    readlink "$CUSTOMER/state/chassis-root" 2>/dev/null || true
}

# --- scenario 1: no live tree -> baked, exactly the pre-fix behaviour --------
fresh_env s1
run_resolver
check "s1 exit" "0" "$RESOLVER_RC"
check "s1 root is baked" "$BAKED" "$RESOLVED_ROOT"
check "s1 state mode" "baked" "$(state_field "$CUSTOMER" mode)"
check "s1 symlink -> baked" "$BAKED" "$(link_target)"
check "s1 no error recorded" "" "$(state_field "$CUSTOMER" error)"

# --- scenario 2: live tree present + usable -> live wins ---------------------
# THE regression test for stale-baked-chassis drift: the operator pulled
# 0.3.1 on the host while the image still bakes 0.3.0.
fresh_env s2 "0.3.0"
mk_tree "$LIVE" "0.3.1"
run_resolver
check "s2 exit" "0" "$RESOLVER_RC"
check "s2 root is live" "$LIVE" "$RESOLVED_ROOT"
check "s2 state mode" "live" "$(state_field "$CUSTOMER" mode)"
check "s2 live version recorded" "0.3.1" "$(state_field "$CUSTOMER" live_version)"
check "s2 baked version recorded" "0.3.0" "$(state_field "$CUSTOMER" baked_version)"
check "s2 symlink -> live" "$LIVE" "$(link_target)"
check "s2 VERSION via symlink" "0.3.1" "$(tr -d '[:space:]' < "$CUSTOMER/state/chassis-root/VERSION")"
check "s2 no error recorded" "" "$(state_field "$CUSTOMER" error)"

# --- scenario 3: torn live tree -> baked + LOUD exit 5 -----------------------
fresh_env s3
mkdir -p "$LIVE"                     # dir exists...
printf '0.3.1\n' > "$LIVE/VERSION"   # ...has a VERSION but no scripts/ etc.
run_resolver
check "s3 loud failure exit" "5" "$RESOLVER_RC"
check "s3 falls back to baked" "$BAKED" "$RESOLVED_ROOT"
check "s3 state mode" "baked" "$(state_field "$CUSTOMER" mode)"
check "s3 error recorded" "yes" "$([[ -n "$(state_field "$CUSTOMER" error)" ]] && echo yes || echo no)"

# --- scenario 4: operator override wins verbatim -----------------------------
fresh_env s4
mk_tree "$LIVE" "0.3.1"
RESOLVED_ROOT="$(CHASSIS_ROOT="/operator/choice" \
    CUSTOMER_HOME="$CUSTOMER" CHASSIS_HOME="$CUSTOMER" \
    CHASSIS_BAKED_TREE_ROOT="$BAKED" \
    bash "$RESOLVER" 2>>"$TMP/resolver.log")"
RESOLVER_RC=$?
check "s4 exit" "0" "$RESOLVER_RC"
check "s4 override honoured" "/operator/choice" "$RESOLVED_ROOT"
check "s4 state mode" "explicit" "$(state_field "$CUSTOMER" mode)"
check "s4 symlink -> override" "/operator/choice" "$(link_target)"

# --- scenario 5: live OLDER than baked still wins (rollback semantics) -------
fresh_env s5 "0.3.1"
mk_tree "$LIVE" "0.3.0"
run_resolver
check "s5 exit" "0" "$RESOLVER_RC"
check "s5 older live still wins" "$LIVE" "$RESOLVED_ROOT"
check "s5 state mode" "live" "$(state_field "$CUSTOMER" mode)"

# --- scenario 6: MAJOR skew refuses the live tree, loudly --------------------
fresh_env s6 "0.3.0"
mk_tree "$LIVE" "1.0.0"
run_resolver
check "s6 loud failure exit" "5" "$RESOLVER_RC"
check "s6 falls back to baked" "$BAKED" "$RESOLVED_ROOT"
check "s6 error mentions MAJOR" "yes" "$([[ "$(state_field "$CUSTOMER" error)" == *MAJOR* ]] && echo yes || echo no)"

# --- scenario 7: rerun retargets the symlink when the live tree goes away ----
fresh_env s7
mk_tree "$LIVE" "0.3.1"
run_resolver
check "s7 first pass live" "$LIVE" "$RESOLVED_ROOT"
rm -rf "$CUSTOMER/chassis"
run_resolver
check "s7 exit after removal" "0" "$RESOLVER_RC"
check "s7 falls back to baked" "$BAKED" "$RESOLVED_ROOT"
check "s7 symlink retargeted" "$BAKED" "$(link_target)"

# --- scenario 8: _env.sh end to end resolves through the symlink -------------
# The integration docker exec sessions depend on: no CHASSIS_ROOT in env, no
# entrypoint exports - _env.sh must land on the resolver's symlink, not guess.
fresh_env s8
mk_tree "$LIVE" "0.3.1"
run_resolver
ENV_RESOLVED="$(env -u CHASSIS_ROOT -u CHASSIS_PLUGINS_ROOT \
    CUSTOMER_HOME="$CUSTOMER" CHASSIS_HOME="$CUSTOMER" \
    bash -c "source '$ENV_SH' && printf '%s' \"\$CHASSIS_ROOT\"" 2>>"$TMP/resolver.log")"
check "s8 _env.sh uses symlink" "$CUSTOMER/state/chassis-root" "$ENV_RESOLVED"
check "s8 symlink serves live VERSION" "0.3.1" "$(tr -d '[:space:]' < "$ENV_RESOLVED/VERSION")"

# --- scenario 9: no baked tree either (host layout) -> empty, exit 0 ---------
fresh_env s9
BAKED="$TMP/s9/nonexistent"
run_resolver
check "s9 exit" "0" "$RESOLVER_RC"
check "s9 empty root" "" "$RESOLVED_ROOT"

# ---------------------------------------------------------------------------
echo
echo "test-chassis-root-resolution: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]] || exit 1
exit 0
