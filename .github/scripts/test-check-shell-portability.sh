#!/usr/bin/env bash
# .github/scripts/test-check-shell-portability.sh
# ==============================================
# Self-test for check-shell-portability.sh.
#
# Every other job in this repo carries behavioural coverage, and a lint without
# it rots in one of two directions: it stops firing on the bug it was written
# for, or it starts firing on the correct dual-dialect idiom until someone
# deletes it as noise. Both halves are asserted here.
#
# The positive fixtures are the three bugs of 2026-09-05 verbatim plus the
# other bash-4 constructs. The negative fixtures are the FIXED spellings that
# already live in the tree - GNU-first stat chains, two-line date fallbacks,
# `declare -a`, a `command -v` guard - because a check that flags those is
# worse than no check.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="$HERE/check-shell-portability.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0
fail=0

ok() { printf '  ok   %s\n' "$1"; pass=$(( pass + 1 )); }
no() { printf '  FAIL %s\n' "$1" >&2; fail=$(( fail + 1 )); }

assert_fires() {
    local title="$1" desc="$2"
    if printf '%s\n' "$OUT" | grep -q "title=$title"; then
        ok "$desc"
    else
        no "$desc (expected a '$title' finding, got none)"
    fi
}

assert_silent() {
    local title="$1" desc="$2"
    if printf '%s\n' "$OUT" | grep -q "title=$title"; then
        no "$desc (unexpected '$title' finding)"
        printf '%s\n' "$OUT" | grep "title=$title" | sed 's/^/       /' >&2
    else
        ok "$desc"
    fi
}

# ---------------------------------------------------------------- positives
mkdir -p "$TMP/bad"
cat > "$TMP/bad/bugs.sh" <<'EOF'
#!/usr/bin/env bash
# Bug 1, behalfbot#197: the compose-failure path of resolve-plugin-root.sh.
BAKED_ROOTS=(/a /b)
echo "${BAKED_ROOTS[-1]:-}"

