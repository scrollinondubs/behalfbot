#!/usr/bin/env bash
# test-memory-canary.sh - behavioural tests for memory-canary.sh (#145).
#
# The canary exists because a memory graph can be broken in a way that produces
# no error: reads return a well-formed empty result, which is indistinguishable
# from "nothing saved yet". A monitor for that failure is only worth anything
# once it has been SEEN to fail, so most of these cases force a broken graph
# and assert the canary reports it, with the resolved path in the message.
#
# Scenarios:
#   1. healthy graph, current template shape           -> PASS
#   2. write and read really are separate processes    -> two distinct PIDs
#   3. .mcp.json points at a namespace-invalid abs path-> FAIL (precheck)
#   4. graph dir not writable                          -> FAIL (precheck, skipped as root)
#   5. read comes back empty despite a good write      -> FAIL (read)
#   6. write does not persist, server still says ok    -> FAIL (write)
#   7. server command cannot start                     -> FAIL (write)
#   8. mcpServers.memory block absent                  -> FAIL (resolve)
#   9. healthy graph, legacy env.MEMORY_FILE_PATH shape-> PASS
#
# No docker, no network, no npx: a stub memory server stands in for
# @modelcontextprotocol/server-memory. The stub persists to $MEMORY_FILE_PATH
# and nowhere else, which is what makes case 2 meaningful.
#
# Exit 0 all pass, 1 on failure, 2 on harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CANARY="${SCRIPT_DIR}/memory-canary.sh"
STUB="${SCRIPT_DIR}/tests/stub-memory-server.sh"

for f in "$CANARY" "$STUB"; do
    if [[ ! -f "$f" ]]; then
        echo "test-memory-canary: missing $f" >&2
        exit 2
    fi
done
if ! command -v jq >/dev/null 2>&1; then
    echo "test-memory-canary: jq required" >&2
    exit 2
fi

fail=0
pass=0

TMP="$(mktemp -d)"
cleanup() { chmod -R u+rwX "$TMP" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

# make_home <name> <memory-json-block> [nomemdir]
make_home() {
    local home="$TMP/$1" block="$2" opt="${3:-}"
    mkdir -p "$home"
    [[ "$opt" == "nomemdir" ]] || mkdir -p "$home/memory"
    printf '{"mcpServers":{%s}}\n' "$block" > "$home/.mcp.json"
    printf '%s' "$home"
}

# The current template shape, with the stub in place of npx. MEMORY_FILE_PATH
# resolves from the launch cwd exactly as the real template does.
stub_template_block() {
    jq -nc --arg stub "$STUB" \
        '{memory:{command:"sh", args:["-c", ("export MEMORY_FILE_PATH=\"$(pwd)/memory/memory.jsonl\"; exec bash " + $stub)]}}' \
        | sed 's/^{//; s/}$//'
}

# Legacy shape: an absolute MEMORY_FILE_PATH baked into an env block.
stub_legacy_block() {
    jq -nc --arg stub "$STUB" --arg path "$1" \
        '{memory:{command:"bash", args:[$stub], env:{MEMORY_FILE_PATH:$path}}}' \
        | sed 's/^{//; s/}$//'
}

run_canary() {
    local home="$1"
    env -u CHASSIS_HOME MEMORY_CANARY_TIMEOUT=30 \
        bash "$CANARY" --customer-home "$home" --json 2>/dev/null
}

# assert_case <name> <json-output> <pass|fail> [expected-stage] [expected-error-substring]
assert_case() {
    local name="$1" out="$2" want="$3" want_stage="${4:-}" want_err="${5:-}"
    local ok stage err got
    # `.ok // "none"` would report false as "none": jq's alternative operator
    # treats false as absent. has() is the only correct test here.
    ok="$(printf '%s' "$out" | jq -r 'if type=="object" and has("ok") then .ok else "none" end' 2>/dev/null)"
    stage="$(printf '%s' "$out" | jq -r '.stage // ""' 2>/dev/null)"
    err="$(printf '%s' "$out" | jq -r '.error // ""' 2>/dev/null)"
    case "$ok" in
        true)  got=pass ;;
        false) got=fail ;;
        *)     got=none ;;
    esac
    if [[ "$got" != "$want" ]]; then
        printf '  FAIL %s: expected %s, got %s\n' "$name" "$want" "$got"
        printf '       | %s\n' "$out"
        fail=$((fail + 1))
        return
    fi
    if [[ -n "$want_stage" && "$stage" != "$want_stage" ]]; then
        printf '  FAIL %s: expected stage %s, got %s\n' "$name" "$want_stage" "$stage"
        printf '       | %s\n' "$err"
        fail=$((fail + 1))
        return
    fi
    if [[ -n "$want_err" ]] && ! printf '%s' "$err" | grep -qF "$want_err"; then
        printf '  FAIL %s: error did not mention "%s"\n' "$name" "$want_err"
        printf '       | %s\n' "$err"
        fail=$((fail + 1))
        return
    fi
    printf '  ok   %s (%s%s)\n' "$name" "$got" "${stage:+, stage=$stage}"
    pass=$((pass + 1))
}

