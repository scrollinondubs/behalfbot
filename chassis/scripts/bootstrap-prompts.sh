#!/usr/bin/env bash
# bootstrap-prompts.sh - Render install-specific prompt templates.
#
# Walks plugins/**/scheduled-tasks/*.md.template and chassis/scheduled-tasks/
# *.md.template, substitutes `${VAR}` placeholders from chassis.config.yaml
# + .env, writes the rendered `.md` alongside the `.template`. Rendered
# files are gitignored (per-install runtime artifacts).
#
# Substitution sources, in order of precedence (later wins):
#   1. .env / .env.baked at $CHASSIS_HOME
#   2. chassis.config.yaml derived values (principal, assistant, discord, quiet_hours)
#   3. Explicit env vars at invocation time (override anything)
#
# Variables sourced from chassis.config.yaml:
#   PRINCIPAL_NAME           ← identity.principal.full_name
#   PRINCIPAL_FIRST_NAME     ← identity.principal.first_name
#   PRINCIPAL_HOME_CITY      ← identity.principal.home_city
#   PRINCIPAL_HOME_COUNTRY   ← identity.principal.home_country
#   PRINCIPAL_TIMEZONE       ← identity.principal.timezone
#   ASSISTANT_NAME           ← identity.assistant.name
#   ASSISTANT_EMAIL          ← identity.assistant.email
#   ASSISTANT_DISPLAY_NAME   ← derived: "<assistant.name> - <principal.full_name>'s AI assistant"
#   DISCORD_PRIMARY_LABEL    ← discord_channels.primary_label
#   DISCORD_ALERTS_LABEL     ← discord_channels.alerts_label
#   DISCORD_OPS_LABEL        ← discord_channels.ops_label
#   QUIET_HOURS_START        ← quiet_hours.start
#   QUIET_HOURS_END          ← quiet_hours.end
#
# Variables sourced from .env (sensitive, never in chassis.config.yaml):
#   PRINCIPAL_MOBILE         (e.g. "+14802215500")
#   DISCORD_PRIMARY_CHANNEL_ID
#   DISCORD_ALERTS_CHANNEL_ID
#   DISCORD_OPS_CHANNEL_ID
#
# Usage:
#   bash chassis/scripts/bootstrap-prompts.sh
#   bash chassis/scripts/bootstrap-prompts.sh --dry-run    # Preview, no writes
#   bash chassis/scripts/bootstrap-prompts.sh --force      # Overwrite existing rendered .md
#
# Exit codes:
#   0  success (rendered or no-op)
#   1  missing required variable (script will list which); rendering aborted
#   2  invocation error (bad flag, missing chassis.config.yaml)
#
# Idempotent: re-running is a no-op unless --force or a template was edited
# (mtime check). Use --force after rotating a sensitive value in .env.

set -uo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (chassis dispatcher exports it; bootstrap callers must too)}"

DRY_RUN=0
FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --force)   FORCE=1; shift ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

CONFIG_FILE="$CHASSIS_HOME/chassis.config.yaml"
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: chassis.config.yaml not found at $CONFIG_FILE" >&2
    exit 2
fi

