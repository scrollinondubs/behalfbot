#!/usr/bin/env bash
# welfare-check-gather.sh — Layer 1: collect full signal panel for welfare reasoning.
#
# Produces a rich JSON object covering every proof-of-life signal ${ASSISTANT_NAME} can
# observe: Discord activity, Mac HID idle, iCloud location, iVideon motion,
# Oura sleep + HR, today's/yesterday's calendar events, quiet-hours flag, and
# the computed hours_since_anything summary.
#
# The dispatcher evaluates:
#   threshold hours_since_anything > 18  AND  is_quiet_hours == false
#
# If the threshold fires, the dispatcher invokes the Layer 2 reasoning prompt
# (scheduled-tasks/welfare-check-prompt.md) with this JSON as input.
#
# Dry-run mode: WELFARE_DRY_RUN=true in env -> gather still runs fully so the
# signal panel is produced, but the dispatcher knows not to send anything.

set -euo pipefail

# PATH inherited from caller (host dispatcher exports Homebrew prefix; container PATH is set by entrypoint)

CHASSIS_DIR="${CHASSIS_HOME:-${CHASSIS_HOME:-$CHASSIS_HOME}}"
VAULT_DIR="$HOME/.angel-vault"
LAST_SEEN_FILE="$CHASSIS_DIR/data/welfare-last-seen.txt"
EMERGENCY_CONTACTS_FILE="$CHASSIS_DIR/data/emergency-contacts.json"
LOCATION_PINGS="$VAULT_DIR/location-pings.jsonl"
HOME_COORDS_FILE="$VAULT_DIR/home-coords.json"

now=$(date -u +%s)

# ── Pause / disabled guard ──────────────────────────────────────────────────

if [ -f "$EMERGENCY_CONTACTS_FILE" ]; then
    pause_until=$(jq -r '.pause_until // ""' "$EMERGENCY_CONTACTS_FILE" 2>/dev/null)
    if [ -n "$pause_until" ] && [ "$pause_until" != "null" ]; then
        pause_epoch=$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$pause_until" +%s 2>/dev/null || echo 0)
        if [ "$now" -lt "$pause_epoch" ]; then
            echo '{"hours_since_anything": 0, "count": 0, "reason": "paused_until_'"$pause_until"'"}'
            exit 0
        fi
    fi
    enabled=$(jq -r '.enabled // false' "$EMERGENCY_CONTACTS_FILE" 2>/dev/null)
    if [ "$enabled" != "true" ]; then
        echo '{"hours_since_anything": 0, "count": 0, "reason": "disabled"}'
        exit 0
    fi
fi

# ── Lisbon time + quiet-hours flag ──────────────────────────────────────────
# Quiet hours: 22:00-09:00 Europe/Lisbon, timezone-aware.

lisbon_hour=$(TZ="Europe/Lisbon" date +%H | sed 's/^0//')
is_quiet_hours="false"
if [ "$lisbon_hour" -ge 22 ] || [ "$lisbon_hour" -lt 9 ]; then
    is_quiet_hours="true"
fi

current_local_time=$(TZ="Europe/Lisbon" date +"%Y-%m-%dT%H:%M:%S")
now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# ── Signal 1: stored last-seen baseline ─────────────────────────────────────

last_seen=0
if [ -f "$LAST_SEEN_FILE" ]; then
    file_ts=$(cat "$LAST_SEEN_FILE" 2>/dev/null || echo "0")
    if [[ "$file_ts" =~ ^[0-9]+$ ]] && [ "$file_ts" -gt 0 ]; then
        last_seen="$file_ts"
    fi
fi

last_discord_message="null"

# ── Signal 2: Discord activity across all bot-accessible channels ────────────

PRINCIPAL_DISCORD_USER_ID="${PRINCIPAL_DISCORD_USER_ID:-}"
ACCESS_FILE="$HOME/.claude/channels/discord/access.json"
DISCORD_BOT_TOKEN="${DISCORD_BOT_TOKEN:-}"
if [ -z "$DISCORD_BOT_TOKEN" ] && [ -f "$CHASSIS_DIR/.env" ]; then
    DISCORD_BOT_TOKEN=$(grep '^DISCORD_BOT_TOKEN=' "$CHASSIS_DIR/.env" 2>/dev/null | cut -d= -f2- || true)
fi

