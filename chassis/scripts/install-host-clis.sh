#!/bin/bash
# install-host-clis.sh — install per-instance host CLIs.
#
# Reads `host_clis:` from $CHASSIS_HOME/chassis.config.yaml (or accepts a
# `--clis` flag) and installs each requested CLI on the host machine via
# brew (macOS) or apt (Linux). Idempotent: re-running re-checks each CLI
# and skips ones already installed.
#
# Why this exists (Sean's 2026-05-25 directive, <v1-reference-install>#700 follow-up):
# Different installs need different CLIs. Sean's install needs vercel,
# turso, stripe (in addition to gh + awscli which the chassis image bakes).
# Ben's may not. Marc's may use a fourth combo. Baking every possible CLI
# into the image bloats the image and forces every installer to update on
# every CLI version bump. Instead: per-instance install at bootstrap time
# based on what the install profile says the customer needs.
#
# CHASSIS LAYER vs HOST LAYER split:
# - Universal CLIs (used by chassis itself: gh, awscli, postgresql-client,
#   docker, rmapi) → baked into chassis image (Dockerfile).
# - Per-install CLIs (Vercel, Turso, Stripe, etc.) → installed on HOST via
#   this script (NOT in the container — the customer's workflows run on the
#   host: vercel deploy, turso db shell, stripe listen).
#
# Tool → install command mapping documented in CLI_INSTALL_MAP.
#
# Usage:
#   bash chassis/scripts/install-host-clis.sh                    # read from chassis.config.yaml
#   bash chassis/scripts/install-host-clis.sh --clis vercel,turso  # explicit list
#   bash chassis/scripts/install-host-clis.sh --dry-run          # print what would happen
#
# Exit codes:
#   0 — all requested CLIs installed (or already present)
#   1 — at least one CLI failed to install
#   2 — usage / config error

set -euo pipefail

DRY_RUN=false
EXPLICIT_CLIS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --clis) EXPLICIT_CLIS="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

CHASSIS_HOME="${CHASSIS_HOME:-$PWD}"
CONFIG_YAML="$CHASSIS_HOME/chassis.config.yaml"

# Tool → install-command mapping. Each entry: NAME|MAC_CMD|LINUX_CMD|VERIFY_CMD.
# MAC_CMD assumes brew is present (every macOS install does). LINUX_CMD assumes
# apt + sudo (Debian/Ubuntu reference); switch to native package manager for
# other distros at customer-onboarding time. VERIFY_CMD is what we run to
# detect "already installed" (non-zero exit = need install).
CLI_INSTALL_MAP=$(cat <<'EOF'
vercel|brew install vercel-cli|npm install -g vercel|vercel --version
turso|brew install tursodatabase/tap/turso|curl -sSfL https://get.tur.so/install.sh | bash|turso --version
stripe|brew install stripe/stripe-cli/stripe|curl -s https://packages.stripe.dev/api/security/keypair/stripe-cli-gpg/public | gpg --dearmor -o /etc/apt/keyrings/stripe.gpg && echo "deb [signed-by=/etc/apt/keyrings/stripe.gpg] https://packages.stripe.dev/stripe-cli-debian-local stable main" | sudo tee /etc/apt/sources.list.d/stripe.list && sudo apt update && sudo apt install -y stripe|stripe --version
gh|brew install gh|apt install -y gh|gh --version
awscli|brew install awscli|apt install -y awscli|aws --version
heroku|brew tap heroku/brew && brew install heroku|curl https://cli-assets.heroku.com/install.sh | sh|heroku --version
flyctl|brew install flyctl|curl -L https://fly.io/install.sh | sh|fly version
railway|brew install railway|npm install -g @railway/cli|railway --version
EOF
)

# Detect OS for the right install command column.
case "$(uname -s)" in
    Darwin) OS_FIELD=2 ;;
    Linux)  OS_FIELD=3 ;;
    *)      echo "Unsupported OS: $(uname -s)" >&2; exit 2 ;;
esac

# Resolve which CLIs to install: --clis flag wins; else read from yaml.
if [[ -n "$EXPLICIT_CLIS" ]]; then
    REQUESTED=$(echo "$EXPLICIT_CLIS" | tr ',' '\n' | tr -d ' ' | grep -v '^$' | sort -u)
elif [[ -f "$CONFIG_YAML" ]]; then
    # Extract `host_clis:` list from yaml. Tolerates both `[a, b, c]` flow style
    # and `\n  - a\n  - b` block style. Falls through to empty if missing.
    REQUESTED=$(python3 -c "
import sys, re
try:
    import yaml
except ImportError:
    print('ERR: PyYAML not installed on host', file=sys.stderr); sys.exit(2)
with open('$CONFIG_YAML') as f:
    cfg = yaml.safe_load(f) or {}
clis = cfg.get('host_clis') or []
if isinstance(clis, str):
    clis = [c.strip() for c in clis.split(',') if c.strip()]
for c in clis:
    print(c)
" | sort -u)
else
    echo "WARN: $CONFIG_YAML not found, no --clis flag → nothing to install" >&2
    exit 0
fi

if [[ -z "$REQUESTED" ]]; then
    echo "INFO: no host CLIs requested in $CONFIG_YAML — nothing to do"
    exit 0
fi

echo "Requested host CLIs:"
echo "$REQUESTED" | sed 's/^/  - /'
echo

FAILED=()
INSTALLED=()
SKIPPED=()

while IFS= read -r cli; do
    [[ -z "$cli" ]] && continue
    row=$(echo "$CLI_INSTALL_MAP" | grep "^${cli}|" || true)
    if [[ -z "$row" ]]; then
        echo "✗ $cli — no install mapping. Add to CLI_INSTALL_MAP in install-host-clis.sh."
        FAILED+=("$cli (unknown)")
        continue
    fi
    install_cmd=$(echo "$row" | cut -d'|' -f"$OS_FIELD")
    verify_cmd=$(echo "$row" | cut -d'|' -f4)

    # Already installed?
    if bash -c "$verify_cmd" >/dev/null 2>&1; then
        echo "✓ $cli — already installed ($verify_cmd OK)"
        SKIPPED+=("$cli")
        continue
    fi

    echo "→ $cli — installing via: $install_cmd"
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  [dry-run] would run: $install_cmd"
        continue
    fi
    if bash -c "$install_cmd"; then
        # Re-verify post-install.
        if bash -c "$verify_cmd" >/dev/null 2>&1; then
            echo "✓ $cli — installed + verified"
            INSTALLED+=("$cli")
        else
            echo "✗ $cli — install ran but verify failed"
            FAILED+=("$cli (verify failed)")
        fi
    else
        echo "✗ $cli — install command failed"
        FAILED+=("$cli (install failed)")
    fi
done <<< "$REQUESTED"

echo
echo "Summary:"
echo "  installed: ${#INSTALLED[@]} (${INSTALLED[*]:-none})"
echo "  already present: ${#SKIPPED[@]} (${SKIPPED[*]:-none})"
echo "  failed: ${#FAILED[@]} (${FAILED[*]:-none})"

if [[ ${#FAILED[@]} -gt 0 ]]; then
    exit 1
fi
