#!/bin/bash
# test-validate-heartbeats.sh - Regression test for validate-heartbeats.sh's
# gather-path resolution.
#
# The bug this locks down
# ========================
# `gather:` lines can carry a leading `KEY=VALUE ...` env prefix (the
# documented way to parameterise a shared chassis gather per heartbeat, e.g.
# `gather: PG_CONTAINER=jax-pg BACKUP_SUBDIR=jax-pg chassis/chassis/scripts/pg-backup.sh`).
# The old `gather_path="${value%% *}"` took the first whitespace token, which
# resolved to `PG_CONTAINER=jax-pg` instead of the script path, and every such
# heartbeat permanently failed the missing-file check.
#
# No docker daemon, no network. Everything runs against a scratch HEARTBEATS.md
# + scratch chassis tree under a temp CHASSIS_HOME.
#
# Exit codes:
#   0 - all scenarios passed
#   1 - one or more scenarios failed
#   2 - test harness itself broke

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALIDATOR="${SCRIPT_DIR}/validate-heartbeats.sh"

if [[ ! -f "$VALIDATOR" ]]; then
    echo "test-validate-heartbeats: missing $VALIDATOR" >&2
    exit 2
fi

fail=0
pass=0

TMPHOME="$(mktemp -d)"
trap 'rm -rf "$TMPHOME"' EXIT

mkdir -p "$TMPHOME/chassis/chassis/scripts"
cat > "$TMPHOME/chassis/chassis/scripts/pg-backup.sh" <<'EOF'
#!/usr/bin/env bash
echo ok
EOF
chmod +x "$TMPHOME/chassis/chassis/scripts/pg-backup.sh"

run_hb() {
    local name="$1"
    local gather_line="$2"
    cat > "$TMPHOME/HEARTBEATS.md" <<EOF
## $name
\`\`\`yaml
gather: $gather_line
\`\`\`
EOF
    CHASSIS_HOME="$TMPHOME" bash "$VALIDATOR" "$TMPHOME/HEARTBEATS.md"
}

# ------------------------------------------------------------------------
# Scenario 1: plain path, no prefix, no args. Baseline - must keep passing.
# ------------------------------------------------------------------------
if run_hb "plain" "chassis/chassis/scripts/pg-backup.sh" >/tmp/vh-out.$$ 2>&1; then
    pass=$((pass + 1))
else
    echo "FAIL [plain] expected exit 0"
    cat /tmp/vh-out.$$
    fail=$((fail + 1))
fi

# ------------------------------------------------------------------------
# Scenario 2: single KEY=VALUE env prefix.
# ------------------------------------------------------------------------
if run_hb "one-env-prefix" "PG_CONTAINER=jax-pg chassis/chassis/scripts/pg-backup.sh" >/tmp/vh-out.$$ 2>&1; then
    pass=$((pass + 1))
else
    echo "FAIL [one-env-prefix] expected exit 0 (env-prefixed gather should resolve, not flag as missing)"
    cat /tmp/vh-out.$$
    fail=$((fail + 1))
fi

# ------------------------------------------------------------------------
# Scenario 3: two KEY=VALUE env prefixes (the exact shape from the bug report).
# ------------------------------------------------------------------------
if run_hb "two-env-prefix" "PG_CONTAINER=jax-pg BACKUP_SUBDIR=jax-pg chassis/chassis/scripts/pg-backup.sh" >/tmp/vh-out.$$ 2>&1; then
    pass=$((pass + 1))
else
    echo "FAIL [two-env-prefix] expected exit 0"
    cat /tmp/vh-out.$$
    fail=$((fail + 1))
fi

# ------------------------------------------------------------------------
# Scenario 4: leading `env` token plus KEY=VALUE prefixes.
# ------------------------------------------------------------------------
if run_hb "env-token" "env PG_CONTAINER=jax-pg BACKUP_SUBDIR=jax-pg chassis/chassis/scripts/pg-backup.sh" >/tmp/vh-out.$$ 2>&1; then
    pass=$((pass + 1))
else
    echo "FAIL [env-token] expected exit 0"
    cat /tmp/vh-out.$$
    fail=$((fail + 1))
fi

# ------------------------------------------------------------------------
# Scenario 5: env prefix AND trailing inline args together.
# ------------------------------------------------------------------------
if run_hb "env-prefix-and-args" "PG_CONTAINER=jax-pg chassis/chassis/scripts/pg-backup.sh --json" >/tmp/vh-out.$$ 2>&1; then
    pass=$((pass + 1))
else
    echo "FAIL [env-prefix-and-args] expected exit 0"
    cat /tmp/vh-out.$$
    fail=$((fail + 1))
fi

# ------------------------------------------------------------------------
# Scenario 6: genuinely missing file must still error (no false negatives
# introduced by the env-prefix stripping).
# ------------------------------------------------------------------------
if run_hb "missing-file" "PG_CONTAINER=jax-pg chassis/chassis/scripts/does-not-exist.sh" >/tmp/vh-out.$$ 2>&1; then
    echo "FAIL [missing-file] expected non-zero exit (file does not exist)"
    cat /tmp/vh-out.$$
    fail=$((fail + 1))
else
    if grep -q "does-not-exist.sh" /tmp/vh-out.$$; then
        pass=$((pass + 1))
    else
        echo "FAIL [missing-file] expected error to name does-not-exist.sh, got:"
        cat /tmp/vh-out.$$
        fail=$((fail + 1))
    fi
fi

rm -f /tmp/vh-out.$$

echo
echo "test-validate-heartbeats: $pass passed, $fail failed"
if [[ $fail -gt 0 ]]; then
    exit 1
fi
exit 0
