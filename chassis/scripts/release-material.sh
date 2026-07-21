#!/usr/bin/env bash
# release-material.sh - gather the raw material for a chassis release's notes.
#
# Pure gather. No LLM, no judgment. It emits structured JSON describing
# everything that changed in one release, and a prompt turns that into human
# release notes (see scheduled-tasks/release-notes-prompt.md).
#
# Why the split: release notes need judgment - "what did this actually give
# people" is not derivable from commit subjects. But the inputs to that
# judgment are entirely mechanical, and doing them in shell means the model
# never has to guess a commit range or miss a merged PR.
#
# Release boundaries without tags
# -------------------------------
# The chassis has no git tags. The auto-updater reads chassis/VERSION off main
# via raw.githubusercontent, so tags were never required for delivery. Rather
# than invent a tagging scheme retroactively and hope it matches history, this
# script derives boundaries from the VERSION file itself:
#
#   the commit that set VERSION to X.Y.Z             = the release commit
#   the commit that set VERSION to the one before it = the previous boundary
#
# That is the same source of truth the updater uses, so a release's range is
# exactly "what an install receives when it moves between those two versions".
# It also works retroactively for every version already shipped.
#
# Usage
# -----
#   release-material.sh 0.2.0        JSON for one release
#   release-material.sh --list       every version, and whether it has a
#                                    GitHub release yet
#   release-material.sh --unreleased versions with no GitHub release
#
# Exit codes: 0 ok, 1 usage/lookup error, 2 unknown version.

set -euo pipefail

REPO_SLUG="${RELEASE_REPO:-scrollinondubs/behalfbot}"
VERSION_FILE="chassis/VERSION"
CHANGELOG_FILE="chassis/CHANGELOG.md"

die() { printf 'release-material: %s\n' "$1" >&2; exit "${2:-1}"; }

command -v git >/dev/null || die "git not found"
command -v jq  >/dev/null || die "jq not found"

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git repo"
cd "$REPO_ROOT"
[[ -f "$VERSION_FILE" ]] || die "no $VERSION_FILE here - run from the chassis repo"

# Emits "<sha> <version>" oldest-first for every point on the release branch
# where the VERSION file's contents CHANGED.
#
# Walks --first-parent, which is the correction that matters. Walking all
# commits finds the commit that edited VERSION *on its feature branch*, which
# can sit mid-branch - so every PR merged between that branch commit and its
# own merge commit falls on the wrong side of the boundary. Observed on 0.1.0:
# the bump lived inside the auto-updater feature branch, so PR #34 - the PR
# that shipped 0.1.0 - was attributed to 0.1.1.
#
# --first-parent asks a different question: when did main start carrying this
# version. That is the question an operator's updater answers too, since it
# reads VERSION off main. Boundaries then land on merge commits and a PR is
# attributed to the release that actually delivered it.
#
# A commit that touches the file without changing the string (whitespace, a
# revert landing on the same value) is skipped - otherwise a no-op commit would
# split one release into two, one of them empty.
version_history() {
    local sha prev="" cur
    while read -r sha; do
        cur=$(git show "${sha}:${VERSION_FILE}" 2>/dev/null | tr -d '[:space:]') || continue
        [[ -n "$cur" ]] || continue
        if [[ "$cur" != "$prev" ]]; then
            printf '%s %s\n' "$sha" "$cur"
            prev="$cur"
        fi
    done < <(git log --first-parent --reverse --format='%H' -- "$VERSION_FILE")
}

# A missing or unauthenticated gh must not silently become "everything is
# released" or "nothing is". It reports unknown instead.
existing_releases() {
    command -v gh >/dev/null || { printf '__GH_UNAVAILABLE__\n'; return 0; }
    gh release list --repo "$REPO_SLUG" --limit 200 --json tagName \
        --jq '.[].tagName' 2>/dev/null || printf '__GH_UNAVAILABLE__\n'
}

cmd_list() {
    local releases gh_ok=1 state sha ver
    releases=$(existing_releases)
    [[ "$releases" == "__GH_UNAVAILABLE__" ]] && { gh_ok=0; releases=""; }

    printf '%-10s %-10s %-10s %s\n' VERSION RELEASED SHA SUBJECT
    while read -r sha ver; do
        if [[ $gh_ok -eq 0 ]]; then
            state="unknown"
        elif grep -qx "v${ver}" <<<"$releases"; then
            state="yes"
        else
            state="NO"
        fi
        printf '%-10s %-10s %-10s %s\n' "$ver" "$state" "${sha:0:8}" \
            "$(git log -1 --format='%s' "$sha")"
    done < <(version_history)

    [[ $gh_ok -eq 0 ]] && \
        printf '\nNOTE: gh unavailable or unauthenticated - the RELEASED column is not trustworthy.\n' >&2
    return 0
}

cmd_unreleased() {
    local releases sha ver
    releases=$(existing_releases)
    [[ "$releases" == "__GH_UNAVAILABLE__" ]] && \
        die "gh unavailable - cannot determine which versions lack a release"
    while read -r sha ver; do
        grep -qx "v${ver}" <<<"$releases" || printf '%s\n' "$ver"
    done < <(version_history)
}

