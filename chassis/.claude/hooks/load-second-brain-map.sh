#!/bin/bash
# load-second-brain-map.sh - SessionStart hook. Loads the second brain's map doc
# (its table of contents) into every session as additionalContext.
#
# The deterministic half of a pair: CLAUDE.md also tells the session to read
# the map, but a directive is something a session can skip and a hook is not.
#
# Opt-in: prints nothing unless second_brain.map.doc_id is set. Uncached and
# loud when configured but unreachable - see chassis/scripts/second-brain-map.py
# and chassis/docs/second-brain-map.md for why.
#
# Register host-side (chassis/docs/second-brain-map.md has the JSON). Always
# exits 0: a SessionStart hook that fails blocks nothing and says nothing, so a
# crash is turned into the same banner an unreachable backend gets.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="${HERE}/../../scripts/second-brain-map.py"

banner() {
    python3 -c '
import json, sys
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
    "SECOND BRAIN UNREACHABLE. The second-brain map hook could not run: " + sys.argv[1] +
    "\n\nTell the principal in your first message and do not proceed as though you have full context."}}))' "$1"
}

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"SECOND BRAIN UNREACHABLE. The second-brain map hook could not run: python3 is not installed. Tell the principal in your first message."}}'
    exit 0
fi

OUT="$(python3 "$HELPER" load 2>/dev/null)"
RC=$?
if [[ $RC -ne 0 ]]; then
    banner "second-brain-map.py exited ${RC}"
elif [[ -n "$OUT" ]]; then
    printf '%s\n' "$OUT"
fi
exit 0