discord_latest_epoch=0
if [ -n "$DISCORD_BOT_TOKEN" ] && [ -f "$ACCESS_FILE" ]; then
    for channel_id in $(jq -r '.groups | keys[]' "$ACCESS_FILE" 2>/dev/null); do
        latest=$(curl -sS --max-time 5 \
            -H "Authorization: Bot $DISCORD_BOT_TOKEN" \
            "https://discord.com/api/v10/channels/${channel_id}/messages?limit=50" \
            2>/dev/null | jq -r --arg uid "$PRINCIPAL_DISCORD_USER_ID" '
                [.[] | select(.author.id == $uid) | .timestamp]
                | max // empty
            ' 2>/dev/null || true)
        if [ -n "$latest" ]; then
            epoch=$(date -j -u -f "%Y-%m-%dT%H:%M:%S" "${latest%.*}" +%s 2>/dev/null || echo 0)
            if [ "$epoch" -gt "$discord_latest_epoch" ] 2>/dev/null; then
                discord_latest_epoch="$epoch"
                last_discord_message="${latest%.*}Z"
            fi
        fi
    done
fi
if [ "$discord_latest_epoch" -gt "$last_seen" ] 2>/dev/null; then
    last_seen="$discord_latest_epoch"
fi

# ── Signal 3: Mac HID idle time ─────────────────────────────────────────────

mac_idle_sec=$(ioreg -c IOHIDSystem 2>/dev/null | awk '/HIDIdleTime/ {printf "%.0f", $NF/1000000000; exit}' 2>/dev/null || echo "")
mac_hid_active="null"
if [ -n "$mac_idle_sec" ] && [ "$mac_idle_sec" -ge 0 ] 2>/dev/null; then
    mac_active_epoch=$((now - mac_idle_sec))
    if [ "$mac_active_epoch" -gt "$last_seen" ] 2>/dev/null; then
        last_seen="$mac_active_epoch"
    fi
    mac_hid_active=$(date -u -r "$mac_active_epoch" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "null")
fi

# ── Signal 4: iCloud location (latest non-null ping from angel-vault) ────────
# Read the freshest location ping from location-pings.jsonl.
# The poller now emits location_stale + location_age_min fields (per #581).

icloud_location="null"
icloud_is_home="false"
if [ -f "$LOCATION_PINGS" ]; then
    # Get the last non-null, non-auth-failed ping
    latest_loc=$(tail -200 "$LOCATION_PINGS" | python3 -c "
import sys, json
records = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r.get('fetch_failed') or r.get('auth_failed'):
        continue
    loc = r.get('location')
    if loc and loc.get('lat') is not None:
        records.append(r)
if records:
    best = records[-1]
    print(json.dumps(best['location']))
" 2>/dev/null || echo "null")

    if [ "$latest_loc" != "null" ] && [ -n "$latest_loc" ]; then
        icloud_location="$latest_loc"

        # Compute is_home by comparing against home-coords.json (1 decimal redaction
        # in fixtures, but the real home-coords file has full precision).
        if [ -f "$HOME_COORDS_FILE" ]; then
            icloud_is_home=$(python3 -c "
import json, math, sys
try:
    loc = json.loads('''$latest_loc''')
    home = json.load(open('$HOME_COORDS_FILE'))
    lat1, lng1 = loc['lat'], loc['lng']
    lat2, lng2 = home['lat'], home['lng']
    # Haversine distance in metres
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    dist = R * 2 * math.asin(math.sqrt(a))
    print('true' if dist < 200 else 'false')
except Exception as e:
    print('false', file=sys.stderr)
    print('false')
" 2>/dev/null || echo "false")
        fi
    fi
fi

# ── Signal 5: iVideon motion events (last 24h) ───────────────────────────────
# Uses _ivideon_client.py. Failure is non-fatal (returns empty array).

ivideon_events="[]"
# Inner python's except branch already prints '[]' on any failure, so no outer
# `|| echo "[]"` fallback is needed. Crucially, the outer fallback was a bug
# under `set -o pipefail`: when _ivideon_client.py failed (e.g. auth error),
# the inner python's except path printed '[]' first, then the pipe's non-zero
# exit (from pipefail picking up the FIRST failing command) fired the `|| echo
# "[]"` AGAIN, producing `[]\n[]`. That two-line value then broke the final
# panel-construction heredoc with `SyntaxError: invalid syntax` at the line
# where $ivideon_events expanded.
ivideon_events=$(python3 "$CHASSIS_DIR/scripts/_ivideon_client.py" events "motion/started" 86400 2>/dev/null \
    | python3 -c "
import json, sys, datetime
try:
    raw = json.load(sys.stdin)
    out = []
    for e in (raw if isinstance(raw, list) else []):
        ts = e.get('time', 0)
        if ts:
            dt = datetime.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%dT%H:%M:%SZ')
            out.append({'ts': dt, 'camera': e.get('source_id', '')})
    print(json.dumps(out))
except Exception:
    print('[]')
" 2>/dev/null) || ivideon_events="[]"

# Update last_seen if any motion event is more recent
if [ "$ivideon_events" != "[]" ]; then
    latest_motion_epoch=$(echo "$ivideon_events" | python3 -c "
import json, sys, datetime
try:
    evts = json.load(sys.stdin)
    if evts:
        latest = max(evts, key=lambda e: e['ts'])
        dt = datetime.datetime.strptime(latest['ts'], '%Y-%m-%dT%H:%M:%SZ')
        print(int(dt.timestamp()))
    else:
        print(0)
except Exception:
    print(0)
" 2>/dev/null || echo 0)
    if [ "$latest_motion_epoch" -gt "$last_seen" ] 2>/dev/null; then
        last_seen="$latest_motion_epoch"
    fi
fi

# ── Signal 6: Oura last sleep session + last HR sample ───────────────────────
# Reads from Postgres oura_sleep_sessions table via _chassis_db.py connect pattern.

oura_last_sleep="null"
oura_last_hr="null"
oura_data=$(python3 -c "
import sys
sys.path.insert(0, '$CHASSIS_DIR/scripts')
from _chassis_db import connect, get_backend
import json
try:
    conn = connect()
    cur = conn.cursor()
    if get_backend() == 'pg':
        ph = '%s'
    else:
        ph = '?'
    # Latest sleep session
    cur.execute('''
        SELECT session_start, session_end,
               ROUND(EXTRACT(EPOCH FROM (session_end - session_start)) / 3600.0, 2) AS hrs
        FROM oura_sleep_sessions
        ORDER BY session_start DESC
        LIMIT 1
    ''')
    row = cur.fetchone()
    sleep = None
    if row:
        sleep = {
            'start': row[0].isoformat() if hasattr(row[0], 'isoformat') else str(row[0]),
            'end':   row[1].isoformat() if hasattr(row[1], 'isoformat') else str(row[1]),
            'hrs':   float(row[2]) if row[2] else None
        }
    # Latest HR sample from oura_daily (heart_rate_avg as proxy for last sync)
    cur.execute('''
        SELECT date, heart_rate_avg
        FROM oura_daily
        ORDER BY date DESC
        LIMIT 1
    ''')
    hr_row = cur.fetchone()
    hr = None
    if hr_row and hr_row[0]:
        hr = str(hr_row[0]) + 'T07:00:00Z'
    conn.close()
    print(json.dumps({'sleep': sleep, 'hr': hr}))
except Exception as e:
    print(json.dumps({'sleep': None, 'hr': None, 'error': str(e)[:200]}))
" 2>/dev/null || echo '{"sleep": null, "hr": null}')

oura_last_sleep=$(echo "$oura_data" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('sleep')))" 2>/dev/null || echo "null")
oura_last_hr=$(echo "$oura_data" | python3 -c "import sys,json; d=json.load(sys.stdin); v=d.get('hr'); print(json.dumps(v))" 2>/dev/null || echo "null")

# Use sleep start as a proof-of-life signal
if [ "$oura_last_sleep" != "null" ] && [ "$oura_last_sleep" != "" ]; then
    sleep_start_epoch=$(echo "$oura_last_sleep" | python3 -c "
import sys, json, datetime
try:
    s = json.load(sys.stdin)
    dt_str = s.get('start', '')
    dt = datetime.datetime.fromisoformat(dt_str.replace('Z','+00:00'))
    print(int(dt.timestamp()))
except Exception:
    print(0)
" 2>/dev/null || echo 0)
    if [ "$sleep_start_epoch" -gt "$last_seen" ] 2>/dev/null; then
        last_seen="$sleep_start_epoch"
    fi
fi

# ── Signal 7: Calendar events (today + yesterday) ────────────────────────────
# Read from the cached morning-briefing calendar data if available,
# otherwise from a calendar cache file written by the morning briefing.
# Shell scripts cannot call MCP tools directly, so we rely on a cache file.

CALENDAR_CACHE="$CHASSIS_DIR/data/welfare-calendar-cache.json"
calendar_today="[]"
calendar_yesterday="[]"
if [ -f "$CALENDAR_CACHE" ]; then
    cache_age=$(( now - $(date -r "$CALENDAR_CACHE" +%s 2>/dev/null || echo 0) ))
    if [ "$cache_age" -lt 86400 ]; then
        calendar_today=$(jq -r '.today // []' "$CALENDAR_CACHE" 2>/dev/null || echo "[]")
        calendar_yesterday=$(jq -r '.yesterday // []' "$CALENDAR_CACHE" 2>/dev/null || echo "[]")
    fi
fi

# ── Anchor to now if all signals are missing ─────────────────────────────────
if [ "$last_seen" -le 0 ] 2>/dev/null; then
    last_seen="$now"
fi

# Persist the max for next run's baseline
echo "$last_seen" > "$LAST_SEEN_FILE"

# ── Compute hours_since_anything ─────────────────────────────────────────────

hours_since=$(python3 -c "print(($now - $last_seen) // 3600)" 2>/dev/null || echo 0)
last_seen_iso=$(date -u -r "$last_seen" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "null")

# ── Output full signal panel ─────────────────────────────────────────────────
# The dispatcher evaluates: hours_since_anything > 18 AND is_quiet_hours == false
# count is set to 1 when both conditions are true, 0 otherwise.

should_fire=0
if [ "$hours_since" -gt 18 ] && [ "$is_quiet_hours" = "false" ]; then
    should_fire=1
fi

# Redact exact coords from gather output (coords stay in ~/.angel-vault only).
# The is_home flag and location age/staleness are safe to surface.
icloud_safe="null"
if [ "$icloud_location" != "null" ] && [ -n "$icloud_location" ]; then
    icloud_safe=$(echo "$icloud_location" | python3 -c "
import sys, json
loc = json.load(sys.stdin)
# Remove precise coords; keep metadata
safe = {
    'is_stale': loc.get('location_stale', False),
    'age_min': loc.get('location_age_min'),
    'accuracy_m': loc.get('accuracy_m'),
    'has_coords': (loc.get('lat') is not None),
}
print(json.dumps(safe))
" 2>/dev/null || echo "null")
fi

# Build the panel via a JSON heredoc parsed by json.loads, not a Python dict
# literal with JSON values interpolated. The dict-literal approach was a
# whack-a-mole maintenance burden: bare `null` (valid JSON, breaks Python),
# bare `true`/`false` (Python wants True/False), multi-line `[]` array values,
# and unquoted strings all caused fragile interpolation failures. JSON
# accepts every interpolated value as-is here because every $var that lands
# in this block was either produced by json.dumps upstream or is a numeric
# literal — both of which are valid JSON tokens.
python3 -c "
import json, sys
panel = json.loads('''
{
    \"now\": \"$now_iso\",
    \"last_seen\": \"$last_seen_iso\",
    \"last_discord_message\": $( [ "$last_discord_message" = "null" ] && echo "null" || echo "\"$last_discord_message\"" ),
    \"mac_hid_active\": $( [ "$mac_hid_active" = "null" ] && echo "null" || echo "\"$mac_hid_active\"" ),
    \"icloud_location_meta\": $icloud_safe,
    \"icloud_is_home\": $icloud_is_home,
    \"ivideon_motion_events_last_24h\": $ivideon_events,
    \"oura_last_sleep_session\": $oura_last_sleep,
    \"oura_last_hr_sample\": $oura_last_hr,
    \"calendar_today\": $calendar_today,
    \"calendar_yesterday\": $calendar_yesterday,
    \"hours_since_anything\": $hours_since,
    \"current_local_time\": \"$current_local_time\",
    \"is_quiet_hours\": $is_quiet_hours,
    \"dry_run\": $([ \"\${WELFARE_DRY_RUN:-false}\" = 'true' ] && echo 'true' || echo 'false'),
    \"count\": $should_fire
}
''')
print(json.dumps(panel))
"