# Conventional-commit type and scope are parsed when present and left null when
# not. Guessing a type from prose produces confident nonsense downstream.
commit_json() {
    local sha="$1" subject body type scope
    subject=$(git log -1 --format='%s' "$sha")
    body=$(git log -1 --format='%b' "$sha")

    if [[ "$subject" =~ ^([a-z]+)(\(([^\)]+)\))?!?: ]]; then
        type="${BASH_REMATCH[1]}"
        scope="${BASH_REMATCH[3]:-}"
    else
        type=""
        scope=""
    fi

    jq -n \
        --arg sha "$sha" \
        --arg short "${sha:0:8}" \
        --arg subject "$subject" \
        --arg body "$body" \
        --arg type "$type" \
        --arg scope "$scope" \
        --arg date "$(git log -1 --format='%ad' --date=short "$sha")" \
        --arg author "$(git log -1 --format='%an' "$sha")" \
        --argjson files "$(git show --name-only --format='' "$sha" | sed '/^$/d' | jq -R . | jq -s .)" \
        '{sha: $sha, short: $short, subject: $subject, body: $body,
          type: (if $type == "" then null else $type end),
          scope: (if $scope == "" then null else $scope end),
          date: $date, author: $author, files: $files}'
}

pr_number() {
    [[ "$1" =~ Merge\ pull\ request\ #([0-9]+) ]] && printf '%s' "${BASH_REMATCH[1]}"
    return 0
}

pr_json() {
    local num="$1"
    command -v gh >/dev/null || { printf 'null'; return 0; }
    gh pr view "$num" --repo "$REPO_SLUG" \
        --json number,title,body,labels,mergedAt \
        --jq '{number, title, body, labels: [.labels[].name], mergedAt}' 2>/dev/null || printf 'null'
}

# Which surfaces a release touched. This is the difference between "we changed
# something" and "installs receive something", which is the fact release notes
# most often get wrong.
surface_of() {
    case "$1" in
        chassis/skills/*)       printf '%s\n' 'skills' ;;
        chassis/scripts/*)      printf '%s\n' 'scripts' ;;
        chassis/db/*)           printf '%s\n' 'database' ;;
        chassis/second_brain/*) printf '%s\n' 'second-brain' ;;
        chassis/*)              printf '%s\n' 'chassis' ;;
        plugins/*)              printf '%s\n' 'plugins' ;;
        docker/*|Dockerfile|docker-compose.yml) printf '%s\n' 'runtime' ;;
        docs/*)                 printf '%s\n' 'docs' ;;
        .github/*)              printf '%s\n' 'ci' ;;
        *)                      printf '%s\n' 'other' ;;
    esac
}

cmd_material() {
    local want="$1"
    local prev_sha="" rel_sha="" prev_ver="" sha ver

    while read -r sha ver; do
        if [[ "$ver" == "$want" ]]; then rel_sha="$sha"; break; fi
        prev_sha="$sha"; prev_ver="$ver"
    done < <(version_history)

    [[ -n "$rel_sha" ]] || die "version $want never appears in $VERSION_FILE history" 2

    # The first release in history has no predecessor, so its range is the
    # whole ancestry rather than a two-dot range.
    local range
    if [[ -n "$prev_sha" ]]; then range="${prev_sha}..${rel_sha}"; else range="$rel_sha"; fi

    local commits_json="[]" prs_json="[]" seen_prs="" num pj
    local c_objs=() p_objs=()
    while read -r sha; do
        [[ -n "$sha" ]] || continue
        c_objs+=("$(commit_json "$sha")")
        num=$(pr_number "$(git log -1 --format='%s' "$sha")")
        if [[ -n "$num" ]] && ! grep -qw "$num" <<<"$seen_prs"; then
            seen_prs="$seen_prs $num"
            pj=$(pr_json "$num")
            [[ "$pj" != "null" ]] && p_objs+=("$pj")
        fi
    done < <(git log --format='%H' "$range")

    [[ ${#c_objs[@]} -gt 0 ]] && commits_json=$(printf '%s\n' "${c_objs[@]}" | jq -s .)
    [[ ${#p_objs[@]} -gt 0 ]] && prs_json=$(printf '%s\n' "${p_objs[@]}" | jq -s .)

    local surfaces_json
    surfaces_json=$(git diff --name-only "$range" 2>/dev/null | while read -r f; do
        [[ -n "$f" ]] && surface_of "$f"
    done | sort -u | jq -R . | jq -s .)

    # The CHANGELOG section the maintainer already wrote, when there is one. It
    # is input, not output: the notes reconcile against it, and must say so
    # when the code and the CHANGELOG disagree.
    local changelog_section
    changelog_section=$(awk -v v="## v${want}" '
        $0 == v {found=1; print; next}
        found && /^## v/ {exit}
        found {print}
    ' "$CHANGELOG_FILE" 2>/dev/null || true)

    local breaking="false"
    grep -q 'BREAKING CHANGES' <<<"$changelog_section" && breaking="true"

    jq -n \
        --arg version "$want" \
        --arg previous "${prev_ver:-}" \
        --arg release_sha "$rel_sha" \
        --arg range "$range" \
        --arg date "$(git log -1 --format='%ad' --date=short "$rel_sha")" \
        --arg changelog "$changelog_section" \
        --argjson breaking "$breaking" \
        --argjson commits "$commits_json" \
        --argjson prs "$prs_json" \
        --argjson surfaces "$surfaces_json" \
        '{version: $version,
          previous_version: (if $previous == "" then null else $previous end),
          release_sha: $release_sha,
          range: $range,
          date: $date,
          breaking: $breaking,
          commit_count: ($commits | length),
          pr_count: ($prs | length),
          surfaces: $surfaces,
          changelog_section: $changelog,
          commits: $commits,
          pull_requests: $prs}'
}

main() {
    case "${1:-}" in
        ""|-h|--help) sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --list)       cmd_list ;;
        --unreleased) cmd_unreleased ;;
        -*)           die "unknown flag: $1" ;;
        *)            cmd_material "$1" ;;
    esac
}

main "$@"
