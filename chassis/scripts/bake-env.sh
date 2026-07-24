#!/usr/bin/env bash
# bake-env.sh — Bake the install's hydration-aware .env into a literal-only
# .env.baked file the chassis container reads via compose's env_file directive.
#
# Why this exists: the canonical install pattern keeps secrets in
# Vaultwarden + hydrates into RAM on-demand from the host's `.env` (via a
# sourced hydration block at the bottom of .env that runs
# scripts/hydrate-from-vaultwarden.sh). This works great on the host where
# `bw` CLI + an unlocked Vaultwarden session are reachable, but the chassis
# container intentionally never reaches Vaultwarden at runtime
# (per docker-compose.yml: "credentials baked into the bind-mounted .env so
# the dispatcher loop never needs to reach vaultwarden at runtime"). Instead
# we bake a literal `KEY=value` file ONCE at install + on each credential
# rotation, and the container reads that file at boot.
#
# This script automates the bake. Run it on the HOST (not inside the
# container) wherever the host's hydration-aware `.env` lives.
#
# Usage:
#   CHASSIS_HOME=/path/to/install bash chassis/scripts/bake-env.sh
#
#   # Re-bake after credential rotation:
#   CHASSIS_HOME=/path/to/install bash chassis/scripts/bake-env.sh
#
#   # Dry-run (print what would be written, don't touch .env.baked):
#   CHASSIS_HOME=/path/to/install DRY_RUN=true bash chassis/scripts/bake-env.sh
#
# Output: $CHASSIS_HOME/.env.baked (mode 0600, gitignored, NOT in S3 backups
# per chassis/scripts/backup-to-s3.sh which only copies .env itself encrypted).
#
# Security posture (the trade-off): see chassis/docs/hydration.md. The TL;DR:
# - Pattern A (host-side): secrets in RAM only via on-demand VW unlock
# - Pattern B (container-side, this script's output): secrets at rest in
#   .env.baked, mode 0600, on the same filesystem as VW's encrypted blob +
#   master pass
# - Apple Silicon Macs encrypt the SSD at rest via the Secure Enclave
#   regardless of FileVault state, so the at-rest delta between A and B is
#   smaller than instinct suggests
# - Anyone with file-read at uid-501 on the install host gets secrets
#   regardless of A vs B
#
# When to re-bake:
# - Any credential rotation (Discord bot token reset, GitHub PAT regen, etc.)
# - After editing `.env` directly (adding a new var, changing a literal)
# - Before bringing the chassis container up if .env.baked is missing or
#   older than $CHASSIS_HOME/.env

set -euo pipefail

# Issue #6: customer state - including .env / .env.baked - lives under
# CUSTOMER_HOME. CHASSIS_HOME is kept as the legacy fallback so pre-#6
# installs continue working without re-export.
: "${CHASSIS_HOME:?CHASSIS_HOME must be set (export to the chassis tree root)}"
: "${CUSTOMER_HOME:=$CHASSIS_HOME}"
export CHASSIS_HOME CUSTOMER_HOME

DRY_RUN="${DRY_RUN:-false}"
SRC_ENV="$CUSTOMER_HOME/.env"
DST_BAKED="$CUSTOMER_HOME/.env.baked"

if [[ ! -f "$SRC_ENV" ]]; then
    echo "ERR: $SRC_ENV not found" >&2
    echo "bake-env runs against the install's host-side .env. If the install hasn't" >&2
    echo "been bootstrapped yet, run bootstrap.sh first." >&2
    exit 2
fi

if [[ ! -r "$SRC_ENV" ]]; then
    echo "ERR: $SRC_ENV is not readable by the current user (uid $(id -u))" >&2
    exit 2
fi

# Snapshot pre-source env so we can subtract it from the post-source dump.
# This isolates only the vars the .env actually defines.
PRE_ENV_FILE=$(mktemp)
POST_ENV_FILE=$(mktemp)
TMP_BAKED=$(mktemp)
trap 'rm -f "$PRE_ENV_FILE" "$POST_ENV_FILE" "$TMP_BAKED"' EXIT

env | sort > "$PRE_ENV_FILE"

