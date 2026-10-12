#!/usr/bin/env bash
# _memory-graph.sh - shared resolution of the memory MCP server out of .mcp.json.
#
# Sourced, never executed. Two consumers:
#
#   bootstrap-audit.sh Gap 3  - install-time reachability: does the configured
#                               graph resolve to a path that exists and is
#                               writable IN THIS NAMESPACE.
#   memory-canary.sh          - continuous liveness: can this install actually
#                               write and read back its own memory, via the
#                               exact command .mcp.json declares.
#
# Both need the same three facts (resolved graph path, config shape, spawn
# argv), and the path resolution is the subtle part - it is what #142 got
# wrong. Keeping one implementation means a fix to the resolution logic lands
# in the audit and the canary at the same time.
#
# Exported on success:
#   MEMORY_GRAPH_PATH   absolute path to the resolved graph file
#   MEMORY_GRAPH_SHAPE  human label for which config shape was found
#   MEMORY_SERVER_ARGV  array: command + args, verbatim from .mcp.json
#   MEMORY_SERVER_ENV   array: KEY=VALUE env pairs, tokens already expanded
# On failure:
#   MEMORY_GRAPH_ERROR  one-line reason
#
# Bash 3.2 compatible (macOS ships 3.2): no mapfile, no associative arrays.
#
# shellcheck disable=SC2034  # the MEMORY_* outputs are consumed by the sourcer

# Substitute ${CHASSIS_HOME} / ${CUSTOMER_HOME}, including the ${VAR:-default}
# form, in a path read out of .mcp.json. Deliberately limited to those two
# names: the config is data, and a generic expansion would evaluate whatever a
# hand-edited .mcp.json happens to contain. A set env var wins; otherwise the
# inline default is used; otherwise the same fallback bootstrap.sh assumes.
expand_home_tokens() {
    local s="$1"
    local re='\$\{(CHASSIS_HOME|CUSTOMER_HOME)(:-[^}]*)?\}'
    local i=0
    while [[ $i -lt 10 && "$s" =~ $re ]]; do
        i=$((i + 1))
        local token="${BASH_REMATCH[0]}"
        local name="${BASH_REMATCH[1]}"
        local inline="${BASH_REMATCH[2]:-}"
        local value=""
        case "$name" in
            CHASSIS_HOME)  value="${CHASSIS_HOME:-}" ;;
            CUSTOMER_HOME) value="${CUSTOMER_HOME:-}" ;;
        esac
        if [[ -z "$value" ]]; then
            if [[ -n "$inline" ]]; then
                value="${inline#:-}"
            elif [[ "$name" == "CHASSIS_HOME" ]]; then
                value="${HOME}/behalfbot"
            fi
        fi
        s="${s//"$token"/$value}"
    done
    printf '%s' "$s"
}

