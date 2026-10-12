#!/usr/bin/env bash
# test-bootstrap-audit-memory.sh - behavioural tests for bootstrap-audit.sh Gap 3.
#
# Gap 3 exists to catch an unreachable memory graph (the William Holdeman
# 2026-06-20 amnesiac-bot incident). It read .mcpServers.memory.env.MEMORY_FILE_PATH
# only, while the current .mcp.json.template deliberately sets no env block, so
# it failed on every install running that template and could never pass - a
# check that cannot pass has never caught anything (#142).
#
# Per the "checks that cannot fail" rule: the fix is only trusted once the
# check has been SEEN to fail. Half of these cases force a broken memory graph
# and assert Gap 3 reports it.
#
# Scenarios:
#   1. current template shape, memory/ writable        -> pass  (the #142 bug)
#   2. legacy env, absolute writable path              -> pass
#   3. legacy env, ${CUSTOMER_HOME} token              -> pass
#   4. legacy env, ${CHASSIS_HOME:-default} form       -> pass  (second symptom)
#   5. legacy env, ${CHASSIS_HOME} bare, var set       -> pass
#   6. current template shape, memory/ MISSING         -> FAIL
#   7. legacy env, absolute path from another namespace-> FAIL
#   8. non-writable parent dir                         -> FAIL  (skipped as root)
#   9. mcpServers.memory block absent entirely         -> FAIL
#  10. memory.mcp_enabled: false, no memory block      -> SKIP (#219)
#  11. memory.mcp_enabled: false, broken graph         -> SKIP (flag wins)
#  12. memory.mcp_enabled: true, healthy / no block    -> pass / FAIL (as today)
#  13. memory: block without the key, no memory block  -> FAIL (absent == true)
#  14. flag false, python3 has no PyYAML (awk path)    -> SKIP
#  15. awk path, mcp_enabled under another block       -> FAIL
#
# No docker, no network - pure temp dirs + jq. Exit 0 all pass, 1 on failure,
# 2 on harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT="${SCRIPT_DIR}/bootstrap-audit.sh"

if [[ ! -f "$AUDIT" ]]; then
    echo "test-bootstrap-audit-memory: audit script not found at $AUDIT" >&2
    exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "test-bootstrap-audit-memory: jq required" >&2
    exit 2
fi

fail=0
pass=0