# Source .env in a fresh subshell + dump resulting env. set -a auto-exports
# every var the .env sets so they all reach the env dump. The 2>/dev/null
# silences any chatter from hydration scripts (bw prompts, etc.). A failed
# hydration block doesn't abort the bake — we still write what we have, but
# warn the user.
if ! bash -c "set -a; source '$SRC_ENV' 2>/dev/null || true; set +a; env" | sort > "$POST_ENV_FILE"; then
    echo "WARN: source '$SRC_ENV' returned non-zero — baked file may be partial" >&2
fi

# Diff to find ONLY the vars the .env sourcing introduced.
NEW_VARS=$(comm -13 "$PRE_ENV_FILE" "$POST_ENV_FILE")

# Filter shell-internal noise. Keep only ALL_CAPS keys (the chassis convention
# for env-var names). Drop subprocess artifacts that always change across
# invocations (PWD, SHLVL, _, etc.) so the diff is stable.
#
# Also explicitly EXCLUDE dispatcher-toxic vars that must never appear in the
# baked file. If they did, claude -p inside the chassis container would prefer
# them over the OAuth credentials chain, billing PAYG with potentially-stale
# keys or hitting the wrong API endpoint. See chassis/docs/hydration.md +
# chassis/scheduled-tasks/heartbeat-dispatcher.sh lines 67-83 for the
# rationale on keeping these unset at runtime.
#
# Concrete incident 2026-05-22 (scrollinondubs/new-jaxity 3h BFL pipeline
# outage): a stale ANTHROPIC_API_KEY leaked into Sean's .env.baked from some
# transient shell env on a past bake-env.sh run. Container restart loaded
# .env.baked → ANTHROPIC_API_KEY became set in container process env →
# claude -p preferred it over OAuth → 'Invalid API key' on every claude
# invocation → dispatcher cycle blocked 2.5h on 40min-per-FIRE retries.
# Shell-internal noise filter (silent — these vars vary across invocations
# and aren't application config).
SHELL_NOISE_REGEX='^(SHLVL|PWD|OLDPWD|SHELL|TERM|TERM_PROGRAM|TERM_PROGRAM_VERSION|TERM_SESSION_ID|TMUX|TMUX_PANE|TMPDIR|BASH|BASH_VERSION|BASH_VERSINFO|BW_HYDRATE)='

# Dispatcher-toxic var blocklist (loud WARN — operator should know if these
# were silently dropped). Concrete incident 2026-05-22 (scrollinondubs/
# new-jaxity 3h BFL pipeline outage): a stale ANTHROPIC_API_KEY leaked into
# Sean's .env.baked from some transient shell env on a past bake-env.sh run.
# Container restart loaded .env.baked → ANTHROPIC_API_KEY became set in
# container process env → claude -p preferred it over OAuth → 'Invalid API
# key' on every claude invocation → dispatcher cycle blocked 2.5h on
# 40min-per-FIRE retries.
TOXIC_VARS_REGEX='^(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_BASE_URL|ANTHROPIC_BETA|CLAUDE_CODE_OAUTH_TOKEN|CLAUDE_PROJECT_DIR)='

# Compose-managed var blocklist (silent). These are set authoritatively by
# the chassis service's docker-compose.yml `environment:` block at container
# start. If they're ALSO present in .env.baked, the entrypoint's source_env
# step re-loads .env.baked AFTER compose env has been applied, overwriting
# the compose-set values with whatever the bake-time .env had — typically a
# host-context value (e.g. CHASSIS_PG_HOST=127.0.0.1, CHASSIS_PG_DSN=...@127.0.0.1:
# 5432/chassis) that's correct for host scripts reaching postgres via its
# published port but WRONG inside the container where 127.0.0.1 is the
# container's own loopback. Concrete incident 2026-05-27 (scrollinondubs/
# new-jaxity): every dispatcher-fired oura-ingest + BFL-ingest fallback
# returned "Connection refused at 127.0.0.1:5432" despite the postgres
# service being healthy and reachable as `postgres:5432`. chassis#141 fixed
# the compose env block but didn't strip the bake-time leakage; this regex
# closes the loop. Silent because the bake-time customer .env LEGITIMATELY
# defines these for host-side scripts — no WARN needed, the host .env still
# carries them.
COMPOSE_MANAGED_VARS_REGEX='^(CHASSIS_PG_DSN|CHASSIS_PG_HOST|CHASSIS_PG_PORT|CHASSIS_PG_USER|CHASSIS_PG_DB)='

