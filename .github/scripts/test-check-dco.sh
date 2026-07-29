#!/usr/bin/env bash
# test-check-dco.sh — prove the DCO check passes signed commits, fails unsigned
# ones, skips merge commits, and refuses to pass on a range it cannot resolve.
#
# The last case is the one that matters most. `git log BASE..HEAD` on a shallow
# clone returns an EMPTY range rather than an error, so a naive implementation
# reports "all commits signed" for every PR forever. That is the same shape as
# the dead monitors found on 2026-07-29: a check that cannot fail is worse than
# no check, because it manufactures confidence.
#
# Builds throwaway git repos under mktemp. Touches nothing in this repo.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKER="$HERE/check-dco.sh"
pass=0
fail=0

WORK=$(mktemp -d)
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

git_quiet() { git -c user.name=Test -c user.email=test@example.com -c commit.gpgsign=false "$@" >/dev/null 2>&1; }

expect() {
    local want="$1" name="$2" dir="$3" base="$4" head="$5"
    local got
    ( cd "$dir" && BASE="$base" HEAD="$head" bash "$CHECKER" >/dev/null 2>&1 )
    got=$?
    if [[ "$got" == "$want" ]]; then
        echo "  ok    $name"
        pass=$((pass + 1))
    else
        echo "  FAIL  $name (wanted exit $want, got $got)"
        fail=$((fail + 1))
    fi
}

# --- repo 1: signed and unsigned commits on a branch -----------------------
R="$WORK/basic"
mkdir -p "$R" && cd "$R" || exit 1
git_quiet init -b main
echo base > f.txt && git_quiet add . && git_quiet commit -m "base" -s
BASE=$(git rev-parse HEAD)

echo one >> f.txt && git_quiet add . && git_quiet commit -m "signed change" -s
SIGNED=$(git rev-parse HEAD)

echo two >> f.txt && git_quiet add . && git_quiet commit -m "unsigned change"
UNSIGNED=$(git rev-parse HEAD)

echo "Sign-off detection:"
expect 0 "all commits signed"                "$R" "$BASE"   "$SIGNED"
expect 1 "one unsigned commit in range"      "$R" "$BASE"   "$UNSIGNED"
expect 0 "empty range (nothing to verify)"   "$R" "$SIGNED" "$SIGNED"

# --- repo 2: a merge commit must be skipped, not demanded ------------------
M="$WORK/merge"
mkdir -p "$M" && cd "$M" || exit 1
git_quiet init -b main
echo base > f.txt && git_quiet add . && git_quiet commit -m "base" -s
MBASE=$(git rev-parse HEAD)

git_quiet checkout -b feature
echo feat > g.txt && git_quiet add . && git_quiet commit -m "feature work" -s

git_quiet checkout main
echo other > h.txt && git_quiet add . && git_quiet commit -m "main moves" -s

git_quiet checkout feature
# --no-ff forces a merge commit, which git creates WITHOUT a sign-off. GitHub
# does the same when a PR is updated from main. Demanding one would fail every
# such PR.
git_quiet merge --no-ff main -m "Merge main into feature"
MHEAD=$(git rev-parse HEAD)

echo
echo "Merge commits:"
expect 0 "unsigned merge commit is skipped" "$M" "$MBASE" "$MHEAD"

# --- repo 3: unresolvable range must NOT pass ------------------------------
echo
echo "Unresolvable range - must refuse, never silently pass:"
expect 2 "BASE not in this clone"  "$R" "0000000000000000000000000000000000000000" "$SIGNED"
expect 2 "HEAD not in this clone"  "$R" "$BASE" "0000000000000000000000000000000000000000"
expect 2 "BASE/HEAD unset"         "$R" "" ""

cd "$HERE" || exit 1
echo
if [[ "$fail" -gt 0 ]]; then
    echo "FAIL: $fail of $((pass + fail)) checks failed"
    exit 1
fi
echo "OK: $pass/$pass checks passed"
