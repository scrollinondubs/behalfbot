#!/usr/bin/env bash
# test-patch-discord-plugin.sh - behavioural tests for patch-discord-plugin.sh.
#
# Runs against a synthetic plugin cache and a synthetic patch so it needs no
# network, no bun and no real plugin. Covers: apply to a pristine copy,
# re-run is a no-op, unknown sha is skipped untouched, a known sha whose patch
# does not apply is left untouched, several version dirs, missing cache dir,
# and that the script always exits 0. A final block checks the shipped patch
# file itself for the properties the script relies on.

# Assertions are eval'd strings, so variables in them are expanded late on
# purpose and look unused to shellcheck.
# shellcheck disable=SC2016,SC2034
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="$SCRIPT_DIR/patch-discord-plugin.sh"
SHIPPED_PATCH="$SCRIPT_DIR/../patches/discord-forwarded-messages.patch"

pass=0
fail=0
ok()   { pass=$((pass + 1)); echo "  ok   $*"; }
bad()  { fail=$((fail + 1)); echo "  FAIL $*"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

sha() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    else shasum -a 256 "$1" | awk '{print $1}'; fi
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-patch-discord.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

MARKER="behalfbot-patch: test-marker"

cat > "$WORK/pristine.ts" <<'EOF'
import { thing } from 'x'

function a() {
  return 1
}

function b() {
  return 2
}
EOF
mkdir -p "$WORK/gen/a" "$WORK/gen/b"
cp "$WORK/pristine.ts" "$WORK/gen/a/server.ts"
cp "$WORK/pristine.ts" "$WORK/gen/b/server.ts"
cat > "$WORK/gen/b/server.ts" <<EOF
import { thing } from 'x'

function a() {
  return 1
}

// $MARKER
function b() {
  return 2
}
EOF
(cd "$WORK/gen" && diff -u a/server.ts b/server.ts) > "$WORK/fixture.patch"
PRISTINE_SHA="$(sha "$WORK/pristine.ts")"

run() {
    # $1 = cache dir; remaining env passed through by caller
    DISCORD_PLUGIN_CACHE_DIR="$1" \
    DISCORD_FORWARD_PATCH="${PATCH_OVERRIDE:-$WORK/fixture.patch}" \
    DISCORD_FORWARD_PRISTINE="${PRISTINE_OVERRIDE:-$PRISTINE_SHA}" \
    DISCORD_FORWARD_MARKER="$MARKER" \
        bash "$SUT"
}

echo "== apply to a pristine copy"
C1="$WORK/c1"; mkdir -p "$C1/0.0.4"
cp "$WORK/pristine.ts" "$C1/0.0.4/server.ts"
chmod 640 "$C1/0.0.4/server.ts"
OUT="$(run "$C1")"; RC=$?
check "exit 0" '[ "$RC" -eq 0 ]'
check "marker present after apply" 'grep -qF "$MARKER" "$C1/0.0.4/server.ts"'
check "result equals the expected patched file" 'cmp -s "$C1/0.0.4/server.ts" "$WORK/gen/b/server.ts"'
check ".orig backup written" '[ -f "$C1/0.0.4/server.ts.orig" ]'
check ".orig is the pristine file" '[ "$(sha "$C1/0.0.4/server.ts.orig")" = "$PRISTINE_SHA" ]'
check "log says patched" 'printf "%s" "$OUT" | grep -q "patched: "'
check "file mode preserved" '[ "$(ls -l "$C1/0.0.4/server.ts" | cut -c1-10)" = "-rw-r-----" ]'
check "no staging or reject files left" '[ "$(ls -A "$C1/0.0.4" | sort | tr "\n" " ")" = "server.ts server.ts.orig " ]'
check "log carries no file content" '! printf "%s" "$OUT" | grep -q "function"'

echo "== re-run is a no-op"
BEFORE="$(sha "$C1/0.0.4/server.ts")"; ORIG_BEFORE="$(sha "$C1/0.0.4/server.ts.orig")"
OUT="$(run "$C1")"; RC=$?
check "exit 0" '[ "$RC" -eq 0 ]'
check "server.ts unchanged" '[ "$(sha "$C1/0.0.4/server.ts")" = "$BEFORE" ]'
check ".orig unchanged" '[ "$(sha "$C1/0.0.4/server.ts.orig")" = "$ORIG_BEFORE" ]'
check "log says already patched" 'printf "%s" "$OUT" | grep -q "already patched"'
check "marker appears exactly once" '[ "$(grep -cF "$MARKER" "$C1/0.0.4/server.ts")" -eq 1 ]'

echo "== unknown sha is skipped"
C2="$WORK/c2"; mkdir -p "$C2/0.0.5"
printf 'hand edited\n' >> "$WORK/pristine.ts.edit"
cat "$WORK/pristine.ts" "$WORK/pristine.ts.edit" > "$C2/0.0.5/server.ts"
UNK_BEFORE="$(sha "$C2/0.0.5/server.ts")"
OUT="$(run "$C2")"; RC=$?
check "exit 0" '[ "$RC" -eq 0 ]'
check "file untouched" '[ "$(sha "$C2/0.0.5/server.ts")" = "$UNK_BEFORE" ]'
check "no .orig written" '[ ! -e "$C2/0.0.5/server.ts.orig" ]'
check "WARN logged with the unknown sha" 'printf "%s" "$OUT" | grep -q "WARN: skipping .*$UNK_BEFORE"'

echo "== unknown sha that already reads messageSnapshots points at dropping the patch"
C3="$WORK/c3"; mkdir -p "$C3/0.0.6"
printf 'const s = msg.messageSnapshots\n' > "$C3/0.0.6/server.ts"
OUT="$(run "$C3")"; RC=$?
check "exit 0" '[ "$RC" -eq 0 ]'
check "WARN mentions upstream" 'printf "%s" "$OUT" | grep -q "upstream may handle forwards"'

echo "== known sha but patch does not apply: left untouched"
C4="$WORK/c4"; mkdir -p "$C4/0.0.4"
printf 'totally different\n' > "$C4/0.0.4/server.ts"
OTHER_SHA="$(sha "$C4/0.0.4/server.ts")"
OUT="$(PRISTINE_OVERRIDE="$OTHER_SHA" run "$C4")"; RC=$?
check "exit 0" '[ "$RC" -eq 0 ]'
check "file untouched" '[ "$(sha "$C4/0.0.4/server.ts")" = "$OTHER_SHA" ]'
check "no .orig written" '[ ! -e "$C4/0.0.4/server.ts.orig" ]'
check "WARN says did not apply" 'printf "%s" "$OUT" | grep -q "did not apply cleanly"'
check "no reject or staging files left" '[ "$(ls -A "$C4/0.0.4")" = "server.ts" ]'

echo "== several version dirs: known one patched, unknown one skipped"
C5="$WORK/c5"; mkdir -p "$C5/0.0.4" "$C5/0.0.9"
cp "$WORK/pristine.ts" "$C5/0.0.4/server.ts"
printf 'future\n' > "$C5/0.0.9/server.ts"
FUT="$(sha "$C5/0.0.9/server.ts")"
OUT="$(run "$C5")"; RC=$?
check "exit 0" '[ "$RC" -eq 0 ]'
check "0.0.4 patched" 'grep -qF "$MARKER" "$C5/0.0.4/server.ts"'
check "0.0.9 untouched" '[ "$(sha "$C5/0.0.9/server.ts")" = "$FUT" ]'

echo "== missing cache dir and missing patch file"
OUT="$(run "$WORK/does-not-exist")"; RC=$?
check "missing cache: exit 0" '[ "$RC" -eq 0 ]'
check "missing cache: logged" 'printf "%s" "$OUT" | grep -q "no Discord plugin cache"'
OUT="$(PATCH_OVERRIDE="$WORK/nope.patch" run "$C2")"; RC=$?
check "missing patch: exit 0" '[ "$RC" -eq 0 ]'
check "missing patch: WARN" 'printf "%s" "$OUT" | grep -q "patch file missing"'

echo "== shipped patch file"
check "exists" '[ -f "$SHIPPED_PATCH" ]'
check "neutral a/ b/ headers" '[ "$(sed -n 1p "$SHIPPED_PATCH")" = "--- a/server.ts" ] && [ "$(sed -n 2p "$SHIPPED_PATCH")" = "+++ b/server.ts" ]'
check "adds the default marker" 'grep -q "^+// behalfbot-patch: discord-forwarded-messages$" "$SHIPPED_PATCH"'
check "adds the forwarded meta flag" 'grep -q "^+.*forwarded: .true." "$SHIPPED_PATCH"'
check "download_attachment reads snapshot attachments" 'grep -q "^+        for (const att of allAtts) {" "$SHIPPED_PATCH"'
check "added lines carry no em dash" '! grep "^+" "$SHIPPED_PATCH" | grep -q "$(printf "\342\200\224")"'
check "added lines do not log" '! grep "^+" "$SHIPPED_PATCH" | grep -qE "stderr|console\."'
check "no host paths leaked" '! grep -qE "/home/|/root/|/Users/" "$SHIPPED_PATCH"'

echo
echo "test-patch-discord-plugin: ${pass} passed, ${fail} failed"
[ "$fail" -eq 0 ]