# memory_mcp_enabled <customer_home>
#
# Returns 0 when this install runs the memory MCP, 1 when chassis.config.yaml
# says `memory.mcp_enabled: false`. The memory MCP is optional (#219): an
# install that keeps its long-term facts in the second brain and its
# corrections in the auto-memory notes store can retire the graph, and once it
# does, Gap 3 and the canary can only fail.
#
# Anything short of an explicit false means enabled: no config file, no
# `memory:` block, no key, an unreadable file, a parser that is not installed.
# An install that never heard of the flag must behave exactly as before, so
# every failure to read it falls toward the old behaviour, never away from it.
#
# Exported: MEMORY_MCP_FLAG_SOURCE, a human label for where the verdict came
# from (used in the skip messages so an operator can find the setting).
memory_mcp_enabled() {
    local customer_home="$1"
    local cfg="$customer_home/chassis.config.yaml"
    MEMORY_MCP_FLAG_SOURCE="default (no memory.mcp_enabled in $cfg)"
    [[ -f "$cfg" ]] || return 0

    local v=""
    if command -v python3 >/dev/null 2>&1; then
        v="$(python3 - "$cfg" <<'PY' 2>/dev/null
import sys
try:
    import yaml
except ImportError:
    sys.exit(3)
try:
    with open(sys.argv[1]) as f:
        c = yaml.safe_load(f) or {}
    m = c.get("memory") if isinstance(c, dict) else None
    v = m.get("mcp_enabled") if isinstance(m, dict) else None
    sys.stdout.write("false" if v is False else "true")
except Exception:
    sys.stdout.write("true")
PY
)"
    fi
    if [[ -z "$v" ]]; then
        # No python3 or no PyYAML: read the canonical two-space layout of the
        # shipped config. Gives up toward "enabled" on anything else.
        v="$(awk '
            /^memory:/ {in_block=1; next}
            in_block && /^[^ #]/ {exit}
            in_block && /^  mcp_enabled:/ {
                sub(/^  mcp_enabled:[ \t]*/, ""); sub(/[ \t]*#.*$/, "");
                gsub(/["\047]/, ""); print tolower($0); exit
            }
        ' "$cfg" 2>/dev/null)"
        [[ "$v" == "false" || "$v" == "no" || "$v" == "off" ]] && v="false" || v="true"
    fi
    if [[ "$v" == "false" ]]; then
        MEMORY_MCP_FLAG_SOURCE="memory.mcp_enabled: false in $cfg"
        return 1
    fi
    return 0
}

# memory_graph_resolve <customer_home>
#
# Reads $customer_home/.mcp.json and works out where the memory graph lives.
# Two config shapes are legitimate, so resolve the path rather than asserting
# a shape:
#
#   legacy  - env.MEMORY_FILE_PATH baked into the block, possibly carrying a
#             ${CHASSIS_HOME} / ${CUSTOMER_HOME} token.
#   current - no env block at all. The template wraps the server in
#             `sh -c 'export MEMORY_FILE_PATH="$(pwd)/memory/memory.jsonl"; ...'`
#             because the host and the chassis container see the same
#             bind-mounted directory at different absolute paths, so a single
#             baked absolute path is valid in only one namespace. Claude Code
#             spawns the server with cwd = the customer root in both, so the
#             resolved graph is $CUSTOMER_HOME/memory/memory.jsonl.
#
# Returns 0 on success, 1 with MEMORY_GRAPH_ERROR set otherwise.
memory_graph_resolve() {
    local customer_home="$1"
    local mcp="$customer_home/.mcp.json"

    MEMORY_GRAPH_PATH=""
    MEMORY_GRAPH_SHAPE=""
    MEMORY_GRAPH_ERROR=""
    MEMORY_SERVER_ARGV=()
    MEMORY_SERVER_ENV=()

    if [[ ! -f "$mcp" ]]; then
        MEMORY_GRAPH_ERROR=".mcp.json not found at $mcp"
        return 1
    fi
    if ! command -v jq >/dev/null 2>&1; then
        MEMORY_GRAPH_ERROR="jq not installed; cannot read $mcp"
        return 1
    fi
    if ! jq -e . "$mcp" >/dev/null 2>&1; then
        MEMORY_GRAPH_ERROR="$mcp is not valid JSON"
        return 1
    fi

    local mem_block
    mem_block="$(jq -c '.mcpServers.memory // empty' "$mcp" 2>/dev/null)"
    if [[ -z "$mem_block" || "$mem_block" == "null" ]]; then
        MEMORY_GRAPH_ERROR="mcpServers.memory missing from $mcp"
        return 1
    fi

    local mem_path
    mem_path="$(printf '%s' "$mem_block" | jq -r '.env.MEMORY_FILE_PATH // empty')"
    if [[ -n "$mem_path" ]]; then
        MEMORY_GRAPH_SHAPE="env.MEMORY_FILE_PATH"
    else
        mem_path="memory/memory.jsonl"
        MEMORY_GRAPH_SHAPE="cwd-resolved, no env block"
    fi

    local expanded
    expanded="$(expand_home_tokens "$mem_path")"
    # A relative path is resolved against the launch cwd, which is the customer
    # root for both the host and the container.
    [[ "$expanded" != /* ]] && expanded="$customer_home/$expanded"
    MEMORY_GRAPH_PATH="$expanded"

    # Spawn argv, verbatim. The canary must run what Claude Code runs; a
    # reimplementation would test the reimplementation, not the install.
    local line
    while IFS= read -r line; do
        MEMORY_SERVER_ARGV+=("$line")
    done < <(printf '%s' "$mem_block" | jq -r '([.command // empty] + (.args // []))[]')

    if [[ ${#MEMORY_SERVER_ARGV[@]} -eq 0 ]]; then
        MEMORY_GRAPH_ERROR="mcpServers.memory has no command in $mcp"
        return 1
    fi

    # Env pairs get the same token expansion the path check applies, because
    # that is what makes the audit's verdict and the canary's spawn agree. A
    # literal unexpanded ${CUSTOMER_HOME} reaching the server would be a
    # different failure than the one the audit reported.
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local k="${line%%=*}"
        local v="${line#*=}"
        MEMORY_SERVER_ENV+=("$k=$(expand_home_tokens "$v")")
    done < <(printf '%s' "$mem_block" | jq -r '(.env // {}) | to_entries[] | "\(.key)=\(.value)"')

    return 0
}
