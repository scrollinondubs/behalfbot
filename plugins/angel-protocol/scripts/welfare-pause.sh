#!/bin/zsh
# Pause/resume welfare checks for planned offline periods.
# Usage: welfare-pause.sh until YYYY-MM-DD [--reason "camping"]
#        welfare-pause.sh resume
#        welfare-pause.sh status

set -euo pipefail

CONTACTS_FILE="${CHASSIS_HOME:-${CHASSIS_HOME:-$CHASSIS_HOME}}/data/emergency-contacts.json"

[[ $# -lt 1 ]] && { echo "Usage: $0 until YYYY-MM-DD | resume | status"; exit 1; }

CMD="$1"
shift

case "$CMD" in
    until)
        [[ $# -lt 1 ]] && { echo "Usage: $0 until YYYY-MM-DD [--reason \"...\"]"; exit 1; }
        DATE="$1"; shift
        REASON=""
        [[ $# -ge 2 && "$1" == "--reason" ]] && { REASON="$2"; shift 2; }

        tmp="${CONTACTS_FILE}.tmp"
        jq --arg d "${DATE}T23:59:59Z" --arg r "$REASON" \
            '.pause_until = $d | ._pause_note = ("Paused: " + $r)' \
            "$CONTACTS_FILE" > "$tmp" && mv "$tmp" "$CONTACTS_FILE"

        echo "Welfare checks paused until $DATE."
        [[ -n "$REASON" ]] && echo "  Reason: $REASON"
        ;;

    resume)
        tmp="${CONTACTS_FILE}.tmp"
        jq '.pause_until = null | ._pause_note = "Set pause_until to an ISO date string to temporarily disable welfare checks (e.g. during travel)"' \
            "$CONTACTS_FILE" > "$tmp" && mv "$tmp" "$CONTACTS_FILE"
        echo "Welfare checks resumed."
        ;;

    status)
        pause=$(jq -r '.pause_until // "null"' "$CONTACTS_FILE")
        if [[ "$pause" != "null" ]]; then
            note=$(jq -r '._pause_note // ""' "$CONTACTS_FILE")
            echo "Welfare checks PAUSED until $pause"
            [[ -n "$note" ]] && echo "  Note: $note"
        else
            echo "Welfare checks ACTIVE"
        fi
        ;;

    *)
        echo "Usage: $0 until YYYY-MM-DD | resume | status"
        exit 1
        ;;
esac