echo "test-memory-canary"

# 1. Healthy install, current template shape.
h="$(make_home healthy "$(stub_template_block)")"
assert_case "healthy graph, current template shape" "$(run_canary "$h")" pass verified

# The canary must have left exactly one canary entity behind, holding the
# nonce it reported. Anything else means the graph is not what it claims.
graph_entities="$(jq -sc '[.[] | select(.type=="entity") | .name]' "$h/memory/memory.jsonl" 2>/dev/null)"
if [[ "$graph_entities" == '["health:memory-canary"]' ]]; then
    printf '  ok   canary entity persisted to the graph file\n'
    pass=$((pass + 1))
else
    printf '  FAIL canary entity not persisted: %s\n' "$graph_entities"
    fail=$((fail + 1))
fi

# 2. The load-bearing property: the read-back happens in a DIFFERENT process
#    from the write. A same-process round trip would pass against an in-memory
#    cache while the file path was broken, which is the #145 bug inverted.
h="$(make_home separate "$(stub_template_block)")"
PIDLOG="$TMP/pids.txt"
: > "$PIDLOG"
out="$(STUB_PID_LOG="$PIDLOG" run_canary "$h")"
assert_case "separate-invocation: canary passes" "$out" pass verified
spawns="$(wc -l < "$PIDLOG" | tr -d ' ')"
distinct="$(sort -u "$PIDLOG" | grep -c . )"
if [[ "$spawns" == "2" && "$distinct" == "2" ]]; then
    printf '  ok   separate-invocation: 2 server spawns, 2 distinct PIDs\n'
    pass=$((pass + 1))
else
    printf '  FAIL separate-invocation: %s spawns, %s distinct PIDs (want 2 and 2)\n' "$spawns" "$distinct"
    fail=$((fail + 1))
fi

# 3. The original bug: an absolute path baked in the other namespace. Must fail
#    at precheck, and the message must carry the path so the alert names the
#    namespace problem instead of saying "memory broken".
BAD_NS="/Users/nobody-$$/.behalfbot/memory/memory.jsonl"
h="$(make_home badns "$(stub_legacy_block "$BAD_NS")")"
assert_case "namespace-invalid absolute path" "$(run_canary "$h")" fail precheck "/Users/nobody-$$/.behalfbot/memory"

# 4. Graph directory not writable. Root ignores mode bits, so skip there.
if [[ "$(id -u)" -eq 0 ]]; then
    echo "  skip non-writable graph dir (running as root, chmod is not enforced)"
else
    h="$(make_home unwritable "$(stub_template_block)")"
    chmod 500 "$h/memory"
    assert_case "graph dir not writable" "$(run_canary "$h")" fail precheck "not writable"
    chmod 700 "$h/memory"
fi

# 5. Read comes back EMPTY despite a write the server reported as successful.
#    /dev/null reproduces the production symptom exactly with no faking: the
#    write succeeds, the read loads an empty graph. This is the case Gap 3
#    cannot see, because the path is present and writable throughout.
h="$(make_home blackhole "$(stub_legacy_block /dev/null)")"
assert_case "read empty despite successful write" "$(run_canary "$h")" fail read "EMPTY"

# 6. Write does not persist while the server keeps reporting success: the prior
#    canary entity survives the delete, so create_entities creates nothing.
h="$(make_home nopersist "$(stub_template_block)")"
printf '{"type":"entity","name":"health:memory-canary","entityType":"health","observations":["nonce stale-from-yesterday"]}\n' \
    > "$h/memory/memory.jsonl"
out="$(STUB_IGNORE_DELETE=1 run_canary "$h")"
assert_case "write reports success but does not persist" "$out" fail write "not persisting"

# 7. Server command cannot start at all.
h="$(make_home nostart '"memory":{"command":"/nonexistent/memory-server-'"$$"'"}')"
assert_case "server command cannot start" "$(run_canary "$h")" fail write ""

# 8. No memory server configured.
h="$(make_home nomemory '"filesystem":{"command":"npx"}')"
assert_case "mcpServers.memory absent" "$(run_canary "$h")" fail resolve "mcpServers.memory missing"

# 9. Legacy env shape on a reachable graph still passes - the canary must not
#    require the current template, only a graph that works.
mkdir -p "$TMP/legacy-ok/memory"
h="$(make_home legacy-ok "$(stub_legacy_block "$TMP/legacy-ok/memory/memory.jsonl")")"
assert_case "healthy graph, legacy env.MEMORY_FILE_PATH shape" "$(run_canary "$h")" pass verified

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]] || exit 1
