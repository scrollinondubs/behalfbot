#!/usr/bin/env bash
# memory-canary.sh - write-read-verify liveness check for the memory MCP graph.
#
# The failure this exists for
# ===========================
# Between at least 2026-07-24 and 2026-08-09 the memory knowledge graph on
# Sean's install was unreachable from inside the chassis container. `.mcp.json`
# baked an absolute HOST path; the container sees that same bind-mounted file
# at a different absolute path, so every container-side read returned
# `{"entities":[],"relations":[]}` and every write went nowhere. Sixteen days,
# no error anyone saw. Fixed in #142 / #143.
#
# Nothing caught it because of the SHAPE of the failure:
#   - writes did not error loudly in a way anything collected;
#   - a well-formed empty graph is indistinguishable from "no prior context",
#     so a read reads as a clean slate, not a broken tool;
#   - bootstrap-audit Gap 3 checks the graph is WRITABLE, not that anything is
#     WRITING. A writable, reachable, completely unused graph passes it clean,
#     which is exactly the state that install was in.
#
# Why a canary and not an mtime threshold
# =======================================
# The obvious monitor is "alarm if memory.jsonl has not changed in N days".
# Rejected in #145: memory writes are event-driven, not periodic, so a
# genuinely quiet week is indistinguishable from a broken graph. Any threshold
# either false-alarms or is set loose enough that it would not have caught the
# sixteen-day outage until late. It also measures a proxy (are writes
# happening) rather than the property we care about (can this install write and
# read back its own memory).
#
# What this does instead
# ======================
#   1. Resolve the memory server out of .mcp.json (shared with bootstrap-audit
#      Gap 3 via _memory-graph.sh) and precheck the graph path in THIS
#      namespace. A namespace-invalid path fails here, named, before any spawn.
#   2. Spawn the server EXACTLY as .mcp.json declares it, cwd = customer root,
#      and clear the reserved entity `health:memory-canary`.
#   3. Let that process exit. Spawn again and write the entity back with a
#      nonce unique to this run.
#   4. Let that process exit. Spawn a THIRD time and read the entity back.
#   5. Assert the nonce read equals the nonce written.
#
# Step 4 is the load-bearing one. A same-process round trip would pass against
# an in-memory cache while the file path was broken - a monitor that passes
# while broken is the bug we just fixed wearing the opposite sign. Separate
# invocation is guaranteed structurally here: each `spawn_memory_server` call
# is an independent process spawn with nothing shared but the graph file.
#
# Why the delete and the write are also separate invocations
# ==========================================================
# One tool call per invocation, always. @modelcontextprotocol/server-memory
# 0.6.3 handles pipelined stdio requests CONCURRENTLY, and every mutation is a
# read-modify-write of the whole graph file with no locking: `deleteEntities`
# and `createEntities` each start with `loadGraph()`. Send both down one pipe
# and `createEntities` can load the pre-delete graph, see the entity still
# present, filter it out as already-existing and create nothing - while
# reporting success. Observed against Sean's live install on the second run:
# the first run passed (nothing to delete), every run after it failed with
# "create_entities did not report creating". Process exit is the only ordering
# barrier available without a stateful JSON-RPC client, so each mutation gets
# its own process.
#
# It must be run INSIDE the container. A host-side check passed cleanly the
# entire time the container was broken, so a host-only run proves nothing about
# the namespace where the assistant actually lives.
#
# Usage:
#   bash chassis/scripts/memory-canary.sh [--customer-home PATH] [--json]
#
#   --json   emit one JSON object instead of human-readable text. Used by
#            gather-memory-canary.sh.
#
# Exit codes: 0 healthy, 1 canary failed, 2 harness error (bad args).
#
# Env:
#   MEMORY_CANARY_TIMEOUT   seconds per server invocation (default 90). `npx -y`
#                           on a cold cache is the slow case.
#   MEMORY_CANARY_ENTITY    reserved entity name (default health:memory-canary).
#
# Concurrency note: @modelcontextprotocol/server-memory rewrites the whole
# graph file on every mutation, so a canary write racing a Claude session write
# can lose one of the two. The window is milliseconds once a day against an
# event-driven writer; not worth a lock file, but it is why the canary keeps
# its entity to two observations and never touches anything else.

set -uo pipefail

CANARY_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=chassis/scripts/_memory-graph.sh
. "${CANARY_SCRIPT_DIR}/_memory-graph.sh"

