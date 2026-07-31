#!/bin/bash
# test-merge-plugin-triggers.sh - Unit tests for merge-plugin-triggers.sh
# plugin-manifest resolution (behalfbot#98).
#
# The bug these lock down
# ========================
# merge-plugin-triggers.sh read manifests from $CHASSIS_HOME/plugins/<name>,
# never consulting CHASSIS_PLUGINS_ROOT. Every other plugin-tree consumer
# (cmd_install_plugin, smoke-test.sh, bootstrap.sh) resolves plugins through
# CHASSIS_PLUGINS_ROOT (the baked+fetched overlay from resolve-plugin-root.sh,
# behalfbot#95). This script alone picked up only the customer-side
# plugins/ directory (local-only plugins like midnight-oil) and silently
# merged zero triggers for everything else - with no error, no warning, and
# a "✓ merged" success line printed regardless.
#
# No docker daemon, no network. Everything runs against temp directories.
#
# Exit codes:
#   0 - all scenarios passed
#   1 - one or more scenarios failed
#   2 - test harness itself broke

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGE="${SCRIPT_DIR}/merge-plugin-triggers.sh"

if [[ ! -f "$MERGE" ]]; then
    echo "test-merge-plugin-triggers: missing $MERGE" >&2
    exit 2
fi

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
    # manifest <dir> <plugin-id> <trigger-name>
    mkdir -p "$1"
    cat > "$1/openclaw.plugin.json" <<JSON
{
  "id": "$2",
  "contracts": {
    "triggers": [
      {"name": "$3", "keyword_regex": "^${3}:", "handler": "/bin/true"}
    ]
  }
}
JSON
}

fresh_env() {
    # fresh_env <label> -> sets CUSTOMER, ROOT globals, writes config + template
    CUSTOMER="$TMP/$1/customer"
    ROOT="$TMP/$1/plugins-root"
    mkdir -p "$CUSTOMER/chassis" "$ROOT"
    cat > "$CUSTOMER/chassis.config.yaml" <<YAML
modules:
  alpha:
    enabled: true
  beta:
    enabled: true
  gamma:
    enabled: false
YAML
    printf '# template header\ntriggers:\n' > "$CUSTOMER/chassis/triggers.yaml.template"
}

run_merge() {
    # -> MERGED_OUT, MERGE_RC
    MERGED_OUT="$(DRY_RUN=true \
        CHASSIS_HOME="$CUSTOMER" CUSTOMER_HOME="$CUSTOMER" \
        CHASSIS_PLUGINS_ROOT="$ROOT" \
        bash "$MERGE" 2>"$TMP/merge.log")"
    MERGE_RC=$?
}

# --- scenario 1: plugin lives only in the resolved plugin root --------------
# THE regression test: pre-fix, this plugin would never be found because the
# script looked under $CHASSIS_HOME/plugins, not $CHASSIS_PLUGINS_ROOT.
fresh_env s1
manifest "$ROOT/alpha" "alpha" "alpha-trigger"
manifest "$ROOT/beta" "beta" "beta-trigger"
run_merge
check "s1 exit" "0" "$MERGE_RC"
check "s1 alpha trigger merged" "1" "$(grep -c 'name: alpha-trigger' <<<"$MERGED_OUT")"
check "s1 beta trigger merged" "1" "$(grep -c 'name: beta-trigger' <<<"$MERGED_OUT")"

# --- scenario 2: one plugin only in customer-local plugins/ -----------------
# The behaviour that DID work pre-fix (e.g. Sean's midnight-oil) must keep
# working: a plugin with no manifest in the resolved root but a manifest
# under $CUSTOMER_HOME/plugins/<name>.
fresh_env s2
manifest "$ROOT/alpha" "alpha" "alpha-trigger"
manifest "$CUSTOMER/plugins/beta" "beta" "beta-trigger"
run_merge
check "s2 exit" "0" "$MERGE_RC"
check "s2 alpha (root) trigger merged" "1" "$(grep -c 'name: alpha-trigger' <<<"$MERGED_OUT")"
check "s2 beta (customer-local) trigger merged" "1" "$(grep -c 'name: beta-trigger' <<<"$MERGED_OUT")"

# --- scenario 3: resolved root wins when a plugin exists in both ------------
fresh_env s3
manifest "$ROOT/alpha" "alpha" "root-wins-trigger"
manifest "$CUSTOMER/plugins/alpha" "alpha" "customer-shadow-trigger"
manifest "$ROOT/beta" "beta" "beta-trigger"
run_merge
check "s3 exit" "0" "$MERGE_RC"
check "s3 root manifest wins" "1" "$(grep -c 'name: root-wins-trigger' <<<"$MERGED_OUT")"
check "s3 customer manifest shadowed" "0" "$(grep -c 'name: customer-shadow-trigger' <<<"$MERGED_OUT")"

# --- scenario 4: an enabled plugin with no manifest anywhere fails loudly ---
# This is the check that can fail (issue acceptance criterion): removing/
# breaking the resolution must not merge silently.
fresh_env s4
manifest "$ROOT/alpha" "alpha" "alpha-trigger"
# beta enabled in config but no manifest under $ROOT or $CUSTOMER/plugins.
run_merge
check "s4 exit is nonzero" "1" "$([[ "$MERGE_RC" -ne 0 ]] && echo 1 || echo 0)"
check "s4 error mentions beta" "1" "$(grep -c 'beta' "$TMP/merge.log")"

# ---------------------------------------------------------------------------
echo
echo "test-merge-plugin-triggers: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]] || exit 1
exit 0
