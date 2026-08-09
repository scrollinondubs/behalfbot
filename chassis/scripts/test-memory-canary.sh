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
#   1b. a SECOND consecutive run still passes          -> PASS (see case 2 note)
#   2. write and read really are separate processes    -> three distinct PIDs
#   3. .mcp.json points at a namespace-invalid abs path-> FAIL (precheck)
#   4. graph dir not writable                          -> FAIL (precheck, skipped as root)
#   5. read comes back empty despite a good write      -> FAIL (read)
#   6. write does not persist, server still says ok    -> FAIL (write)
#   7. server command cannot start                     -> FAIL (write)
#   8. mcpServers.memory block absent                  -> FAIL (resolve)
#   9. healthy graph, legacy env.MEMORY_FILE_PATH shape-> PASS
#  10. gather-memory-canary.sh: healthy -> count 0, broken -> count 1, missing
#      canary -> count 1, and ALWAYS exit 0 (a non-zero gather exit is read as
#      count=0, which would mute the monitor exactly when it fires)
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
CALLLOG="$TMP/calls.txt"
: > "$PIDLOG"
: > "$CALLLOG"
out="$(STUB_PID_LOG="$PIDLOG" STUB_CALL_LOG="$CALLLOG" run_canary "$h")"
assert_case "separate-invocation: canary passes" "$out" pass verified
spawns="$(wc -l < "$PIDLOG" | tr -d ' ')"
distinct="$(sort -u "$PIDLOG" | grep -c . )"
# Three: clear, write, read. One tool call per process - server-memory handles
# pipelined requests concurrently over a lock-free read-modify-write of the
# whole graph file, so process exit is the only ordering barrier available.
if [[ "$spawns" == "3" && "$distinct" == "3" ]]; then
    printf '  ok   separate-invocation: 3 server spawns, 3 distinct PIDs\n'
    pass=$((pass + 1))
else
    printf '  FAIL separate-invocation: %s spawns, %s distinct PIDs (want 3 and 3)\n' "$spawns" "$distinct"
    fail=$((fail + 1))
fi

# One tool call per process, in order. Pipelining delete_entities and
# create_entities into one process is what broke against the real server on
# every run after the first: both handlers loadGraph concurrently, so create
# read the pre-delete graph, found the entity still there and created nothing
# while reporting success. This asserts the ordering barrier is still process
# exit, which the stub is too well-behaved to catch on its own.
call_sequence="$(awk '{print $2}' "$CALLLOG" | tr '\n' ',')"
call_pids="$(awk '{print $1}' "$CALLLOG" | sort -u | grep -c .)"
if [[ "$call_sequence" == "delete_entities,create_entities,open_nodes," && "$call_pids" == "3" ]]; then
    printf '  ok   one tool call per process, in order (delete, create, read)\n'
    pass=$((pass + 1))
else
    printf '  FAIL tool-call sequencing: %s across %s pids\n' "$call_sequence" "$call_pids"
    fail=$((fail + 1))
fi

# A SECOND consecutive run must also pass. The first run has nothing to delete,
# so it passes even when the delete/create sequencing is wrong - which is
# exactly how the bug above hid until the canary ran twice.
assert_case "second consecutive run on the same graph" "$(run_canary "$h")" pass verified

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


# ------------------------------------------------------------------
# gather-memory-canary.sh
#
# The gather's exit inversion is the single most load-bearing convention here:
# the dispatcher treats a non-zero gather exit as count=0, so a gather that
# exited non-zero on failure would go silent exactly when it fires. That is a
# one-character regression away at all times, so assert it rather than trust
# the comment.
# ------------------------------------------------------------------

GATHER="${SCRIPT_DIR}/gather-memory-canary.sh"

# Build a CHASSIS_HOME layout the gather can resolve the canary through.
FAKE_CHASSIS="$TMP/chassis-home"
mkdir -p "$FAKE_CHASSIS/chassis/scripts/tests"
cp "$CANARY" "$SCRIPT_DIR/_memory-graph.sh" "$GATHER" "$FAKE_CHASSIS/chassis/scripts/"
cp "$STUB" "$FAKE_CHASSIS/chassis/scripts/tests/"

# run_gather <customer-home> <chassis-home>
run_gather() {
    env -u CHASSIS_HOME CUSTOMER_HOME="$1" CHASSIS_HOME="$2" MEMORY_CANARY_TIMEOUT=30 \
        bash "$GATHER" 2>/dev/null
}

# assert_gather <name> <customer-home> <chassis-home> <expected-count> [expected-stage]
assert_gather() {
    local name="$1" home="$2" chassis="$3" want_count="$4" want_stage="${5:-}"
    local out rc count stage
    out="$(run_gather "$home" "$chassis")"
    rc=$?
    if [[ $rc -ne 0 ]]; then
        printf '  FAIL gather %s: exited %d. A non-zero gather exit is read as count=0, so the monitor goes silent when it fires.\n' "$name" "$rc"
        fail=$((fail + 1))
        return
    fi
    if ! printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
        printf '  FAIL gather %s: stdout is not JSON: %s\n' "$name" "$out"
        fail=$((fail + 1))
        return
    fi
    count="$(printf '%s' "$out" | jq -r '.count')"
    stage="$(printf '%s' "$out" | jq -r '.stage // ""')"
    if [[ "$count" != "$want_count" ]]; then
        printf '  FAIL gather %s: expected count %s, got %s\n' "$name" "$want_count" "$count"
        printf '       | %s\n' "$out"
        fail=$((fail + 1))
        return
    fi
    if [[ -n "$want_stage" && "$stage" != "$want_stage" ]]; then
        printf '  FAIL gather %s: expected stage %s, got %s\n' "$name" "$want_stage" "$stage"
        fail=$((fail + 1))
        return
    fi
    printf '  ok   gather %s (exit 0, count=%s%s)\n' "$name" "$count" "${stage:+, stage=$stage}"
    pass=$((pass + 1))
}

h="$(make_home gather-healthy "$(stub_template_block)")"
assert_gather "healthy install is silent" "$h" "$FAKE_CHASSIS" 0 verified

h="$(make_home gather-broken "$(stub_legacy_block /dev/null)")"
assert_gather "broken graph fires, exit still 0" "$h" "$FAKE_CHASSIS" 1 read

# A chassis tree with no canary script: the monitor cannot run. Loud on
# purpose - a critical liveness check that is absent is itself the alarm, which
# is why this diverges from gather-bootstrap-audit.sh's quiet count=0.
mkdir -p "$TMP/chassis-empty/chassis/scripts"
assert_gather "missing canary script fires" "$h" "$TMP/chassis-empty" 1 unavailable

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]] || exit 1
