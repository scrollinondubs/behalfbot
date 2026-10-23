#!/usr/bin/env bash
# test-swipe-host-control-flags.sh - dating-swipe-host.sh honours halt and
# conservation mode.
#
# The wrapper runs from host launchd, outside the chassis dispatcher, so the
# dispatcher's criticality gate never sees it. Before this suite a swipe slot
# fired straight through conservation mode and through a halt.
#
# Self-contained: a temp CHASSIS_HOME with a gather that always reports a
# ready emulator, and stubs for `claude` (records that it ran) and `sleep`
# (skips the jitter, and can flip a flag mid-sleep). No emulator, no network.
#
# Run: bash plugins/dating/tests/test-swipe-host-control-flags.sh

# shellcheck disable=SC2015  # pass/fail always succeed, so A && B || C is safe here

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_SH="$(cd "$SCRIPT_DIR/.." && pwd)/scripts/dating-swipe-host.sh"

if ! command -v jq >/dev/null 2>&1; then
    echo "test-swipe-host-control-flags: jq is required" >&2
    exit 2
fi

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ON_FLAG='{"enabled": true, "enabled_at": "2026-01-01T00:00:00Z", "enabled_by": "test", "auto_lift_after": null, "reason": "test"}'
EXPIRED_FLAG='{"enabled": true, "enabled_at": "2026-01-01T00:00:00Z", "enabled_by": "test", "auto_lift_after": "2000-01-01T00:00:00Z", "reason": "test"}'

# setup <name> - a fresh install root; prints its path.
setup() {
    local home="$TMP/$1"
    mkdir -p "$home/scripts" "$home/scheduled-tasks" "$home/dating-context" "$home/bin"
    printf '#!/bin/bash\necho %s\n' "'{\"count\": 1}'" > "$home/scripts/gather-dating-swipe.sh"
    echo "swipe" > "$home/scheduled-tasks/dating-swipe-prompt.md"
    printf '#!/bin/bash\ntouch "%s/claude-ran"\n' "$home" > "$home/bin/claude"
    # The jitter sleep. FLIP_DURING_SLEEP=<file> writes ON_FLAG there, the way
    # an operator saying "conserve" mid-jitter would.
    cat > "$home/bin/sleep" <<STUB
#!/bin/bash
if [[ -n "\${FLIP_DURING_SLEEP:-}" ]]; then
    printf '%s\n' '$ON_FLAG' > "\$FLIP_DURING_SLEEP"
fi
STUB
    chmod +x "$home/scripts/gather-dating-swipe.sh" "$home/bin/claude" "$home/bin/sleep"
    printf '%s\n' "$home"
}

run_host() {
    local home="$1"
    shift
    env -u CUSTOMER_HOME CHASSIS_HOME="$home" PATH="$home/bin:$PATH" "$@" \
        bash "$HOST_SH" 1 >/dev/null 2>&1
}

log_of() { cat "$1"/logs/scheduled/*-dating-swipe-1.log 2>/dev/null; }

echo "dating-swipe-host.sh control flags"

home=$(setup none)
run_host "$home"
[[ -f "$home/claude-ran" ]] && pass "no flags: session runs" || fail "no flags: session did not run"

home=$(setup conservation)
printf '%s\n' "$ON_FLAG" > "$home/scheduled-tasks/conservation-mode.json"
run_host "$home"
[[ ! -f "$home/claude-ran" ]] && pass "conservation on: claude not invoked" || fail "conservation on: claude was invoked"
log_of "$home" | grep -q "skip: conservation mode is on (before jitter)" \
    && pass "conservation on: skip logged before jitter" || fail "conservation on: no skip line in log"

home=$(setup halt)
printf '%s\n' "$ON_FLAG" > "$home/scheduled-tasks/halt.json"
run_host "$home"
[[ ! -f "$home/claude-ran" ]] && pass "halt on: claude not invoked" || fail "halt on: claude was invoked"
log_of "$home" | grep -q "skip: halt is on" && pass "halt on: skip logged" || fail "halt on: no skip line in log"

home=$(setup off)
printf '%s\n' '{"enabled": false, "enabled_at": null, "enabled_by": null, "auto_lift_after": null, "reason": null}' \
    > "$home/scheduled-tasks/conservation-mode.json"
run_host "$home"
[[ -f "$home/claude-ran" ]] && pass "conservation off document: session runs" || fail "conservation off document: session did not run"

home=$(setup expired)
printf '%s\n' "$EXPIRED_FLAG" > "$home/scheduled-tasks/conservation-mode.json"
run_host "$home"
[[ -f "$home/claude-ran" ]] && pass "conservation past auto-lift: session runs" || fail "conservation past auto-lift: session did not run"
[[ "$(jq -r '.enabled' "$home/scheduled-tasks/conservation-mode.json")" == "false" ]] \
    && pass "conservation past auto-lift: flag lifted" || fail "conservation past auto-lift: flag still on"

home=$(setup flip)
run_host "$home" FLIP_DURING_SLEEP="$home/scheduled-tasks/conservation-mode.json"
[[ ! -f "$home/claude-ran" ]] && pass "conservation set during jitter: claude not invoked" || fail "conservation set during jitter: claude was invoked"
log_of "$home" | grep -q "(after jitter)" && pass "conservation set during jitter: caught after the sleep" || fail "conservation set during jitter: no after-jitter skip"

home=$(setup garbled)
echo "not json" > "$home/scheduled-tasks/conservation-mode.json"
run_host "$home"
[[ ! -f "$home/claude-ran" ]] && pass "unreadable flag file: treated as on" || fail "unreadable flag file: session ran"

# Without _control-flags.sh beside it (a copy of the wrapper on its own), the
# jq fallback still has to stop the session.
home=$(setup nolib)
mkdir -p "$TMP/nolib-copy/plugins/dating/scripts"
cp "$HOST_SH" "$TMP/nolib-copy/plugins/dating/scripts/"
printf '%s\n' "$ON_FLAG" > "$home/scheduled-tasks/conservation-mode.json"
env -u CUSTOMER_HOME CHASSIS_HOME="$home" PATH="$home/bin:$PATH" \
    bash "$TMP/nolib-copy/plugins/dating/scripts/dating-swipe-host.sh" 1 >/dev/null 2>&1
[[ ! -f "$home/claude-ran" ]] && pass "no flags library: jq fallback still skips" || fail "no flags library: session ran"

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
