#!/usr/bin/env bash
# parsers/passthrough.sh — trigger parser that passes the entire message body
# through as a single JSON field. Default parser when a trigger doesn't declare
# a more specific one.
#
# Input:  message body on stdin
# Output: {"raw": "<body>"} on stdout
#
# Use when the handler wants to do its own parsing of the natural-language
# input and the dispatcher just needs to confirm the keyword matched.

set -euo pipefail

body=$(cat)
jq -n --arg raw "$body" '{raw: $raw}'