# Surface a WARN line per toxic var that was present in the bake-time env.
# Operators sometimes export these intentionally for a different purpose; the
# WARN makes it visible that bake-env stripped them rather than silently
# losing the var.
TOXIC_PRESENT=$(echo "$NEW_VARS" | grep -E '^[A-Z][A-Z0-9_]+=' | grep -E "$TOXIC_VARS_REGEX" | sed 's/=.*$//' | sort -u || true)
if [[ -n "$TOXIC_PRESENT" ]]; then
    while IFS= read -r toxic_var; do
        echo "WARN: dispatcher-toxic var '$toxic_var' present at bake time; excluded from .env.baked (see chassis/scripts/bake-env.sh TOXIC_VARS_REGEX)" >&2
    done <<< "$TOXIC_PRESENT"
fi

APP_VARS=$(echo "$NEW_VARS" | grep -E '^[A-Z][A-Z0-9_]+=' | grep -vE "$SHELL_NOISE_REGEX" | grep -vE "$TOXIC_VARS_REGEX" | grep -vE "$COMPOSE_MANAGED_VARS_REGEX" || true)

if [[ -z "$APP_VARS" ]]; then
    echo "ERR: source produced no new application env vars — check $SRC_ENV format" >&2
    exit 2
fi

# Host-path overrides. CUSTOMER_HOME / CHASSIS_HOME / REPO are deliberately
# NOT re-appended after stripping - inside the chassis container these vars
# are set authoritatively by compose's `environment:` block to /app/customer
# (the bind-mount destination). If they were in .env.baked, the entrypoint's
# source_env step would re-source after compose env has been applied,
# overwriting the container path with whatever host path the bake recorded.
# The dispatcher would then `cd "$CUSTOMER_HOME"` to a host path that doesn't
# exist inside the container and every gather script would fail with "no
# such file or directory". Concrete incident 2026-06-03 (Ben's fatboy
# cutover): all gather scripts failed-loop on `cd: /home/ozzy/ben-install`
# because that host path doesn't exist in the chassis container. Fix is to
# strip and NOT re-append - compose's environment block already owns the
# container-side values.
#
# MANIFEST + CUSTOMER_CLAUDE_DIR stay overridden because:
#   - MANIFEST: derived from CUSTOMER_HOME, only read by host-side migration
#     scripts (vaultwarden-migration-manifest.json lives on host).
#   - CUSTOMER_CLAUDE_DIR: $HOME/.claude is the user's Claude Code data dir
#     on the HOST; compose interpolates it as a bind-mount source. The
#     container side of that mount is /home/chassis/.claude regardless.
#
# Without this strip-only behavior, moving an install from $CHASSIS_HOME to
# $HOME/work/new-jaxity required a manual rewrite of .env before re-baking,
# and any missed key caused the chassis container to mount the WRONG host
# directory (silent wrong-bind-mount). Confirmed during the 2026-05-24
# Phase 6 cutover attempt - chassis came up healthy but bind-mount source
# was the old path because the baked CUSTOMER_HOME still pointed at
# $CHASSIS_HOME. See <v1-reference-install>#697 + scrollinondubs/
# new-jaxity#49 for context. The strip step still does that work; only the
# re-append for the three container-overridden keys is removed.
PATH_KEYS_REGEX='^(CUSTOMER_HOME|CHASSIS_HOME|REPO|MANIFEST|CUSTOMER_CLAUDE_DIR)='
# `|| true` guards the no-match case: when none of the new vars are host-path
# keys, grep exits 1, and under `set -o pipefail` (see `set -euo pipefail` at
# the top) that non-zero propagates through the pipeline and `set -e` aborts
# the whole bake. This bit whenever bake ran from a shell that had already
# sourced `.env` (so the path keys were pre-env and got subtracted out). Every
# sibling grep in this script (TOXIC_PRESENT, APP_VARS) already carries the
# same guard.
PATH_ORIGINALS=$(echo "$APP_VARS" | grep -E "$PATH_KEYS_REGEX" | sort -u || true)
if [[ -n "$PATH_ORIGINALS" ]]; then
    while IFS= read -r line; do
        key="${line%%=*}"
        echo "INFO: stripping host-path var '$key' from .env.baked (was: ${line#*=})" >&2
    done <<< "$PATH_ORIGINALS"
