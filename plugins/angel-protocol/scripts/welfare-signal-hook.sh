#!/bin/bash
# welfare-signal-hook.sh
# UserPromptSubmit hook: updates welfare-last-seen.txt whenever
# a Discord message from the principal (PRINCIPAL_DISCORD_USERNAME) is in
# the prompt. Runs on every prompt submission - must be fast (<1s).
#
# Required env (set in $CHASSIS_HOME/.env):
#   PRINCIPAL_DISCORD_USERNAME   - the principal's Discord username, e.g. "alice123"

LAST_SEEN_FILE="${CHASSIS_HOME:-/app/customer}/data/welfare-last-seen.txt"
INPUT=$(cat)

if [[ -z "${PRINCIPAL_DISCORD_USERNAME:-}" ]]; then
    exit 0
fi

# Check if this prompt contains a Discord message from the principal.
# Match on `user="<username>"` to scope to the harness-emitted channel tag.
if echo "$INPUT" | grep -q "user=\"${PRINCIPAL_DISCORD_USERNAME}\"" 2>/dev/null; then
    date +%s > "$LAST_SEEN_FILE"
fi

# Always allow - this is an observation hook, not a gate
exit 0