CUSTOMER_HOME="${CUSTOMER_HOME:-${HOME}/.behalfbot}"
OUTPUT_JSON=false
TIMEOUT_SECS="${MEMORY_CANARY_TIMEOUT:-90}"
ENTITY="${MEMORY_CANARY_ENTITY:-health:memory-canary}"
ENTITY_TYPE="health"
# Fixed first observation so a human reading the graph knows what this is.
ENTITY_NOTE="monitor-owned liveness canary (chassis memory-canary heartbeat); not recall material"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --customer-home) CUSTOMER_HOME="$2"; shift 2 ;;
        --json) OUTPUT_JSON=true; shift ;;
        --help|-h)
            sed -n '1,60p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

STAGE=""
ERROR=""
NONCE=""
SERVER_CMD=""

# report <ok> - print the verdict and exit with the right code.
report() {
    local ok="$1"
    if [[ "$OUTPUT_JSON" == true ]]; then
        # jq is a hard dependency of memory_graph_resolve, so it is present by
        # the time anything can fail past the dependency check.
        jq -nc \
            --argjson ok "$ok" \
            --arg stage "$STAGE" \
            --arg error "$ERROR" \
            --arg resolved_path "${MEMORY_GRAPH_PATH:-}" \
            --arg shape "${MEMORY_GRAPH_SHAPE:-}" \
            --arg entity "$ENTITY" \
            --arg nonce "$NONCE" \
            --arg server_cmd "$SERVER_CMD" \
            --arg customer_home "$CUSTOMER_HOME" \
            --arg ts_utc "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            '{ok:$ok, stage:$stage, error:$error, resolved_path:$resolved_path,
              shape:$shape, entity:$entity, nonce:$nonce, server_cmd:$server_cmd,
              customer_home:$customer_home, ts_utc:$ts_utc}'
    elif [[ "$ok" == "true" ]]; then
        printf 'memory-canary OK\n'
        printf '  graph:  %s (%s)\n' "${MEMORY_GRAPH_PATH:-}" "${MEMORY_GRAPH_SHAPE:-}"
        printf '  entity: %s\n' "$ENTITY"
        printf '  nonce:  %s written and read back in a separate server invocation\n' "$NONCE"
    else
        printf 'memory-canary FAILED at stage: %s\n' "$STAGE"
        printf '  graph:  %s (%s)\n' "${MEMORY_GRAPH_PATH:-unresolved}" "${MEMORY_GRAPH_SHAPE:-unresolved}"
        printf '  error:  %s\n' "$ERROR"
        [[ -n "$SERVER_CMD" ]] && printf '  spawn:  cwd=%s %s\n' "$CUSTOMER_HOME" "$SERVER_CMD"
    fi
    [[ "$ok" == "true" ]] && exit 0 || exit 1
}

die() { STAGE="$1"; ERROR="$2"; report false; }

# ------------------------------------------------------------------
# JSON-RPC over stdio against the configured memory server.
# ------------------------------------------------------------------

# spawn_memory_server <requests-file> <stdout-file> <stderr-file>
#
# One invocation = one OS process. Requests are newline-delimited JSON-RPC;
# the server answers them in order and exits on stdin EOF. Every call to this
# function is a fresh process by construction - that is the separate-invocation
# guarantee, not a convention the caller has to remember.
spawn_memory_server() {
    local req="$1" out="$2" err="$3"
    local -a runner=()
    if command -v timeout >/dev/null 2>&1; then
        runner=(timeout "$TIMEOUT_SECS")
    fi
    (
        cd "$CUSTOMER_HOME" || exit 127
        if [[ ${#MEMORY_SERVER_ENV[@]} -gt 0 ]]; then
            exec env "${MEMORY_SERVER_ENV[@]}" \
                ${runner[0]+"${runner[@]}"} "${MEMORY_SERVER_ARGV[@]}" \
                <"$req" >"$out" 2>"$err"
        else
            exec ${runner[0]+"${runner[@]}"} "${MEMORY_SERVER_ARGV[@]}" \
                <"$req" >"$out" 2>"$err"
        fi
    )
}

# Two framing lines every session needs before any tools/call.
handshake_lines() {
    printf '%s\n' \
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"memory-canary","version":"1"}}}' \
        '{"jsonrpc":"2.0","method":"notifications/initialized"}'
}

# tool_call_line <id> <tool> <arguments-json>
tool_call_line() {
    jq -nc --argjson id "$1" --arg name "$2" --argjson args "$3" \
        '{jsonrpc:"2.0", id:$id, method:"tools/call", params:{name:$name, arguments:$args}}'
}

# rpc_field <stdout-file> <id> <jq-filter> - first non-empty match for that id.
#
# Line-by-line rather than one jq pass over the file: a server that prints a
# stray non-JSON line to stdout would make a whole-file jq parse emit nothing,
# turning a recoverable read into a false "server never answered".
rpc_field() {
    local f="$1" id="$2" filter="$3" line out
    [[ -f "$f" ]] || return 0
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        out="$(printf '%s' "$line" | jq -c --argjson id "$id" \
            "select(type==\"object\" and .id==\$id) | $filter" 2>/dev/null)"
        if [[ -n "$out" && "$out" != "null" ]]; then
            printf '%s' "$out"
            return 0
        fi
    done < "$f"
}