# Bug 2, behalfbot#193: BSD stat first, GNU second.
mtime=$(stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK" 2>/dev/null || echo 0)

# Bug 3, behalfbot#195: BSD date with no GNU fallback.
lift_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$auto_lift" +%s 2>/dev/null || echo 0)
EOF

cat > "$TMP/bad/bash4.sh" <<'EOF'
#!/bin/bash
declare -A SEEN=()
mapfile -t LINES < <(ls -1A "$dir")
UPPER="${name^^}"
lower="${name,,}"
declare -n ref=target
shopt -s globstar
echo hi &>> /tmp/log
quoted="${name@Q}"
EOF

cat > "$TMP/bad/gnuisms.sh" <<'EOF'
#!/bin/bash
stamp=$(date -d "$iso" +%s)
sum=$(sha256sum "$f" | cut -d' ' -f1)
grep -P '\d+' "$f"
base64 -w 0 < "$f"
find . -name '*.sh' -printf '%p\n'
ls | xargs -r rm
sed -i 's/a/b/' "$f"
EOF

OUT="$(bash "$CHECK" "$TMP/bad" 2>&1)"

assert_fires negative-array-subscript "bug 1: \${ARR[-1]} fires"
assert_fires stat-wrong-order         "bug 2: BSD stat ahead of the GNU fallback fires"
assert_fires date-bsd-only            "bug 3: BSD date with no GNU fallback fires"
assert_fires associative-array        "declare -A fires"
assert_fires mapfile                  "mapfile fires"
assert_fires case-modification        "\${var^^} / \${var,,} fires"
assert_fires nameref                  "declare -n fires"
assert_fires globstar                 "shopt -s globstar fires"
assert_fires append-both-streams      "&>> fires"
assert_fires parameter-transform      "\${var@Q} fires"
assert_fires date-gnu-only            "GNU date -d with no BSD fallback fires"
assert_fires gnu-only-tool            "unguarded sha256sum fires"
assert_fires grep-P                   "grep -P fires"
assert_fires base64-w                 "base64 -w fires"
assert_fires find-printf              "find -printf fires"
assert_fires xargs-r                  "xargs -r fires"
assert_fires sed-i                    "sed -i fires"

# ---------------------------------------------------------------- negatives
mkdir -p "$TMP/good"
cat > "$TMP/good/fixed.sh" <<'EOF'
#!/usr/bin/env bash
# The shapes that already live in the tree after #193 / #195 / #197.

# Spelled out rather than ${BAKED_ROOTS[-1]}, which needs bash 4.3.
LAST="${BAKED_ROOTS[$(( ${#BAKED_ROOTS[@]} - 1 ))]}"

# GNU first, BSD second. GNU `stat -f` writes to stdout even when it fails,
# so this is the only safe order.
mtime="$(stat -c %Y "$LOCK" 2>/dev/null)" || mtime=""
if [ -z "$mtime" ]; then
    mtime="$(stat -f %m "$LOCK" 2>/dev/null)" || mtime=""
fi

# Both date dialects, either order, within the window.
last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S" "$last" +%s 2>/dev/null || \
             date -d "$last" +%s 2>/dev/null || echo 0)

# One-liner form of the same thing.
cutoff=$(date -v-"${DAYS}"d +%Y-%m-%d 2>/dev/null || date -d "-${DAYS} days" +%Y-%m-%d)

# Indexed arrays are bash 3.2.
declare -a QUEUE=()
local -r FROZEN=1

# Guarded GNU coreutils.
if command -v sha256sum >/dev/null 2>&1; then
    sig=$(printf '%s' "$text" | sha256sum)
fi
if command -v timeout >/dev/null 2>&1; then
    timeout 5 true
fi

# Portable in-place edit.
sed -i.bak 's/a/b/' "$f" && rm -f "$f.bak"
EOF

cat > "$TMP/good/not-shell.sh" <<'EOF'
#!/usr/bin/env bash
# A comment mentioning ${BAKED_ROOTS[-1]} must not fire, and neither must
# python subscripting inside an inline program.
python3 - <<'PY'
newest = items[-1]["message_id"]
PY
EOF

cat > "$TMP/good/escaped.sh" <<'EOF'
#!/usr/bin/env bash
mtime=$(stat -f %m "$f")  # portable-ok: macOS-only colima wrapper, never runs in the container
old=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$x" +%s 2>/dev/null || echo 0)  # portable-ok: deliberate pre-fix idiom, asserted to misparse
EOF

OUT="$(bash "$CHECK" "$TMP/good" 2>&1)"

assert_silent negative-array-subscript "the arithmetic-index rewrite does not fire"
assert_silent stat-wrong-order         "GNU-first stat across two lines does not fire"
assert_silent stat-bsd-only            "a windowed GNU stat counts as the fallback"
assert_silent date-bsd-only            "a two-line GNU date fallback does not fire"
assert_silent date-gnu-only            "a one-line BSD-then-GNU date chain does not fire"
assert_silent associative-array        "declare -a does not fire"
assert_silent gnu-only-tool            "a command -v guarded sha256sum does not fire"
assert_silent sed-i                    "sed -i.bak does not fire"

if printf '%s\n' "$OUT" | grep -q '^::warning'; then
    no "the fixed-idiom fixtures produce ZERO findings"
    printf '%s\n' "$OUT" | grep '^::warning' | sed 's/^/       /' >&2
else
    ok "the fixed-idiom fixtures produce ZERO findings"
fi

# ---------------------------------------------------------------- exit codes
assert_rc() {
    local want="$1" desc="$2"; shift 2
    local got=0
    "$@" >/dev/null 2>&1 || got=$?
    if [ "$got" -eq "$want" ]; then
        ok "$desc"
    else
        no "$desc (exit $got, wanted $want)"
    fi
}

assert_rc 0 "advisory by default: findings still exit 0"  bash "$CHECK" "$TMP/bad"
assert_rc 1 "--strict exits 1 on findings"                bash "$CHECK" --strict "$TMP/bad"
assert_rc 0 "--strict exits 0 on a clean tree"            bash "$CHECK" --strict "$TMP/good"

# The check must not silently scan nothing. A lint that walks an empty file
# list and reports zero findings is the failure class this whole lane exists
# for, so assert it actually visited the fixtures.
OUT="$(bash "$CHECK" "$TMP/good" 2>&1)"
if printf '%s\n' "$OUT" | grep -qE 'across 3 shell file'; then
    ok "reports the number of files it actually scanned"
else
    no "reports the number of files it actually scanned"
    printf '%s\n' "$OUT" | tail -3 | sed 's/^/       /' >&2
fi

echo
echo "test-check-shell-portability: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