TMP="$(mktemp -d)"
cleanup() { chmod -R u+rwX "$TMP" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

# Build a customer home. $1 = dir name under $TMP, $2 = memory json block,
# $3 = "nomemdir" to skip creating memory/.
make_home() {
    local home="$TMP/$1" block="$2" opt="${3:-}"
    mkdir -p "$home"
    [[ "$opt" == "nomemdir" ]] || mkdir -p "$home/memory"
    printf '{"mcpServers":{%s}}\n' "$block" > "$home/.mcp.json"
    printf '%s' "$home"
}

# Run the audit against $1 and print only the Gap 3 section. CHASSIS_HOME is
# cleared unless passed as $2, so the ${VAR:-default} case exercises the
# default rather than an ambient value.
run_gap3() {
    local home="$1" chassis_home="${2:-}"
    env -u CHASSIS_HOME CHASSIS_HOME="$chassis_home" BOT_NAME=testbot \
        bash "$AUDIT" --customer-home "$home" 2>/dev/null \
        | sed 's/\x1b\[[0-9;]*m//g' \
        | awk '/^Gap 3:/{f=1;next} /^Gap 4:/{f=0} f'
}

assert_case() {
    local name="$1" out="$2" want="$3"   # want = pass | fail | skip
    local got
    # A skip line carries neither glyph, so without its own branch it would
    # read as "none" - and so would a Gap 3 that printed nothing at all.
    if printf '%s' "$out" | grep -q 'skipped: memory MCP disabled'; then
        got=skip
    elif printf '%s' "$out" | grep -q '✓'; then
        got=pass
    elif printf '%s' "$out" | grep -q '✗'; then
        got=fail
    else
        got="none"
    fi
    if [[ "$got" == "$want" ]]; then
        printf '  ok   %s (Gap 3 %s)\n' "$name" "$got"
        pass=$((pass + 1))
    else
        printf '  FAIL %s: expected Gap 3 to %s, got %s\n' "$name" "$want" "$got"
        printf '%s\n' "$out" | sed 's/^/       | /'
        fail=$((fail + 1))
    fi
}

TEMPLATE_BLOCK='"memory":{"command":"sh","args":["-c","export MEMORY_FILE_PATH=\"$(pwd)/memory/memory.jsonl\"; exec npx -y @modelcontextprotocol/server-memory"]}'

echo "test-bootstrap-audit-memory"

# 1. The bug itself: current template shape on a healthy install must PASS.
h="$(make_home current "$TEMPLATE_BLOCK")"
assert_case "current template shape, memory/ writable" "$(run_gap3 "$h")" pass

# 2. Legacy absolute env path that is valid here.
h="$(make_home legacy-abs '"memory":{"command":"npx","env":{"MEMORY_FILE_PATH":"'"$TMP"'/legacy-abs/memory/memory.jsonl"}}')"
assert_case "legacy env, absolute writable path" "$(run_gap3 "$h")" pass

# 3. ${CUSTOMER_HOME} token.
h="$(make_home legacy-customer '"memory":{"command":"npx","env":{"MEMORY_FILE_PATH":"${CUSTOMER_HOME}/memory/memory.jsonl"}}')"
assert_case "legacy env, \${CUSTOMER_HOME} token" "$(run_gap3 "$h")" pass

# 4. ${CHASSIS_HOME:-default} form, CHASSIS_HOME unset. The old expansion only
#    handled the bare token, so this fell through as a literal and failed.
mkdir -p "$TMP/chassis-default/memory"
h="$(make_home legacy-default '"memory":{"command":"npx","env":{"MEMORY_FILE_PATH":"${CHASSIS_HOME:-'"$TMP"'/chassis-default}/memory/memory.jsonl"}}')"
assert_case "legacy env, \${CHASSIS_HOME:-default} form" "$(run_gap3 "$h")" pass

# 5. Bare ${CHASSIS_HOME} with the var set wins over any inline default.
mkdir -p "$TMP/chassis-set/memory"
h="$(make_home legacy-bare '"memory":{"command":"npx","env":{"MEMORY_FILE_PATH":"${CHASSIS_HOME}/memory/memory.jsonl"}}')"
assert_case "legacy env, bare \${CHASSIS_HOME}, var set" "$(run_gap3 "$h" "$TMP/chassis-set")" pass

# 6. Template shape but no memory/ dir - the unreachable-graph failure the gap
#    exists for. Must still FAIL, otherwise the fix passes everything.
h="$(make_home current-broken "$TEMPLATE_BLOCK" nomemdir)"
assert_case "current template shape, memory/ MISSING" "$(run_gap3 "$h")" fail

# 7. Absolute path baked in another namespace (host path read in a container).
h="$(make_home legacy-foreign '"memory":{"command":"npx","env":{"MEMORY_FILE_PATH":"/Users/nobody-'"$$"'/.behalfbot/memory/memory.jsonl"}}')"
assert_case "legacy env, path from another namespace" "$(run_gap3 "$h")" fail

# 8. Parent dir exists but is not writable. Root ignores mode bits, so skip.
if [[ "$(id -u)" -eq 0 ]]; then
    echo "  skip non-writable parent dir (running as root, chmod is not enforced)"
else
    h="$(make_home unwritable "$TEMPLATE_BLOCK")"
    chmod 500 "$h/memory"
    assert_case "memory/ not writable" "$(run_gap3 "$h")" fail
    chmod 700 "$h/memory"
fi

# 9. No memory server at all.
h="$(make_home nomemory '"filesystem":{"command":"npx"}')"
assert_case "mcpServers.memory absent" "$(run_gap3 "$h")" fail

# ------------------------------------------------------------------
# memory.mcp_enabled (#219)
#
# The memory MCP is optional. An install that sets the flag false must see Gap
# 3 skipped - not passed, not failed - and an install that never set it must
# see exactly the behaviour above.
# ------------------------------------------------------------------

# write_config <home> <yaml-body>
write_config() { printf '%s\n' "$2" > "$1/chassis.config.yaml"; }

h="$(make_home flag-off-nomemory '"filesystem":{"command":"npx"}')"
write_config "$h" $'memory:\n  ships_with: pre_seeded\n  mcp_enabled: false'
assert_case "mcp_enabled false, no memory block" "$(run_gap3 "$h")" skip

h="$(make_home flag-off-broken "$TEMPLATE_BLOCK" nomemdir)"
write_config "$h" $'memory:\n  mcp_enabled: false   # retired'
assert_case "mcp_enabled false, broken graph" "$(run_gap3 "$h")" skip

h="$(make_home flag-on "$TEMPLATE_BLOCK")"
write_config "$h" $'memory:\n  mcp_enabled: true'
assert_case "mcp_enabled true, healthy graph" "$(run_gap3 "$h")" pass

h="$(make_home flag-on-nomemory '"filesystem":{"command":"npx"}')"
write_config "$h" $'memory:\n  mcp_enabled: true'
assert_case "mcp_enabled true, memory block absent" "$(run_gap3 "$h")" fail

h="$(make_home flag-absent-nomemory '"filesystem":{"command":"npx"}')"
write_config "$h" $'memory:\n  ships_with: pre_seeded'
assert_case "memory block without mcp_enabled, memory server absent" "$(run_gap3 "$h")" fail

# The awk fallback: python3 present but PyYAML not importable. A yaml.py that
# raises ImportError shadows the real one through PYTHONPATH.
NOYAML="$TMP/noyaml"
mkdir -p "$NOYAML"
printf 'raise ImportError("stubbed for test")\n' > "$NOYAML/yaml.py"
h="$(make_home flag-off-awk '"filesystem":{"command":"npx"}')"
write_config "$h" $'memory:\n  ships_with: pre_seeded\n  mcp_enabled: false   # retired 2026'
assert_case "mcp_enabled false read by the awk fallback (no PyYAML)" \
    "$(PYTHONPATH="$NOYAML" run_gap3 "$h")" skip
h="$(make_home flag-absent-awk '"filesystem":{"command":"npx"}')"
write_config "$h" $'memory:\n  ships_with: pre_seeded\nother:\n  mcp_enabled: false'
assert_case "awk fallback ignores mcp_enabled outside the memory block" \
    "$(PYTHONPATH="$NOYAML" run_gap3 "$h")" fail

# The summary line still carries "N failed" where the gather parses it.
h="$(make_home flag-summary '"filesystem":{"command":"npx"}')"
write_config "$h" $'memory:\n  mcp_enabled: false'
summary="$(env -u CHASSIS_HOME BOT_NAME=testbot bash "$AUDIT" --customer-home "$h" 2>/dev/null \
    | sed 's/\x1b\[[0-9;]*m//g' | grep '^Summary:')"
if printf '%s' "$summary" | grep -qE '^Summary: [0-9]+ passed, [0-9]+ warned, [0-9]+ failed, [1-9][0-9]* skipped$'; then
    printf '  ok   summary line counts the skip and keeps "N failed" (%s)\n' "$summary"
    pass=$((pass + 1))
else
    printf '  FAIL summary line shape: %s\n' "$summary"
    fail=$((fail + 1))
fi

# ------------------------------------------------------------------
# gather-bootstrap-audit.sh locates the audit (#151)
#
# The gather derived the audit's path from CHASSIS_HOME, which names the chassis
# tree only on a host-side install. On a container it resolved a path that does
# not exist and emitted the quiet "cannot gate" count=0 forever - the audit has
# never run on a containerized install. Assert the two resolution paths that
# replace it: a sibling, and the boot-time chassis-root record for a root that
# is not $CHASSIS_HOME/chassis. A stub audit stands in for the real one so this
# stays a path-resolution test, not a second copy of the audit suite.
# ------------------------------------------------------------------

GATHER="${SCRIPT_DIR}/gather-bootstrap-audit.sh"

make_stub_tree() {
    local root="$TMP/$1"
    mkdir -p "$root/scripts"
    cat > "$root/scripts/bootstrap-audit.sh" <<'STUB'
#!/usr/bin/env bash
echo "Summary: 3 passed, 1 warned, 2 failed"
STUB
    chmod +x "$root/scripts/bootstrap-audit.sh"
    printf '%s' "$root"
}

# assert_gather_count <name> <gather-path> <customer-home> <chassis-home> <expected-count>
assert_gather_count() {
    local name="$1" gather="$2" home="$3" chassis="$4" want="$5"
    local out count
    out="$(env -u CHASSIS_HOME CUSTOMER_HOME="$home" CHASSIS_HOME="$chassis" bash "$gather" 2>/dev/null)"
    count="$(printf '%s' "$out" | jq -r '.count' 2>/dev/null)"
    if [[ "$count" != "$want" ]]; then
        printf '  FAIL gather %s: expected count %s, got %s\n       | %s\n' "$name" "$want" "$count" "$out"
        fail=$((fail + 1))
        return
    fi
    printf '  ok   gather %s (count=%s)\n' "$name" "$count"
    pass=$((pass + 1))
}

SIBLING_TREE="$(make_stub_tree sibling-tree)"
cp "$GATHER" "$SIBLING_TREE/scripts/"
assert_gather_count "sibling audit is found" "$SIBLING_TREE/scripts/gather-bootstrap-audit.sh" \
    "$TMP/gather-sibling-home" "$TMP/nonexistent-chassis-home" 2

OFFSET_TREE="$(make_stub_tree offset-tree/chassis/chassis)"
LONE_GATHER_DIR="$TMP/lone-audit-gather"
mkdir -p "$LONE_GATHER_DIR" "$TMP/gather-offset-home"
cp "$GATHER" "$LONE_GATHER_DIR/"
cat > "$TMP/gather-offset-home/chassis-root.state.json" <<EOF
{"schema": 1, "mode": "live", "resolved_root": "$OFFSET_TREE",
 "resolved_at": "2026-08-09T00:00:00Z", "error": null}
EOF
assert_gather_count "chassis root outside \$CHASSIS_HOME/chassis resolves via state file" \
    "$LONE_GATHER_DIR/gather-bootstrap-audit.sh" \
    "$TMP/gather-offset-home" "$TMP/nonexistent-chassis-home" 2

# No audit anywhere: stays quiet at count=0. Unlike the memory canary this
# gather is not criticality: critical, and its documented contract is a silent
# note rather than an alarm - the fix must not change that.
mkdir -p "$TMP/gather-empty-home"
assert_gather_count "missing audit stays quiet" "$LONE_GATHER_DIR/gather-bootstrap-audit.sh" \
    "$TMP/gather-empty-home" "$TMP/nonexistent-chassis-home" 0

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]] || exit 1
