#!/usr/bin/env bash
# parsers/url-extract.sh — trigger parser that extracts every http(s) URL from
# the message body. Used by Pacman-style triggers where the keyword introduces
# one or more URLs the handler needs to walk.
#
# Input:  message body on stdin
# Output: {"urls": ["https://...", ...], "url_count": N, "raw": "<body>"}
#         If no URLs found: {"urls": [], "url_count": 0, "raw": "<body>"}

set -euo pipefail

body=$(cat)

# grep -oE: print only the matching part. The URL regex is intentionally loose
# — Discord/HTTP URLs vary widely and Pacman-style use cases just need
# something the handler can fetch.
urls=$(printf '%s' "$body" \
    | grep -oE 'https?://[A-Za-z0-9._~:/?#@!$&'"'"'()*+,;=%-]+' \
    | awk '!seen[$0]++' \
    || true)

if [[ -z "$urls" ]]; then
    jq -n --arg raw "$body" '{urls: [], url_count: 0, raw: $raw}'
    exit 0
fi

# Convert newline-separated list to JSON array
url_json=$(printf '%s\n' "$urls" | jq -R . | jq -s .)
url_count=$(printf '%s\n' "$urls" | wc -l | tr -d ' ')

jq -n --arg raw "$body" --argjson urls "$url_json" --argjson count "$url_count" '
    {urls: $urls, url_count: $count, raw: $raw}'
