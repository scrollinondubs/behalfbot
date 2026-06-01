#!/usr/bin/env bash
# file-or-comment-issue.sh — dedup-aware GitHub issue helper.
#
# Drop-in replacement for `gh issue create` in heartbeat alert prompts.
# Searches the target repo for open issues matching a dedupe key. If found,
# comments on the existing issue with the new context. If not, files a fresh
# issue with the supplied title/body/labels.
#
# Why this exists (<v1-reference-install> cutover regression 2026-05-22):
# ====================================================
# Pre-cutover, host-side alert prompts manually grep'd the open issue queue
# before filing — one open issue per failure mode, comments on recurrence.
# Post-cutover, container-side prompts forgot the dedupe step; a single
# Strava token failure spawned 53 duplicate GitHub issues in 14 hours
# (<v1-reference-install> #630-#686, closed as duplicates 2026-05-22). Real signal got buried
# in noise. This helper centralizes the dedupe pattern so every alert
# prompt does it right by default.
#
# Usage:
#   file-or-comment-issue.sh \
#     --repo scrollinondubs/new-jaxity \
#     --dedupe-key "strava-ingest: token refresh" \
#     --title "strava-ingest: token refresh failed — $(date -u +%Y-%m-%dT%H:%MZ)" \
#     --body-file /tmp/strava-alert-body.md \
#     --labels "<v1-reference-install>,strava,bug"
#
# Alternative: pass --body inline instead of --body-file.
#
# Dedupe-key semantics:
# - Searches OPEN issues in the repo for the dedupe-key as a substring of
#   the issue title (case-sensitive, since GitHub search is case-sensitive
#   on the `in:title` qualifier).
# - If 1+ open issues match: comments on the FIRST (most recent by default
#   gh ordering) and exits 0 with the existing issue URL.
# - If 0 matches: creates a new issue with the supplied --title / --body /
#   --labels and exits 0 with the new issue URL.
#
# stdout (single line):
#   {"action": "commented" | "created", "url": "...", "number": N, "issue_title": "..."}
#
# Exit codes:
#   0  on success (either commented or created)
#   1  on operational failure (gh CLI errors, missing args, etc.)
#
# Cross-references:
# - scrollinondubs/new-jaxity#4 — the dedup-pattern issue this fixes
# - scrollinondubs/new-jaxity#3 — Strava root cause that surfaced the regression
# - <v1-reference-install> #630-#686 — 53 duplicate alerts that prompted this helper

set -euo pipefail

REPO=""
DEDUPE_KEY=""
TITLE=""
BODY=""
BODY_FILE=""
LABELS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)          REPO="$2"; shift 2;;
        --dedupe-key)    DEDUPE_KEY="$2"; shift 2;;
        --title)         TITLE="$2"; shift 2;;
        --body)          BODY="$2"; shift 2;;
        --body-file)     BODY_FILE="$2"; shift 2;;
        --labels)        LABELS="$2"; shift 2;;
        *) echo "ERR: unknown arg $1" >&2; exit 1;;
    esac
done

if [[ -z "$REPO" || -z "$DEDUPE_KEY" || -z "$TITLE" ]]; then
    echo "ERR: --repo, --dedupe-key, and --title are required" >&2
    exit 1
fi

if [[ -n "$BODY_FILE" && -f "$BODY_FILE" ]]; then
    BODY="$(cat "$BODY_FILE")"
fi

if [[ -z "$BODY" ]]; then
    echo "ERR: --body or --body-file must be supplied" >&2
    exit 1
fi

# Search open issues for matching title substring. gh's GraphQL search uses
# the standard GitHub search syntax — `in:title <key>` restricts to title
# matches. `repo:OWNER/NAME` scopes to the repo. `is:open` excludes closed.
#
# Quoting the dedupe key forces an exact-substring match within the title
# (not tokenized search). Important: keys with spaces still match because
# we quote them.
search_query="repo:${REPO} is:open is:issue in:title \"${DEDUPE_KEY}\""
match_json=$(gh issue list --repo "$REPO" --state open \
    --search "in:title \"${DEDUPE_KEY}\"" \
    --json number,title,url \
    --limit 5 2>/dev/null || echo '[]')

match_count=$(echo "$match_json" | jq 'length')

if [[ "$match_count" -gt 0 ]]; then
    # Comment on the FIRST match (gh's default sort is newest-first, so this
    # is the most recently created open issue with the matching title).
    existing_num=$(echo "$match_json" | jq -r '.[0].number')
    existing_url=$(echo "$match_json" | jq -r '.[0].url')
    existing_title=$(echo "$match_json" | jq -r '.[0].title')

    # The comment body is the supplied --body, prefixed with a recurrence
    # marker line so a glance at the issue thread shows the cadence.
    comment_body="**Recurrence at $(date -u +%Y-%m-%dT%H:%M:%SZ):**

${BODY}"
    gh issue comment "$existing_num" --repo "$REPO" --body "$comment_body" >/dev/null

    jq -n --arg url "$existing_url" --arg title "$existing_title" --argjson num "$existing_num" \
        '{action: "commented", url: $url, number: $num, issue_title: $title}'
    exit 0
fi

# No existing open issue: file a new one.
create_args=(--repo "$REPO" --title "$TITLE" --body "$BODY")
if [[ -n "$LABELS" ]]; then
    create_args+=(--label "$LABELS")
fi

new_url=$(gh issue create "${create_args[@]}" 2>&1 | tail -1)
new_num=$(echo "$new_url" | sed -E 's|.*/issues/([0-9]+).*|\1|')

jq -n --arg url "$new_url" --arg title "$TITLE" --argjson num "$new_num" \
    '{action: "created", url: $url, number: $num, issue_title: $title}'
