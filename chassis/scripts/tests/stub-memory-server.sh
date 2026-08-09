#!/usr/bin/env bash
# stub-memory-server.sh - minimal stand-in for @modelcontextprotocol/server-memory.
#
# Test support for test-memory-canary.sh. Implements the four pieces of the MCP
# stdio protocol the canary uses (initialize, delete_entities, create_entities,
# open_nodes) over the same JSONL graph format the real server uses, so the
# suite runs with no network and no npx.
#
# The one property that MUST match the real server: state lives in the file at
# $MEMORY_FILE_PATH and nowhere else. The canary's whole point is that the read
# happens in a different process from the write, so a stub that kept state in
# memory would silently void the guarantee the suite exists to prove.
#
# Env:
#   MEMORY_FILE_PATH     required. Graph file, JSONL, same shape as the real one.
#   STUB_PID_LOG         optional. Appends this process's PID on startup, so a
#                        test can assert the canary really spawned two processes.
#   STUB_IGNORE_DELETE   optional. When 1, delete_entities reports success but
#                        changes nothing - simulates a write that does not
#                        persist while the server keeps saying it worked.

set -uo pipefail

GRAPH="${MEMORY_FILE_PATH:-}"
if [[ -z "$GRAPH" ]]; then
    echo "stub-memory-server: MEMORY_FILE_PATH unset" >&2
    exit 64
fi

[[ -n "${STUB_PID_LOG:-}" ]] && printf '%s\n' "$$" >> "$STUB_PID_LOG"

echo "stub memory server running on stdio" >&2

load_graph() { [[ -r "$GRAPH" ]] && cat "$GRAPH" || true; }

respond_result() {
    # $1 = id (raw JSON), $2 = text content, $3 = structuredContent JSON
    jq -nc --argjson id "$1" --arg text "$2" --argjson structured "$3" \
        '{jsonrpc:"2.0", id:$id, result:{content:[{type:"text", text:$text}], structuredContent:$structured}}'
}

handle_tool() {
    local id="$1" tool="$2" args="$3"
    case "$tool" in
        delete_entities)
            if [[ "${STUB_IGNORE_DELETE:-0}" != "1" ]]; then
                local names kept
                names="$(printf '%s' "$args" | jq -c '.entityNames // []')"
                kept="$(load_graph | jq -c --argjson names "$names" \
                    'select((.type != "entity") or ((.name as $n | $names | index($n)) | not))')"
                if [[ -n "$kept" ]]; then
                    printf '%s\n' "$kept" > "$GRAPH"
                else
                    : > "$GRAPH"
                fi
            fi
            respond_result "$id" "Entities deleted successfully" '{"success":true,"message":"deleted"}'
            ;;
        create_entities)
            local ents existing created
            ents="$(printf '%s' "$args" | jq -c '.entities // []')"
            existing="$(load_graph | jq -sc '[.[] | select(.type=="entity") | .name]')"
            created="$(jq -nc --argjson ents "$ents" --argjson ex "$existing" \
                '[$ents[] | select((.name as $n | $ex | index($n)) | not)]')"
            printf '%s' "$created" | jq -c '.[] | {type:"entity", name:.name, entityType:.entityType, observations:.observations}' >> "$GRAPH"
            respond_result "$id" "$created" "$(jq -nc --argjson e "$created" '{entities:$e}')"
            ;;
        open_nodes)
            local names found
            names="$(printf '%s' "$args" | jq -c '.names // []')"
            found="$(load_graph | jq -sc --argjson names "$names" \
                '[.[] | select(.type=="entity") | select(.name as $n | $names | index($n)) | {name:.name, entityType:.entityType, observations:.observations}]')"
            respond_result "$id" "$found" "$(jq -nc --argjson e "$found" '{entities:$e, relations:[]}')"
            ;;
        *)
            jq -nc --argjson id "$id" --arg tool "$tool" \
                '{jsonrpc:"2.0", id:$id, error:{code:-32601, message:("unknown tool: " + $tool)}}'
            ;;
    esac
}

while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    method="$(printf '%s' "$line" | jq -r '.method // empty' 2>/dev/null)"
    case "$method" in
        initialize)
            id="$(printf '%s' "$line" | jq -c '.id // 0')"
            jq -nc --argjson id "$id" \
                '{jsonrpc:"2.0", id:$id, result:{protocolVersion:"2024-11-05", capabilities:{tools:{}}, serverInfo:{name:"stub-memory-server", version:"0.1"}}}'
            ;;
        tools/call)
            id="$(printf '%s' "$line" | jq -c '.id // 0')"
            tool="$(printf '%s' "$line" | jq -r '.params.name // empty')"
            args="$(printf '%s' "$line" | jq -c '.params.arguments // {}')"
            handle_tool "$id" "$tool" "$args"
            ;;
        *) : ;;
    esac
done

exit 0