# rpc_result <stdout-file> <id> - the `result` object for that id, or empty.
rpc_result() { rpc_field "$1" "$2" '.result // empty'; }

# rpc_error_text <stdout-file> <id> - a protocol-level error message, or empty.
rpc_error_text() { rpc_field "$1" "$2" '.error.message // empty' | jq -r . 2>/dev/null; }

# tool_payload <result-json> - the structured payload of a tools/call result.
# Prefers structuredContent; falls back to parsing content[0].text, which is
# what older server builds return.
tool_payload() {
    local res="$1" payload
    payload="$(printf '%s' "$res" | jq -c '.structuredContent // empty' 2>/dev/null)"
    if [[ -z "$payload" || "$payload" == "null" ]]; then
        payload="$(printf '%s' "$res" | jq -r '.content[0].text // empty' 2>/dev/null | jq -c . 2>/dev/null)"
    fi
    printf '%s' "$payload"
}

# ------------------------------------------------------------------
# Stage 1 - resolve config
# ------------------------------------------------------------------

if ! memory_graph_resolve "$CUSTOMER_HOME"; then
    die "resolve" "$MEMORY_GRAPH_ERROR"
fi
SERVER_CMD="$(printf '%q ' "${MEMORY_SERVER_ARGV[@]}" | sed 's/ $//')"

# ------------------------------------------------------------------
# Stage 2 - precheck the resolved path in THIS namespace
#
# The original bug is caught here, and caught with the path in the first line
# of the message rather than a generic "memory broken". A path that does not
# exist in this namespace is almost always one baked in the other namespace.
# ------------------------------------------------------------------

GRAPH_PARENT="$(dirname "$MEMORY_GRAPH_PATH")"
if [[ ! -d "$GRAPH_PARENT" ]]; then
    die "precheck" "memory graph dir does not exist in this namespace: $GRAPH_PARENT (shape: $MEMORY_GRAPH_SHAPE). A path baked on the host is not valid inside the container, and vice versa."
fi
if ! touch "$MEMORY_GRAPH_PATH" 2>/dev/null; then
    die "precheck" "memory graph not writable in this namespace: $MEMORY_GRAPH_PATH (shape: $MEMORY_GRAPH_SHAPE)"
fi

# ------------------------------------------------------------------
# Stage 3 - write, in invocations 1 and 2
# ------------------------------------------------------------------

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Nonce is per-run, not just a timestamp: a read that returns yesterday's value
# is a stale-read failure and must not pass. Time component keeps it readable.
NONCE="$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM}${RANDOM}"

WRITE_ARGS_DELETE="$(jq -nc --arg e "$ENTITY" '{entityNames:[$e]}')"
WRITE_ARGS_CREATE="$(jq -nc --arg e "$ENTITY" --arg t "$ENTITY_TYPE" --arg note "$ENTITY_NOTE" --arg n "$NONCE" \
    '{entities:[{name:$e, entityType:$t, observations:[$note, ("nonce " + $n)]}]}')"

# Invocation 1: clear the entity, so it stays exactly two observations wide
# instead of growing one per day, and so create_entities in the NEXT invocation
# returns the entity rather than filtering it out as already-present.
{
    handshake_lines
    tool_call_line 2 delete_entities "$WRITE_ARGS_DELETE"
} > "$TMP/clear.req"

spawn_memory_server "$TMP/clear.req" "$TMP/clear.out" "$TMP/clear.err"
CLEAR_RC=$?

if [[ $CLEAR_RC -ne 0 ]]; then
    die "write" "memory server exited $CLEAR_RC on the clear invocation ($SERVER_CMD, cwd $CUSTOMER_HOME): $(tr '\n' ' ' < "$TMP/clear.err" | head -c 400)"
fi
if [[ -z "$(rpc_result "$TMP/clear.out" 1)" ]]; then
    die "write" "memory server never answered initialize ($SERVER_CMD, cwd $CUSTOMER_HOME): $(tr '\n' ' ' < "$TMP/clear.err" | head -c 400)"
fi
CLEAR_ERR="$(rpc_error_text "$TMP/clear.out" 2)"
if [[ -n "$CLEAR_ERR" ]]; then
    die "write" "delete_entities returned a JSON-RPC error: $CLEAR_ERR"
fi

# Invocation 2: write the nonce.
{
    handshake_lines
    tool_call_line 2 create_entities "$WRITE_ARGS_CREATE"
} > "$TMP/write.req"

