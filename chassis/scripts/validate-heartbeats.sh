#!/usr/bin/env bash
# validate-heartbeats.sh — Lint HEARTBEATS.md against chassis portability rules.
#
# Walks each `## <name>` block + its yaml fence and verifies:
#
#   - `gather:`, `prompt:`, `cwd:` fields use chassis-relative paths
#     (i.e. don't start with `/` and don't start with `~`). The chassis
#     dispatcher cd's to $CHASSIS_HOME before evaluating gather_cmd, so
#     relative paths resolve correctly under both host and container.
#     Absolute paths (Sean's V1 install had `$CHASSIS_HOME/scripts/...`)
#     are non-portable and break inside the chassis container.
#
#   - Referenced gather scripts have portable shebangs:
#       #!/usr/bin/env <interp>     ✓ portable (resolves via PATH)
#       #!/bin/bash, #!/bin/zsh     ✓ portable (always present)
#       #!/opt/homebrew/...         ✗ macOS-Homebrew only; missing in container
#       #!/usr/local/bin/...        ✗ partial coverage; warn
#
# Usage:
#   validate-heartbeats.sh                                    # uses $CHASSIS_HOME/HEARTBEATS.md
#   validate-heartbeats.sh path/to/HEARTBEATS.md              # explicit file
#   validate-heartbeats.sh --strict                           # warnings become errors
#
# Exit:
#   0  - all checks pass (warnings printed but non-blocking)
#   1  - one or more rule violations (absolute paths, missing files)
#   2  - missing HEARTBEATS.md or bad invocation
#
# Per <v1-reference-install>#604 — add this linter to the chassis bootstrap +
# wire into CI so future installers' HEARTBEATS.md drift gets caught at
# install time instead of at first heartbeat tick.

set -euo pipefail

STRICT=false
HB_FILE=""
for arg in "$@"; do
    case "$arg" in
        --strict) STRICT=true ;;
        --help|-h)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        --*)
            echo "unknown flag: $arg" >&2
            exit 2
            ;;
        *)
            HB_FILE="$arg"
            ;;
    esac
done

CHASSIS_ROOT="${CHASSIS_HOME:-${CHASSIS_HOME:-$(cd "$(dirname "$0")/../.." && pwd)}}"
HB_FILE="${HB_FILE:-$CHASSIS_ROOT/HEARTBEATS.md}"

if [[ ! -f "$HB_FILE" ]]; then
    echo "ERR: HEARTBEATS.md not found at $HB_FILE" >&2
    echo "set CHASSIS_HOME or pass an explicit path" >&2
    exit 2
fi

errors=0
warnings=0

err() {
    printf 'ERR  %s\n' "$*" >&2
    errors=$((errors + 1))
}

warn() {
    printf 'WARN %s\n' "$*" >&2
    warnings=$((warnings + 1))
}

# Walk HEARTBEATS.md, extract per-heartbeat config fields. Skip HTML-commented
# blocks so disabled heartbeats don't lint. Output: tab-separated rows of
#   <heartbeat-name> <field-name> <value>
# for each gather/prompt/cwd field found.
parse_fields() {
    awk '
        in_html_comment { if (/-->/) in_html_comment=0; next }
        /<!--.*-->/ { next }
        /<!--/ { in_html_comment=1; next }
        /^## / { name=$2; in_yaml=0; next }
        /^```yaml/ { if (name != "") { in_yaml=1; next } }
        /^```/ && in_yaml { in_yaml=0; name=""; next }
        in_yaml && /^(gather|prompt|cwd):[[:space:]]/ {
            field=$1; sub(/:$/, "", field)
            sub(/^[a-z]+:[[:space:]]+/, "")
            printf "%s\t%s\t%s\n", name, field, $0
        }
    ' "$HB_FILE"
}

# Per-heartbeat rule check
while IFS=$'\t' read -r hb_name field value; do
    [[ -z "$hb_name" ]] && continue

    # Rule 1: no absolute paths
    if [[ "$value" == /* ]]; then
        err "[$hb_name].$field is absolute: \`$value\`. Use a chassis-relative path so the dispatcher resolves it under both host (\$CHASSIS_HOME) and container (\$CHASSIS_HOME). Example: \`scripts/gather-X.sh\` instead of \`/Users/.../scripts/gather-X.sh\`."
        continue
    fi

    # Rule 2: no ~-prefix paths
    if [[ "$value" == "~"* ]]; then
        err "[$hb_name].$field starts with \`~\`: \`$value\`. Tilde expansion is shell-dependent and breaks inside Python heredocs + some container shells. Use chassis-relative path."
        continue
    fi

    # Rule 3: gather field — referenced file must exist + have portable shebang
    if [[ "$field" == "gather" ]]; then
        # Strip any inline arguments (e.g. `gather: scripts/foo.sh --json`)
        gather_path="${value%% *}"
        full_path="$CHASSIS_ROOT/$gather_path"

        if [[ ! -f "$full_path" ]]; then
            err "[$hb_name].gather references missing file: \`$gather_path\` (resolved to \`$full_path\`). Either the script was deleted/renamed or HEARTBEATS.md has a stale path."
            continue
        fi

        if [[ -x "$full_path" ]]; then
            first_line=$(head -1 "$full_path")
            case "$first_line" in
                "#!/usr/bin/env "*)
                    : # portable
                    ;;
                "#!/bin/bash"|"#!/bin/sh"|"#!/bin/zsh")
                    : # always-present interpreters
                    ;;
                "#!/opt/homebrew/"*)
                    warn "[$hb_name].gather script \`$gather_path\` has Homebrew-only shebang: \`$first_line\`. Use \`#!/usr/bin/env <interp>\` so the container's Debian PATH resolves it."
                    ;;
                "#!/usr/local/bin/"*)
                    warn "[$hb_name].gather script \`$gather_path\` has /usr/local/bin shebang: \`$first_line\`. Not present on all base images. Prefer \`#!/usr/bin/env <interp>\`."
                    ;;
                "#!"*)
                    warn "[$hb_name].gather script \`$gather_path\` has non-canonical shebang: \`$first_line\`. Prefer \`#!/usr/bin/env <interp>\` for cross-platform portability."
                    ;;
                *)
                    warn "[$hb_name].gather script \`$gather_path\` has no shebang. Will run via default shell, which may differ between host and container."
                    ;;
            esac
        fi
    fi

    # Rule 4: prompt field — referenced file must exist (it's read by claude -p)
    if [[ "$field" == "prompt" ]]; then
        full_path="$CHASSIS_ROOT/$value"
        if [[ ! -f "$full_path" ]]; then
            err "[$hb_name].prompt references missing file: \`$value\` (resolved to \`$full_path\`)."
        fi
    fi

    # Rule 5: cwd field — referenced directory must exist
    if [[ "$field" == "cwd" ]]; then
        full_path="$CHASSIS_ROOT/$value"
        if [[ ! -d "$full_path" ]]; then
            err "[$hb_name].cwd references missing directory: \`$value\` (resolved to \`$full_path\`)."
        fi
    fi
done < <(parse_fields)

# Summary
total=$((errors + warnings))
if [[ $total -eq 0 ]]; then
    echo "validate-heartbeats: OK ($HB_FILE)"
    exit 0
fi

echo ""
echo "validate-heartbeats: $errors error(s), $warnings warning(s) in $HB_FILE"

if [[ $errors -gt 0 ]]; then
    exit 1
fi
if [[ "$STRICT" == "true" && $warnings -gt 0 ]]; then
    echo "(--strict: treating warnings as errors)"
    exit 1
fi
exit 0