fi

APP_VARS=$(echo "$APP_VARS" | grep -vE "$PATH_KEYS_REGEX" || true)

# Re-append only the keys that legitimately need to reach the container as
# host paths (MANIFEST is host-side only; CUSTOMER_CLAUDE_DIR is interpolated
# by compose). CUSTOMER_HOME / CHASSIS_HOME / REPO are intentionally absent -
# see the comment block above.
APP_VARS="$APP_VARS
MANIFEST=$CUSTOMER_HOME/data/vaultwarden-migration-manifest.json
CUSTOMER_CLAUDE_DIR=$HOME/.claude"

n_keys=$(echo "$APP_VARS" | grep -c '^[A-Z]' || true)

# Single-quote each value so the resulting file is safe for both:
#   - compose's env_file: directive (compose strips one layer of quotes per
#     the compose-spec env_file rules)
#   - bash `source` / `. .env.baked` (single-quoted strings are literal in
#     bash; no expansion, no command substitution)
#
# Without this, values containing JSON braces, spaces, semicolons, or any
# shell metachar break bash sourcing — e.g. REMARKABLE_TOKEN's
# `{"devicetoken": "eyJ...", "usertoken": ""}` parses as a variable
# assignment plus three subsequent "command not found" errors. Containers
# loop on the entrypoint source step until the file is hand-fixed (chassis
# #134, <v1-reference-install>#700 incident 2026-05-25).
#
# Embedded single quotes are escaped via the standard `'\''` trick.
# Comments and blank lines pass through unchanged.
APP_VARS_QUOTED=$(printf '%s\n' "$APP_VARS" | python3 -c "
import sys
for line in sys.stdin:
    line = line.rstrip('\n')
    if not line or line.lstrip().startswith('#') or '=' not in line:
        print(line)
        continue
    key, _, value = line.partition('=')
    # If value is already single-quoted by the source .env, leave it alone.
    if len(value) >= 2 and value.startswith(\"'\") and value.endswith(\"'\"):
        print(line)
        continue
    safe = value.replace(\"'\", \"'\\\\''\")
    print(f\"{key}='{safe}'\")
")

{
    echo "# .env.baked - literal-only secrets file for the chassis container's env_file directive"
    echo "# Generated by chassis/scripts/bake-env.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# Source: $SRC_ENV"
    echo "# DO NOT EDIT manually - re-run bake-env.sh after credential rotation or .env changes"
    echo "# Mode: 0600 - never commit, never share, gitignored"
    echo "#"
    echo "# Format: KEY='value' with single-quote escaping. Safe for both"
    echo "# compose env_file (strips one quote layer) AND bash \`source\`."
    echo "#"
    echo "# Security posture documented in chassis/docs/hydration.md"
    echo ""
    echo "$APP_VARS_QUOTED"
} > "$TMP_BAKED"

if [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] would write $n_keys keys to $DST_BAKED (mode 0600)"
    echo "[dry-run] preview (first 10 lines + key names only):"
    head -10 "$TMP_BAKED"
    echo "..."
    echo "[dry-run] key names that would be written:"
    grep -oE '^[A-Z][A-Z0-9_]+' "$TMP_BAKED" | sort
    echo "[dry-run] no changes made to $DST_BAKED"
    exit 0
fi

mv "$TMP_BAKED" "$DST_BAKED"
chmod 600 "$DST_BAKED"

echo "Wrote $DST_BAKED ($n_keys keys, mode 0600)"
echo ""
echo "Next - recreate the container via the compose wrapper, never bare docker compose."
echo "It pins --env-file, the project name, CUSTOMER_HOME and the compose-file paths:"
echo ""
echo "  bash $(dirname "${BASH_SOURCE[0]}")/compose.sh up -d --force-recreate chassis"
