#!/usr/bin/env bash
# check-dco.sh — every non-merge commit in a PR must carry a Signed-off-by line.
#
# This is a DCO check, not a CLA. Nobody signs a document and nobody assigns
# copyright. `git commit -s` appends one line asserting the contributor wrote
# the change (or has the right to submit it) and is submitting it under this
# project's licence. See CONTRIBUTING.md for the reasoning.
#
# Merge commits are skipped. GitHub generates them and they carry no author
# assertion, so requiring a sign-off on them would fail every PR that has been
# updated from main.
#
# Usage:
#   BASE=<sha> HEAD=<sha> bash .github/scripts/check-dco.sh
# Exit 0 if every commit is signed off, 1 otherwise.

set -uo pipefail

BASE="${BASE:-}"
HEAD="${HEAD:-}"

if [[ -z "$BASE" || -z "$HEAD" ]]; then
    echo "check-dco: BASE and HEAD must be set" >&2
    exit 2
fi

# `git log BASE..HEAD` needs both objects present. A shallow clone silently
# yields an empty range instead of an error, which would pass this check on
# every PR - the exact "a check that cannot fail" shape this repo has been
# bitten by before. Verify the range resolves before trusting an empty result.
if ! git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null; then
    echo "check-dco: BASE $BASE is not present in this clone — fetch depth too shallow" >&2
    exit 2
fi
if ! git rev-parse --verify --quiet "$HEAD^{commit}" >/dev/null; then
    echo "check-dco: HEAD $HEAD is not present in this clone — fetch depth too shallow" >&2
    exit 2
fi

commits=$(git rev-list --no-merges "$BASE..$HEAD")

if [[ -z "$commits" ]]; then
    echo "check-dco: no non-merge commits in range — nothing to verify"
    exit 0
fi

missing=0
checked=0

while IFS= read -r sha; do
    [[ -z "$sha" ]] && continue
    checked=$((checked + 1))
    subject=$(git log -1 --format=%s "$sha")
    if git log -1 --format=%B "$sha" | grep -qiE '^ *Signed-off-by: .+ <[^@]+@[^>]+>'; then
        echo "  ok    ${sha:0:8}  $subject"
    else
        echo "  MISS  ${sha:0:8}  $subject"
        missing=$((missing + 1))
    fi
done <<< "$commits"

echo
if [[ "$missing" -gt 0 ]]; then
    cat <<'EOF'
FAIL: some commits have no Signed-off-by line.

This project uses the Developer Certificate of Origin (https://developercertificate.org/).
It is one line in the commit message. No form to sign, no copyright assignment -
you keep your copyright. It records that you wrote the change, or have the right
to submit it, and are submitting it under this project's licence.

To fix the most recent commit:

    git commit --amend -s --no-edit
    git push --force-with-lease

To fix every commit on your branch:

    git rebase --signoff origin/main
    git push --force-with-lease

To avoid it next time, commit with -s:

    git commit -s -m "your message"

EOF
    echo "$missing of $checked commit(s) unsigned"
    exit 1
fi

echo "OK: all $checked commit(s) signed off"
