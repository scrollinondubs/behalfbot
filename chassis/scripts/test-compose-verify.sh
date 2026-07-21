#!/bin/bash
# test-compose-verify.sh - exercise _compose-verify.sh against a REAL scratch
# compose project (behalfbot#100).
#
# The bug this guards against was a check that could not observe the thing
# that broke, so this test refuses to be a mock: it stands up actual
# containers, once WITHOUT the override (the pre-fix updater's failure mode)
# and once WITH it, and asserts the verifier fails the first and passes the
# second.
#
# Scenario mirrors the real install that motivated #100:
#   base:     service "chassis" (no published port), service "extra" (publishes
#             a port, runs by default - the spurious Vaultwarden analogue)
#   override: chassis publishes 127.0.0.1:$PORT->8080, extra scaled to 0
#
# Requires docker + docker compose + jq; SKIPs (exit 0) when unavailable so CI
# without a docker engine stays green. Uses a throwaway project name and a
# dynamically picked localhost port; never touches a live install's stack.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=chassis/scripts/_compose-verify.sh
source "${SCRIPT_DIR}/_compose-verify.sh"

PASS=0
FAIL=0
say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  PASS: $*"; }
bad()  { FAIL=$((FAIL+1)); say "  FAIL: $*"; }

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    say "SKIP: docker compose and/or jq not available"
    exit 0
fi
if ! docker info >/dev/null 2>&1; then
    say "SKIP: docker daemon not reachable"
    exit 0
fi

WORK="$(mktemp -d)"
PROJECT="bbtest-verify-$$"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])' 2>/dev/null || echo 15987)"

cleanup() {
    docker compose -p "$PROJECT" -f "$WORK/base.yml" -f "$WORK/override.yml" down -v --remove-orphans >/dev/null 2>&1
    rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$WORK/base.yml" <<EOF
name: $PROJECT
services:
  chassis:
    image: alpine:3.20
    command: ["sleep", "600"]
  extra:
    image: alpine:3.20
    command: ["sleep", "600"]
    ports:
      - "127.0.0.1:0:9090"
EOF

cat > "$WORK/override.yml" <<EOF
services:
  chassis:
    ports:
      - "127.0.0.1:${PORT}:8080"
  extra:
    deploy:
      replicas: 0
EOF

MERGED_JSON="$WORK/merged.json"
docker compose -p "$PROJECT" -f "$WORK/base.yml" -f "$WORK/override.yml" config --format json > "$MERGED_JSON" \
    || { say "FATAL: could not render merged config"; exit 1; }

say "== unit: expected-ports extraction from the merged config =="
expected=$(compose_expected_ports "$MERGED_JSON")
[[ "$expected" == "chassis|8080|${PORT}|127.0.0.1|tcp" ]] \
    && ok "expected ports = 'chassis|8080|${PORT}|127.0.0.1|tcp'" \
    || bad "expected-ports extraction got: '$expected'"
zeros=$(compose_zero_replica_services "$MERGED_JSON")
[[ "$zeros" == "extra" ]] \
    && ok "zero-replica services = 'extra'" \
    || bad "zero-replica extraction got: '$zeros'"

say "== case 1: stack brought up BARE (override dropped - the #100 failure) =="
docker compose -p "$PROJECT" -f "$WORK/base.yml" up -d >/dev/null 2>&1 \
    || { say "FATAL: bare up failed"; exit 1; }

if out=$(compose_verify_running_config "$MERGED_JSON"); then
    bad "verifier PASSED a stack running without the override"
else
    ok "verifier failed the bare stack"
fi
grep -q "service 'chassis' should publish 127.0.0.1:${PORT}->8080/tcp" <<<"${out:-}" \
    && ok "unpublished override port detected" \
    || bad "missing port failure not reported; got: ${out:-<nothing>}"
grep -q "service 'extra' is scaled to 0 .* but is running" <<<"${out:-}" \
    && ok "running scaled-to-0 service detected" \
    || bad "zero-replica violation not reported; got: ${out:-<nothing>}"

compose_override_in_config_files "$PROJECT" "$WORK/override.yml" "chassis"
rc=$?
[[ $rc -eq 1 ]] \
    && ok "config_files label check returns 1 (override absent from the stack)" \
    || bad "config_files label check returned $rc, expected 1"

say "== case 2: stack brought up WITH the override (the fixed invocation) =="
docker compose -p "$PROJECT" -f "$WORK/base.yml" -f "$WORK/override.yml" up -d >/dev/null 2>&1 \
    || { say "FATAL: merged up failed"; exit 1; }

if out=$(compose_verify_running_config "$MERGED_JSON"); then
    ok "verifier passed the merged stack"
else
    bad "verifier failed a correct stack: $out"
fi
extra_running=$(compose_service_containers "$PROJECT" "extra")
[[ -z "$extra_running" ]] \
    && ok "compose up with the override scaled 'extra' down to 0" \
    || bad "'extra' still running after merged up: $extra_running"

compose_override_in_config_files "$PROJECT" "$WORK/override.yml" "chassis"
rc=$?
[[ $rc -eq 0 ]] \
    && ok "config_files label check returns 0 (override recorded on the container)" \
    || bad "config_files label check returned $rc, expected 0"

say ""
say "RESULT: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
