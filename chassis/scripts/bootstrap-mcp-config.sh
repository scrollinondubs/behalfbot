#!/usr/bin/env bash
# bootstrap-mcp-config.sh - hydrate $CHASSIS_HOME/.mcp.json from the chassis
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
# concretely 2026-05-25 in the <v1-reference-install>-mac-mini cutover - see
# <v1-reference-install>#698 and scrollinondubs/new-jaxity#62.
#
# What it does
# ============
# Resolves the canonical template + the customer's config and .env, then hands
# all of the rendering to `hydrate-mcp-json.py`. This script owns path
# resolution, the overwrite guard, and file permissions; the hydrator owns the
# template grammar (`_enable_when`, `_override_when`, `<PLACEHOLDER>`
# substitution, `_*` metadata stripping, JSON validation). There is exactly one
# renderer - see the delegation block below for why that matters.
#
# 1. Resolves `$CHASSIS_HOME/chassis/.mcp.json.template`.
# 2. Passes `$CUSTOMER_HOME/chassis.config.yaml` and `$CUSTOMER_HOME/.env` to
#    the hydrator, which substitutes each `<PLACEHOLDER>` from .env or the
#    environment. Unresolved CREDENTIAL placeholders are dropped rather than
#    emitted as literal bearer tokens; unresolved non-credential ones stay put
#    and exit 2 makes them loud.
# 3. Writes `$CUSTOMER_HOME/.mcp.json` under `umask 077`, chmod 0600 (file
#    contains long-lived API tokens).
#
# Safety
# ======
# - Refuses to overwrite an existing `.mcp.json` unless invoked with
#   --force. The use case is install-time bootstrap, NOT routine refresh.
#   If you need to rotate a token, edit `.mcp.json` directly or rotate in
#   Vaultwarden + re-run with --force.
# - Never logs token values. The hydrator names only the placeholders it could
#   NOT resolve, and the env keys it dropped for that reason.
# - File written with `umask 077` to land at 0600.
#
# Usage
# =====
#   bash chassis/scripts/bootstrap-mcp-config.sh              # idempotent - skips if file exists
#   bash chassis/scripts/bootstrap-mcp-config.sh --force      # rewrite even if file exists
#   bash chassis/scripts/bootstrap-mcp-config.sh --dry-run    # print result to stdout, don't write
#
# Related
# =======
# - docs/mcp-setup.md - per-MCP install instructions, template walkthrough
# - chassis/.mcp.json.template - canonical template + placeholder list
# - <v1-reference-install>#698 - cutover punchlist that motivated this script

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
    echo "ERROR: template missing - $TEMPLATE" >&2
    echo "  Is chassis vendored at \$CHASSIS_HOME/chassis/?" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Delegate to hydrate-mcp-json.py. Always.
#
# This script used to carry a second renderer: a sed+jq pass that ran whenever
# chassis.config.yaml or PyYAML was missing. It stripped `_enable_when` without
# evaluating it, so it registered every feature-gated server unconditionally -
# siyuan AND notion AND secondbrain on one install, plus the Google entries
# (#57) on installs that never enabled Google and have no OAuth token on disk.
# A recovery path that produces a config the primary path would never produce
# is not a fallback, it is a second product with its own bugs.
#
# It is gone. The two conditions that used to select it are handled inside the
# hydrator instead, where the `_enable_when` grammar and its tests already live:
#
#   no PyYAML   -> load_yaml falls back to the minimal parser shipped in
#                  chassis/second_brain/factory.py.
#   no config   -> hydrator renders against an empty config, which drops every
#                  `==`-gated server and keeps the ungated core. Minimal and
#                  correct beats complete and wrong.
#
# Related: PR #58 flagged this file as knowingly broken; this is the Stage 2 fix.
# ---------------------------------------------------------------------------
HYDRATOR="$SCRIPT_DIR/hydrate-mcp-json.py"
CONFIG="$CUSTOMER_HOME/chassis.config.yaml"

if [[ ! -f "$HYDRATOR" ]]; then
    echo "ERROR: renderer missing - $HYDRATOR" >&2
    echo "  chassis tree is incomplete; re-pull the subtree or rebuild the image." >&2
    exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not on PATH - hydrate-mcp-json.py cannot run." >&2
    exit 2
fi

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