spawn_memory_server "$TMP/write.req" "$TMP/write.out" "$TMP/write.err"
WRITE_RC=$?

if [[ $WRITE_RC -ne 0 ]]; then
    die "write" "memory server exited $WRITE_RC on the write invocation ($SERVER_CMD, cwd $CUSTOMER_HOME): $(tr '\n' ' ' < "$TMP/write.err" | head -c 400)"
fi

INIT_RESULT="$(rpc_result "$TMP/write.out" 1)"
if [[ -z "$INIT_RESULT" ]]; then
    die "write" "memory server never answered initialize ($SERVER_CMD): $(tr '\n' ' ' < "$TMP/write.err" | head -c 400)"
fi

CREATE_ERR="$(rpc_error_text "$TMP/write.out" 2)"
if [[ -n "$CREATE_ERR" ]]; then
    die "write" "create_entities returned a JSON-RPC error: $CREATE_ERR"
fi
CREATE_RESULT="$(rpc_result "$TMP/write.out" 2)"
if [[ -z "$CREATE_RESULT" ]]; then
    die "write" "create_entities returned no result for $ENTITY"
fi
if [[ "$(printf '%s' "$CREATE_RESULT" | jq -r '.isError // false')" == "true" ]]; then
    die "write" "create_entities reported isError: $(printf '%s' "$CREATE_RESULT" | jq -r '.content[0].text // "no detail"' | head -c 300)"
fi
CREATE_PAYLOAD="$(tool_payload "$CREATE_RESULT")"
# The payload is {entities:[...]} (structured) or a bare [...] (content text).
CREATED_NAME="$(printf '%s' "$CREATE_PAYLOAD" | jq -r --arg e "$ENTITY" \
    'if type=="array" then . else (.entities // []) end | map(select(.name==$e)) | .[0].name // empty' 2>/dev/null)"
if [[ -z "$CREATED_NAME" ]]; then
    die "write" "create_entities did not report creating $ENTITY. A prior canary entity survived delete_entities, so writes are not persisting to $MEMORY_GRAPH_PATH."
fi

# ------------------------------------------------------------------
# Stage 4 - read back, in invocation 3 (a separate process)
# ------------------------------------------------------------------

{
    handshake_lines
    tool_call_line 2 open_nodes "$(jq -nc --arg e "$ENTITY" '{names:[$e]}')"
} > "$TMP/read.req"

spawn_memory_server "$TMP/read.req" "$TMP/read.out" "$TMP/read.err"
READ_RC=$?

if [[ $READ_RC -ne 0 ]]; then
    die "read" "memory server exited $READ_RC on the read-back invocation: $(tr '\n' ' ' < "$TMP/read.err" | head -c 400)"
fi

READ_ERR="$(rpc_error_text "$TMP/read.out" 2)"
if [[ -n "$READ_ERR" ]]; then
    die "read" "open_nodes returned a JSON-RPC error: $READ_ERR"
fi
READ_RESULT="$(rpc_result "$TMP/read.out" 2)"
if [[ -z "$READ_RESULT" ]]; then
    die "read" "open_nodes returned no result on the read-back invocation"
fi
READ_PAYLOAD="$(tool_payload "$READ_RESULT")"
READ_COUNT="$(printf '%s' "$READ_PAYLOAD" | jq -r 'if type=="array" then length else ((.entities // []) | length) end' 2>/dev/null)"
[[ -z "$READ_COUNT" ]] && READ_COUNT=0

if [[ "$READ_COUNT" -eq 0 ]]; then
    die "read" "read-back came back EMPTY for $ENTITY despite a create_entities that reported success. The write and the read are not hitting the same file: resolved path $MEMORY_GRAPH_PATH (shape: $MEMORY_GRAPH_SHAPE), spawn cwd $CUSTOMER_HOME."
fi

READ_NONCE="$(printf '%s' "$READ_PAYLOAD" \
    | jq -r --arg e "$ENTITY" 'if type=="array" then . else (.entities // []) end
        | map(select(.name==$e)) | .[0].observations // []
        | map(select(startswith("nonce "))) | .[0] // empty' 2>/dev/null)"
READ_NONCE="${READ_NONCE#nonce }"

if [[ -z "$READ_NONCE" ]]; then
    die "read" "read-back found $ENTITY but it carries no nonce observation; the graph at $MEMORY_GRAPH_PATH is not holding what was written."
fi
if [[ "$READ_NONCE" != "$NONCE" ]]; then
    die "read" "read-back nonce MISMATCH for $ENTITY: wrote '$NONCE', read '$READ_NONCE'. The read is served from a different or stale graph than the write (resolved path $MEMORY_GRAPH_PATH)."
fi

STAGE="verified"
report true
