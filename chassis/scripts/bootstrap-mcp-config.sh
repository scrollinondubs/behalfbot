#!/usr/bin/env bash
# bootstrap-mcp-config.sh — hydrate $CHASSIS_HOME/.mcp.json from the chassis
# template at install time.
#
# Why this exists
# ===============
# `.mcp.json` is gitignored (it holds raw API tokens for MCP servers like
# GitHub, Brave, Tavily, Turso, Oura, etc.). When a customer install is
# cloned, migrated, or cutover-recovered the file is missing and Claude
# Code in the chassis container bombs out with:
#
#     Error: Invalid MCP configuration:
#     MCP config file not found: /app/customer/.mcp.json
#
# Every gather that fires `claude -p` (morning-briefing, github-issue-triage,
# strava-ingest, daily-log, etc.) fails the dispatcher's circuit-breaker
# after two retries, and the entire heartbeat cycle goes silent. Confirmed
# concretely 2026-05-25 in the <v1-reference-install>-mac-mini cutover — see
# <v1-reference-install>#698 and scrollinondubs/new-jaxity#62.
#
# What it does
# ============
# 1. Reads `$CHASSIS_HOME/chassis/.mcp.json.template` (canonical template
#    shipped with chassis).
# 2. Sources `$CHASSIS_HOME/.env` so the bw / Vaultwarden hydration block
#    pulls every secret into the environment. (Falls back gracefully when
#    Vaultwarden isn't reachable — placeholder stays in the output for
#    Sean to fill manually.)
# 3. Substitutes each `<PLACEHOLDER>` against the env var of the same name,
#    plus shell-expands `${CHASSIS_HOME}` literally.
# 4. Strips the `_README` and other `_*` comment keys (they aren't valid
#    JSON-Schema fields anyway — Claude Code ignores them but `jq`
#    consumers would see noise).
# 5. Validates the result parses as JSON.
# 6. Atomically writes `$CHASSIS_HOME/.mcp.json` with 0600 perms (file
#    contains long-lived API tokens).
#
# Safety
# ======
# - Refuses to overwrite an existing `.mcp.json` unless invoked with
#   --force. The use case is install-time bootstrap, NOT routine refresh.
#   If you need to rotate a token, edit `.mcp.json` directly or rotate in
#   Vaultwarden + re-run with --force.
# - Never logs token values. Logs only WHICH placeholders were filled vs
#   left as `<PLACEHOLDER>` strings.
# - File written with `umask 077` to land at 0600.
#
# Usage
# =====
#   bash chassis/scripts/bootstrap-mcp-config.sh              # idempotent — skips if file exists
#   bash chassis/scripts/bootstrap-mcp-config.sh --force      # rewrite even if file exists
#   bash chassis/scripts/bootstrap-mcp-config.sh --dry-run    # print result to stdout, don't write
#
# Related
# =======
# - docs/mcp-setup.md — per-MCP install instructions, template walkthrough
# - chassis/.mcp.json.template — canonical template + placeholder list
# - <v1-reference-install>#698 — cutover punchlist that motivated this script

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (export it before running, or invoke from the install runbook)}"
# Issue #6: prefer CUSTOMER_HOME for .env / .mcp.json output; CHASSIS_HOME
# fallback keeps legacy installs working.
: "${CUSTOMER_HOME:=$CHASSIS_HOME}"
export CHASSIS_HOME CUSTOMER_HOME

# Resolve template relative to THIS script's location. Chassis is reachable
# through two layouts:
#   - vendored install: $CHASSIS_HOME/chassis/chassis/.mcp.json.template
#     (customer's $CUSTOMER_HOME has chassis at chassis/, which itself has
#     a top-level chassis/ subdir for vendor-friendly nesting)
#   - standalone chassis repo: $CHASSIS_REPO/chassis/.mcp.json.template
#     (no vendor wrap)
# Either way, the template sits one directory above this script's parent.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR%/scripts}/.mcp.json.template"
TARGET="$CUSTOMER_HOME/.mcp.json"
ENV_FILE="$CUSTOMER_HOME/.env"

FORCE=0
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help)
            grep -E "^# (Usage|  bash)" "$0" | sed 's/^# //'
            exit 0
            ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ ! -f "$TEMPLATE" ]]; then
    echo "ERROR: template missing — $TEMPLATE" >&2
    echo "  Is chassis vendored at \$CHASSIS_HOME/chassis/?" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Delegate to hydrate-mcp-json.py when we can.
#
# The sed+jq path below cannot evaluate `_enable_when`: it strips the key and
# registers the server anyway. That was survivable while the gated servers were
# things like brave-search, where the worst case is a server nobody calls. It
# stopped being survivable with the Google entries (#57), which would otherwise
# get registered on every install that recovers through this script - including
# installs that never enabled Google and have no OAuth token on disk.
#
# hydrate-mcp-json.py is the renderer bootstrap.sh already uses and the one the
# test suite covers. Use it here too, so both paths honor one grammar. The sed
# path stays as the fallback for the case it was written for: a broken install
# where chassis.config.yaml is missing and .mcp.json must be reconstructed.
# ---------------------------------------------------------------------------
HYDRATOR="$SCRIPT_DIR/hydrate-mcp-json.py"
CONFIG="$CUSTOMER_HOME/chassis.config.yaml"

