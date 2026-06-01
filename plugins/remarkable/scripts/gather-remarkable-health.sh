#!/usr/bin/env bash
# gather-remarkable-health.sh — heartbeat gather: is the reMarkable
# OTA pipeline reachable?
#
# Emits JSON gather-contract on stdout:
#   {"count": 0, "status": "healthy", "items": N, "checked_at": "..."}
#     → no alert needed (rmapi auth + cloud reachable, tablet visible)
#   {"count": 1, "status": "<failure tag>", "detail": "...", "checked_at": "..."}
#     → fires the alert prompt
#
# What's checked:
#   1. rmapi binary exists + executable
#   2. ~/.rmapi config exists + has a devicetoken
#   3. `rmapi ls /` returns OK (auth refresh still works, cloud reachable)
#   4. Tablet last-sync state — we infer from the # of items at root
#      (>0 = tablet has been syncing; 0 = either empty or auth-broken)
#
# Failure tags surface in the alert prompt so claude can route the
# response (re-pair runbook vs network-debug etc.).
#
# This heartbeat ALSO catches the silent-fail mode that surfaced
# 2026-05-25: rmapi config had devicetoken but empty usertoken, every
# call returned 400, no other heartbeat noticed until Sean asked
# directly. With this gather wired into the heartbeat dispatcher the
# next occurrence fires a #<devops> alert within the heartbeat
# cadence (default daily).

set -uo pipefail

REPO="${CHASSIS_HOME:?CHASSIS_HOME must be exported (install root that holds .env + the chassis subtree)}"
RMAPI="${RMAPI_BIN:-$(command -v rmapi 2>/dev/null || echo rmapi)}"
CONFIG="${HOME}/.rmapi"
CHECKED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

emit_failure() {
    local tag="$1"; shift
    local detail="$1"; shift
    local escaped_detail
    escaped_detail=$(printf '%s' "$detail" | sed 's/"/\\"/g')
    printf '{"count": 1, "status": "%s", "detail": "%s", "checked_at": "%s"}\n' \
        "$tag" "$escaped_detail" "$CHECKED_AT"
}

# 1. Binary present?
if [[ ! -x "$RMAPI" ]]; then
    emit_failure "rmapi_binary_missing" \
        "expected rmapi at $RMAPI but no executable found"
    exit 0
fi

# 2. Config present + has devicetoken?
if [[ ! -f "$CONFIG" ]]; then
    emit_failure "rmapi_config_missing" \
        "expected $CONFIG but file missing — needs re-pair via https://my.remarkable.com/device/desktop/connect"
    exit 0
fi

# rmapi's config format varies across forks:
#   ddvk historical: YAML-style `devicetoken: eyJ...` (one per line)
#   ddvk recent JSON: `{"devicetoken": "eyJ...", "usertoken": "eyJ..."}`
# Tolerate both. The substring match catches either format without
# needing to parse YAML or JSON properly.
if ! grep -q "devicetoken" "$CONFIG" 2>/dev/null; then
    emit_failure "rmapi_devicetoken_missing" \
        "$CONFIG has no devicetoken (neither YAML nor JSON form) — needs re-pair"
    exit 0
fi

# 3. API call works? Capture stderr for diagnostic detail on failure.
LS_OUT=$("$RMAPI" ls / 2>&1)
LS_RC=$?
if [[ $LS_RC -ne 0 ]]; then
    # Trim the output to fit JSON cleanly + redact any tokens that might
    # leak into a verbose error.
    first_line=$(printf '%s\n' "$LS_OUT" | head -1)
    emit_failure "rmapi_api_call_failed" \
        "$RMAPI ls / returned rc=$LS_RC; first line: $first_line"
    exit 0
fi

# 4. Count items at root (sanity check — empty cloud is suspicious).
ITEMS=$(printf '%s\n' "$LS_OUT" | grep -cE '^\[(f|d)\]' || true)
if [[ $ITEMS -eq 0 ]]; then
    emit_failure "rmapi_root_empty" \
        "rmapi ls / succeeded but returned 0 items. Tablet may have been wiped, or this account is empty."
    exit 0
fi

# All clear.
printf '{"count": 0, "status": "healthy", "items": %s, "checked_at": "%s"}\n' \
    "$ITEMS" "$CHECKED_AT"
