#!/bin/bash
# _chassis-changelog.sh - `## Unreleased` extraction + digest for drift detection.
#
# Sourced, never executed. Shared by gather-chassis-update-check.sh (detect) and
# chassis-update.sh (apply) so the two cannot disagree about what counts as
# "upstream main carries code this tree does not have".
#
# Why this exists (behalfbot#147)
# ===============================
# `chassis/VERSION` only moves on an explicit release commit. Everything merged
# in between lands under `## Unreleased` in the changelog with VERSION
# untouched. Both the checker and the applier gated on string equality between
# local and upstream VERSION, so once those matched, every subsequent merge was
# structurally invisible to every install that tracks main - the distribution
# branch. On 2026-08-09 the reference install sat nine commits behind an
# upstream reporting the same version number, including the fix for a bug that
# had been silently destroying its knowledge graph for sixteen days.
#
# The signal, and the direction it is measured in
# -----------------------------------------------
# Drift = the digest of the LOCAL tree's `## Unreleased` section differs from
# the digest of UPSTREAM's. Local versus upstream, not upstream versus a stored
# baseline. That distinction is the whole design:
#
#   - It measures the condition that matters ("this tree differs from what an
#     apply would deliver") rather than a proxy ("upstream moved since we last
#     said something").
#   - It needs no seeding. A stored baseline has a cold start: the first run
#     after this ships either fires on every install or silently records an
#     already-drifted install as level, and that second option is the exact
#     failure being fixed.
#   - It self-clears. After an apply the two digests match with no state write,
#     so there is no way for the "we are level now" record to be missed.
#
# What it cannot see
# ------------------
# A merge that lands code and adds no changelog entry moves no digest and is
# invisible here. That gap is real and this file does not close it. The durable
# complement is requiring a changelog entry per PR, which is a CI change and a
# separate issue. Until then: a merge with no changelog entry is a merge no
# install will be told about.
#
# Digests are always computed on the HOST that is comparing them. The fallback
# chain below picks whatever hash tool is present, and different tools give
# different values, so a digest is only ever meaningful against another digest
# produced by the same process. Never compute one inside a container and
# compare it to one computed outside.

# Print the `## Unreleased` body from a changelog. Argument is a file path, or
# "-" / omitted to read stdin. The heading itself is dropped and capture stops
# at the next `## ` heading, so the result is exactly the block a release cut
# would move under a version heading.
#
# Returns non-zero for a missing file so callers can tell an empty section
# ("nothing unreleased upstream") apart from an unreadable changelog ("cannot
# answer the question").
chassis_changelog_unreleased() {
    local file="${1:--}"
    local prog='
        /^## Unreleased/ { capture = 1; next }
        /^## / && capture { exit }
        capture { print }
    '
    if [[ "$file" == "-" ]]; then
        awk "$prog"
    else
        [[ -f "$file" ]] || return 1
        awk "$prog" "$file"
    fi
}

# Hash stdin down to a short comparable token.
#
# Prints the literal `empty` for whitespace-only input, which is the state of
# the section immediately after a release cut. Callers treat `empty` as "there
# is nothing unreleased upstream", never as a hash value to compare.
#
# sha256sum (chassis container / Linux) then shasum (macOS host) then cksum
# (POSIX, always present). No new dependency: whichever is found, both sides of
# a given comparison run through this same function on the same machine.
chassis_changelog_digest() {
    local text hash
    text="$(cat)"
    if [[ -z "${text//[[:space:]]/}" ]]; then
        printf 'empty'
        return 0
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        hash=$(printf '%s' "$text" | sha256sum)
    elif command -v shasum >/dev/null 2>&1; then
        hash=$(printf '%s' "$text" | shasum -a 256)
    else
        hash=$(printf '%s' "$text" | cksum)
    fi
    printf '%s' "${hash%% *}" | cut -c1-16
}

# Convenience: digest of a changelog's `## Unreleased` section.
# File path, or "-" for stdin. Non-zero when the changelog cannot be read.
chassis_unreleased_digest() {
    local file="${1:--}"
    [[ "$file" == "-" || -f "$file" ]] || return 1
    chassis_changelog_unreleased "$file" | chassis_changelog_digest
}