# Source .env first (sensitive secrets land in env).
if [[ -f "$CHASSIS_HOME/.env.baked" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$CHASSIS_HOME/.env.baked" 2>/dev/null
    set +a
elif [[ -f "$CHASSIS_HOME/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$CHASSIS_HOME/.env" 2>/dev/null
    set +a
fi

# Parse chassis.config.yaml via a stack-based awk that handles N-level
# nesting. Comments + blank lines skipped. Quoted string values unquoted.
# Returns empty string when path doesn't exist.
yaml_get() {
    local path="$1"
    awk -v path="$path" '
        BEGIN {
            n = split(path, parts, ".")
            # stack_indent[d] = the indent level at depth d
            # stack_key[d]    = the key at depth d
        }
        # Skip pure-comment and blank lines.
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        {
            line = $0
            # Strip trailing comments (best-effort; not safe in strings,
            # but our values are simple).
            sub(/[[:space:]]+#.*$/, "", line)
            # Compute indent (number of leading spaces).
            ind = 0
            while (substr(line, ind+1, 1) == " ") ind++
            # Skip lines without a colon (e.g. list items, doc text).
            if (line !~ /:/) next
            # Pop the stack until top-of-stack is shallower than this line.
            while (stack_depth > 0 && stack_indent[stack_depth] >= ind) {
                stack_depth--
            }
            # Extract key.
            key = line
            sub(/^[[:space:]]+/, "", key)
            sub(/:.*$/, "", key)
            # Extract value.
            val = line
            sub(/^[^:]*:/, "", val)
            sub(/^[[:space:]]+/, "", val)
            sub(/[[:space:]]+$/, "", val)
            sub(/^"/, "", val); sub(/"$/, "", val)
            # Push current key.
            stack_depth++
            stack_indent[stack_depth] = ind
            stack_key[stack_depth] = key
            # Match against requested path.
            if (stack_depth == n) {
                ok = 1
                for (i = 1; i <= n; i++) {
                    if (stack_key[i] != parts[i]) { ok = 0; break }
                }
                if (ok && val != "") {
                    print val
                    exit
                }
            }
        }
    ' "$CONFIG_FILE"
}

# Pull config-driven values.
# Note: `export` is load-bearing here — envsubst runs in a subshell and
# only sees exported variables. .env vars come pre-exported via `set -a`
# above, but the yaml-derived assignments need explicit export.
export PRINCIPAL_NAME="${PRINCIPAL_NAME:-$(yaml_get identity.principal.full_name)}"
export PRINCIPAL_FIRST_NAME="${PRINCIPAL_FIRST_NAME:-$(yaml_get identity.principal.first_name)}"
export PRINCIPAL_HOME_CITY="${PRINCIPAL_HOME_CITY:-$(yaml_get identity.principal.home_city)}"
export PRINCIPAL_HOME_COUNTRY="${PRINCIPAL_HOME_COUNTRY:-$(yaml_get identity.principal.home_country)}"
export PRINCIPAL_TIMEZONE="${PRINCIPAL_TIMEZONE:-$(yaml_get identity.principal.timezone)}"
export ASSISTANT_NAME="${ASSISTANT_NAME:-$(yaml_get identity.assistant.name)}"
export ASSISTANT_EMAIL="${ASSISTANT_EMAIL:-$(yaml_get identity.assistant.email)}"
export DISCORD_PRIMARY_LABEL="${DISCORD_PRIMARY_LABEL:-$(yaml_get discord_channels.primary_label)}"
export DISCORD_ALERTS_LABEL="${DISCORD_ALERTS_LABEL:-$(yaml_get discord_channels.alerts_label)}"
export DISCORD_OPS_LABEL="${DISCORD_OPS_LABEL:-$(yaml_get discord_channels.ops_label)}"
export QUIET_HOURS_START="${QUIET_HOURS_START:-$(yaml_get quiet_hours.start)}"
export QUIET_HOURS_END="${QUIET_HOURS_END:-$(yaml_get quiet_hours.end)}"

# Derive ASSISTANT_DISPLAY_NAME from name + principal.
if [[ -z "${ASSISTANT_DISPLAY_NAME:-}" ]]; then
    APOSTROPHE_S="'s"
    ASSISTANT_DISPLAY_NAME="${ASSISTANT_NAME} - ${PRINCIPAL_NAME}${APOSTROPHE_S} AI assistant"
fi
export ASSISTANT_DISPLAY_NAME

# Required-var check. Emit ALL missing at once so the operator fixes them in
# one pass, not one-at-a-time.
REQUIRED=(
    PRINCIPAL_NAME PRINCIPAL_FIRST_NAME PRINCIPAL_HOME_CITY PRINCIPAL_HOME_COUNTRY
    PRINCIPAL_TIMEZONE ASSISTANT_NAME ASSISTANT_EMAIL ASSISTANT_DISPLAY_NAME
    DISCORD_PRIMARY_LABEL DISCORD_PRIMARY_CHANNEL_ID
    DISCORD_ALERTS_LABEL DISCORD_OPS_LABEL
    QUIET_HOURS_START QUIET_HOURS_END
    PRINCIPAL_MOBILE
)
MISSING=()
for var in "${REQUIRED[@]}"; do
    val="${!var:-}"
    # Treat empty + literal placeholder ("<...>") as missing.
    if [[ -z "$val" || "$val" =~ ^\<.*\>$ ]]; then
        MISSING+=("$var")
    fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "ERROR: missing required values for prompt rendering:" >&2
    for v in "${MISSING[@]}"; do echo "  - $v" >&2; done
    echo "" >&2
    echo "Fill in chassis.config.yaml (identity / discord_channels / quiet_hours)" >&2
    echo "and set sensitive vars (PRINCIPAL_MOBILE, DISCORD_*_CHANNEL_ID) in .env." >&2
    exit 1
fi

# Find all .md.template files. Use a while-read loop instead of mapfile
# so this works on macOS's stock bash 3.2 (mapfile is bash 4+).
TEMPLATES=()
while IFS= read -r line; do
    TEMPLATES+=("$line")
done < <(find "$CHASSIS_HOME" \
    -type f \
    \( -path "*/scheduled-tasks/*.md.template" -o -path "*/scripts/prompts/*.md.template" \) \
    -not -path "*/.git/*" \
    -not -path "*/node_modules/*" \
    -not -path "*/.venv*/*" \
    2>/dev/null | sort)

if [[ ${#TEMPLATES[@]} -eq 0 ]]; then
    echo "no .md.template files found under $CHASSIS_HOME — nothing to render"
    exit 0
fi

RENDERED=0
SKIPPED=0
WROTE_PATHS=()

render_one() {
    local tpl="$1"
    local out="${tpl%.template}"

    if [[ -f "$out" && $FORCE -eq 0 ]]; then
        # Skip if rendered file is newer than template.
        if [[ "$out" -nt "$tpl" ]]; then
            SKIPPED=$((SKIPPED + 1))
            return 0
        fi
    fi

    # Use envsubst to expand exactly the variables we exported, leaving
    # other shell-shaped strings (like ${CHASSIS_HOME} in script examples)
    # alone if they're not in our allowlist. Pass the allowlist explicitly.
    local allowlist='${PRINCIPAL_NAME} ${PRINCIPAL_FIRST_NAME} ${PRINCIPAL_HOME_CITY} ${PRINCIPAL_HOME_COUNTRY} ${PRINCIPAL_TIMEZONE} ${PRINCIPAL_MOBILE} ${ASSISTANT_NAME} ${ASSISTANT_EMAIL} ${ASSISTANT_DISPLAY_NAME} ${DISCORD_PRIMARY_LABEL} ${DISCORD_PRIMARY_CHANNEL_ID} ${DISCORD_ALERTS_LABEL} ${DISCORD_ALERTS_CHANNEL_ID} ${DISCORD_OPS_LABEL} ${DISCORD_OPS_CHANNEL_ID} ${QUIET_HOURS_START} ${QUIET_HOURS_END}'

    if [[ $DRY_RUN -eq 1 ]]; then
        echo "would render: $tpl -> $out"
    else
        if envsubst "$allowlist" < "$tpl" > "${out}.tmp"; then
            mv "${out}.tmp" "$out"
            RENDERED=$((RENDERED + 1))
            WROTE_PATHS+=("$out")
        else
            rm -f "${out}.tmp"
            echo "ERROR: envsubst failed for $tpl" >&2
            return 1
        fi
    fi
}

for tpl in "${TEMPLATES[@]}"; do
    render_one "$tpl"
done

if [[ $DRY_RUN -eq 1 ]]; then
    echo ""
    echo "Dry-run complete: would render ${#TEMPLATES[@]} templates."
else
    echo ""
    echo "Rendered: $RENDERED   Skipped (up to date): $SKIPPED"
    if [[ ${#WROTE_PATHS[@]} -gt 0 && $RENDERED -le 5 ]]; then
        echo "Wrote:"
        for p in "${WROTE_PATHS[@]}"; do echo "  $p"; done
    fi
fi