if [[ -f "$HYDRATOR" && -f "$CONFIG" ]] && command -v python3 >/dev/null 2>&1 \
   && python3 -c 'import yaml' 2>/dev/null; then
    hydrator_args=(--config "$CONFIG" --template "$TEMPLATE")
    [[ -f "$ENV_FILE" ]] && hydrator_args+=(--env "$ENV_FILE")

    if [[ $DRY_RUN -eq 1 ]]; then
        python3 "$HYDRATOR" "${hydrator_args[@]}" --output "$TARGET" --dry-run
        exit 0
    fi

    if [[ -f "$TARGET" && $FORCE -eq 0 ]]; then
        echo "skip: $TARGET already exists; pass --force to overwrite" >&2
        exit 0
    fi

    rc=0
    ( umask 077; python3 "$HYDRATOR" "${hydrator_args[@]}" --output "$TARGET" ) || rc=$?
    # Exit 2 means the file was written with <PLACEHOLDER> tokens still in it.
    # Loud, not fatal - same contract bootstrap.sh applies.
    if [[ $rc -eq 0 || $rc -eq 2 ]]; then
        chmod 600 "$TARGET" 2>/dev/null || true
        exit "$rc"
    fi
    echo "ERROR: hydrate-mcp-json.py exited $rc" >&2
    exit "$rc"
fi

echo "WARN: falling back to the sed+jq renderer (no chassis.config.yaml, or no" >&2
echo "      python3+PyYAML). It cannot evaluate _enable_when, so feature-gated" >&2
echo "      servers are registered unconditionally. Review $TARGET before use." >&2

if [[ -f "$TARGET" && $FORCE -eq 0 && $DRY_RUN -eq 0 ]]; then
    echo "skip: $TARGET already exists; pass --force to overwrite" >&2
    exit 0
fi

# Source .env to pull in Vaultwarden-hydrated secrets. Tolerate partial
# hydration failures — any unset env var becomes a `<PLACEHOLDER>` left in
# the output, which is louder than failing silently.
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    source "$ENV_FILE" 2>/dev/null || true
    set +a
fi

# Discover placeholders in the template — each `<NAME>` becomes a substitution
# target. Match only [A-Z_]+ to avoid false hits on the README's `<PLACEHOLDER>`.
# Avoid `mapfile` (bash 4+ only — macOS ships bash 3.2 at /bin/bash).
PLACEHOLDERS=()
while IFS= read -r line; do
    PLACEHOLDERS+=("$line")
done < <(grep -oE '<[A-Z_]+>' "$TEMPLATE" | sort -u)

# Build the output via a single sed pass. Skip `<PLACEHOLDER>` (used in the
# README copy as a literal example, not a real substitution target).
OUTPUT=$(cat "$TEMPLATE")

filled=()
unfilled=()
for ph in "${PLACEHOLDERS[@]}"; do
    var_name="${ph#<}"
    var_name="${var_name%>}"
    [[ "$var_name" == "PLACEHOLDER" ]] && continue
    value="${!var_name:-}"
    if [[ -n "$value" ]]; then
        # Escape the value for sed: backslashes, ampersands, and the chosen
        # delimiter (|). Tokens never legitimately contain `|` so this is safe.
        escaped=$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')
        OUTPUT=$(printf '%s' "$OUTPUT" | sed "s|<$var_name>|$escaped|g")
        filled+=("$var_name")
    else
        unfilled+=("$var_name")
    fi
done

# Shell-expand ${CHASSIS_HOME} and ${CUSTOMER_HOME} (kept literal in the
# template so the file is self-documenting). Only substitute the exact tokens.
OUTPUT=$(printf '%s' "$OUTPUT" | sed -e "s|\${CHASSIS_HOME}|$CHASSIS_HOME|g" -e "s|\${CUSTOMER_HOME}|$CUSTOMER_HOME|g")

# Strip template-only metadata: the top-level `_README`, every `_*` key inside a
# server entry, and the `_*` divider entries that are section headers rather than
# servers. Match on the underscore prefix, not on a hand-maintained list of names -
# the old `del(._role) | del(._enable_when) | del(._install_note)` form silently
# leaked every key nobody remembered to add to it.
if command -v jq >/dev/null 2>&1; then
    OUTPUT=$(printf '%s' "$OUTPUT" | jq '
        del(._README)
        | .mcpServers |= (
            with_entries(select(.key | startswith("_") | not))
            | map_values(with_entries(select(.key | startswith("_") | not)))
        )')
fi

# Validate the result is parseable JSON before writing.
if ! printf '%s' "$OUTPUT" | jq empty 2>/dev/null; then
    echo "ERROR: hydrated output failed JSON validation" >&2
    echo "  template: $TEMPLATE" >&2
    echo "  target: $TARGET" >&2
    echo "  unfilled placeholders: ${unfilled[*]:-none}" >&2
    exit 3
fi

if [[ $DRY_RUN -eq 1 ]]; then
    printf '%s\n' "$OUTPUT"
    echo "" >&2
    echo "=== bootstrap-mcp-config dry-run summary ===" >&2
    echo "  filled:   ${filled[*]:-none}" >&2
    echo "  unfilled: ${unfilled[*]:-none}" >&2
    exit 0
fi

# Atomic write with 0600. The bw-hydrated env may contain API tokens that
# should never land on disk world-readable.
umask 077
TMP="${TARGET}.tmp"
printf '%s\n' "$OUTPUT" > "$TMP"
mv "$TMP" "$TARGET"
chmod 600 "$TARGET"

echo "wrote $TARGET"
echo "  filled:   ${#filled[@]} placeholders (${filled[*]:-none})"
if [[ ${#unfilled[@]} -gt 0 ]]; then
    echo "  unfilled: ${#unfilled[@]} placeholders (${unfilled[*]})"
    echo "  → these stayed as <NAME> strings; fill manually or hydrate via Vaultwarden + rerun with --force"
fi
