#!/bin/bash
# gather-bfl-photos.sh — heartbeat gather script for BFL photo ingest.
#
# Polls a Discord channel for new photo messages since last run. Downloads
# images, runs vision-extract (skips already-extracted), ingests via
# bfl-ingest.py, posts confirmations to the health channel via webhook.
#
# Exit stdout contract (for the chassis dispatcher):
#   {"count": N, "failed": [...]}
#   N = number of items that FAILED local processing and need Claude rescue.
#   N=0 means clean run. "failed" is a list of {photo, extraction} path pairs
#   for items where the ingest step exited non-zero (excluding rc=2 which means
#   classification=other). MUST be JSON — the dispatcher parses via jq.
#   A bare `count=N` string falls through to the line-count fallback (always 1)
#   and fires every tick.
#
# Env vars required:
#   CHASSIS_HOME            chassis root (set by dispatcher)
#   DISCORD_BOT_TOKEN       bot token (must have read access to the channel)
#   HEALTH_CHANNEL_ID       Discord channel ID
#   HEALTH_WEBHOOK_URL      webhook for posting ingest confirmations (optional)
#   BFL_ARCHIVE_DIR         archive root; default ~/behalfbot-archive/bfl

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (chassis dispatcher contract)}"
: "${DISCORD_BOT_TOKEN:?DISCORD_BOT_TOKEN must be set}"
: "${HEALTH_CHANNEL_ID:?HEALTH_CHANNEL_ID must be set}"

PLUGIN_DIR="${CHASSIS_HOME}/plugins/bfl"
STATE_FILE="${CHASSIS_HOME}/scheduled-tasks/bfl-health-state.json"
ARCHIVE_DIR="${BFL_ARCHIVE_DIR:-$HOME/behalfbot-archive/bfl}"
RAW_DIR="${ARCHIVE_DIR}/raw"
EXTRACTIONS_DIR="${ARCHIVE_DIR}/extractions"

mkdir -p "$RAW_DIR" "$EXTRACTIONS_DIR" "$(dirname "$STATE_FILE")"

# Initialize state file if missing
if [ ! -f "$STATE_FILE" ]; then
  echo '{"last_message_id": null}' > "$STATE_FILE"
fi

PYTHON="${PYTHON:-python3}"

PLUGIN_DIR_ENV="$PLUGIN_DIR" RAW_DIR_ENV="$RAW_DIR" EXTRACTIONS_DIR_ENV="$EXTRACTIONS_DIR" STATE_FILE_ENV="$STATE_FILE" \
"$PYTHON" <<'PY'
import json, os, pathlib, subprocess, sys, urllib.request

PLUGIN_DIR = pathlib.Path(os.environ["PLUGIN_DIR_ENV"])
RAW_DIR = pathlib.Path(os.environ["RAW_DIR_ENV"])
EXTRACTIONS_DIR = pathlib.Path(os.environ["EXTRACTIONS_DIR_ENV"])
STATE_FILE = pathlib.Path(os.environ["STATE_FILE_ENV"])

channel = os.environ["HEALTH_CHANNEL_ID"]
token = os.environ["DISCORD_BOT_TOKEN"]
last_id = json.loads(STATE_FILE.read_text()).get("last_message_id")

# Step 1: Poll Discord
url = f"https://discord.com/api/v10/channels/{channel}/messages?limit=50"
if last_id:
    url += f"&after={last_id}"
req = urllib.request.Request(url, headers={
    "Authorization": f"Bot {token}",
    "User-Agent": "behalfbot-bfl-gather (1.0)",
})
try:
    raw = urllib.request.urlopen(req, timeout=15).read()
    messages = json.loads(raw)
except Exception as e:
    print(f"DISCORD-API-FAIL: {e}", file=sys.stderr)
    print(json.dumps({"count": 0}))
    sys.exit(0)

# Collect image attachments in chronological order. Capture the message
# `content` (text typed alongside the photo) so we can write it as a
# sibling `.caption.txt` — bfl-ingest reads that into bfl_meals.description.
# Also capture the Discord message `timestamp` (ISO-8601 UTC) so a sibling
# `.timestamp.txt` can drive bfl_meals.time_actual — Discord strips EXIF
# from image attachments, so the message-send time is the closest reliable
# proxy for "when was this meal eaten" we have.
items = []
for m in messages:
    content = (m.get("content") or "").strip()
    msg_ts = m.get("timestamp") or ""  # ISO-8601 UTC
    for a in m.get("attachments", []):
        if (a.get("content_type") or "").startswith("image/"):
            items.append({
                "message_id": m["id"],
                "url": a["url"],
                "filename": a["filename"],
                "caption": content,
                "message_timestamp": msg_ts,
            })
items.reverse()  # Discord returns newest-first

# Even with no new Discord messages, we may have orphaned images from prior
# failed ticks (vision-extract transient errors that left no extraction JSON).
# Bounded retry: only retry orphans modified within the last 7 days.
import time as _time
_RETRY_WINDOW_S = 7 * 86400
_orphan_now = _time.time()

def _is_recent_orphan(p):
    if (EXTRACTIONS_DIR / (p.stem + ".json")).exists():
        return False
    try:
        return (_orphan_now - p.stat().st_mtime) < _RETRY_WINDOW_S
    except OSError:
        return False

_orphan_check = [p for p in (list(RAW_DIR.glob("*.jpg")) +
                             list(RAW_DIR.glob("*.jpeg")) +
                             list(RAW_DIR.glob("*.png")))
                 if _is_recent_orphan(p)]
if not items and not _orphan_check:
    print(json.dumps({"count": 0}))
    sys.exit(0)

# Step 2: Download all new images. Also write the Discord message caption
# to a sibling `.caption.txt` so bfl-ingest can pick it up as the meal
# description.
failed = 0
failed_items = []
downloaded = []
for it in items:
    target = RAW_DIR / it["filename"]
    stem = target.stem
    caption_target = RAW_DIR / f"{stem}.caption.txt"
    if target.exists() and target.stat().st_size > 0:
        downloaded.append(target)
    else:
        dl_req = urllib.request.Request(it["url"], headers={"User-Agent": "Mozilla/5.0 behalfbot-bfl-gather"})
        try:
            with urllib.request.urlopen(dl_req, timeout=30) as r:
                target.write_bytes(r.read())
            downloaded.append(target)
        except Exception as e:
            print(f"DOWNLOAD-FAIL {it['filename']}: {e}", file=sys.stderr)
            failed += 1
            continue
    if it.get("caption") and not caption_target.exists():
        try:
            caption_target.write_text(it["caption"])
        except Exception as e:
            print(f"CAPTION-WRITE-FAIL {stem}: {e}", file=sys.stderr)
    # Write Discord message timestamp as ISO-8601 UTC so bfl-ingest can
    # populate bfl_meals.time_actual. Discord strips EXIF from image
    # attachments, so the message-send time is the best proxy for meal time.
    timestamp_target = RAW_DIR / f"{stem}.timestamp.txt"
    if it.get("message_timestamp") and not timestamp_target.exists():
        try:
            timestamp_target.write_text(it["message_timestamp"])
        except Exception as e:
            print(f"TIMESTAMP-WRITE-FAIL {stem}: {e}", file=sys.stderr)

    # Caption-derived date override. Lets the installer attribute a meal to
    # a specific day when uploading after midnight or backfilling. Patterns:
    #   "for: yesterday"      → previous day relative to message-send time
    #   "for yesterday"       → same (colon optional)
    #   "for: 2026-05-08"     → explicit date
    #   "@2026-05-08"         → explicit date (anywhere in caption)
    # Date validation: must be a real YYYY-MM-DD. Invalid dates are ignored
    # silently — the 4-hour BFL day-boundary heuristic in exif_date() is
    # the fallback if no override is present.
    override_target = RAW_DIR / f"{stem}.date_override.txt"
    caption = (it.get("caption") or "").strip().lower()
    msg_ts = it.get("message_timestamp") or ""
    if caption and not override_target.exists():
        import re as _re
        from datetime import datetime as _dt, timedelta as _td
        override_date = None
        # Pattern 1: "for: yesterday" / "for yesterday"
        if _re.search(r"\bfor\b\s*:?\s*yesterday\b", caption):
            try:
                msg_dt = _dt.fromisoformat(msg_ts.replace("Z", "+00:00"))
                # Use installer-local TZ for "yesterday" semantics.
                # CHASSIS_TIMEZONE is set in chassis.config.yaml; defaults
                # to system local. Reading via zoneinfo when present.
                tz_name = os.environ.get("CHASSIS_TIMEZONE", "")
                msg_local = msg_dt
                if tz_name:
                    try:
                        from zoneinfo import ZoneInfo
                        msg_local = msg_dt.astimezone(ZoneInfo(tz_name))
                    except Exception:
                        msg_local = msg_dt.astimezone()
                else:
                    msg_local = msg_dt.astimezone()
                override_date = (msg_local - _td(days=1)).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pass
        # Pattern 2: "for: YYYY-MM-DD" or "for YYYY-MM-DD"
        if not override_date:
            m = _re.search(r"\bfor\b\s*:?\s*(\d{4}-\d{2}-\d{2})\b", caption)
            if m:
                override_date = m.group(1)
        # Pattern 3: "@YYYY-MM-DD" anywhere
        if not override_date:
            m = _re.search(r"@(\d{4}-\d{2}-\d{2})\b", caption)
            if m:
                override_date = m.group(1)
        # Validate the matched date is actually a real calendar date
        if override_date:
            try:
                _dt.strptime(override_date, "%Y-%m-%d")
                override_target.write_text(override_date)
            except (ValueError, OSError) as e:
                print(f"DATE-OVERRIDE-INVALID {stem}: {override_date} ({e})", file=sys.stderr)

# Step 3: Vision-extract any image without an extraction JSON. Scans the
# entire raw/ directory rather than only this-tick's downloads — catches
# orphans from prior failed ticks where vision-extract hit a transient API
# error and never wrote the JSON. Vision-extract internally skips
# already-extracted images, so the happy-path remains a no-op. 7-day window
# keeps stale, permanently-failed images from spamming retries forever.
new_basenames = sorted({p.stem for p in _orphan_check} | {p.stem for p in downloaded
                       if not (EXTRACTIONS_DIR / (p.stem + ".json")).exists()})
if new_basenames:
    only_args = []
    for b in new_basenames:
        only_args += ["--only", b]
    # Redirect subprocess stdout to gather's stderr so vision-extract's
    # diagnostic prints don't pollute gather's JSON contract on stdout.
    r = subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "scripts/bfl-vision-extract.py"),
         "--input-dir", str(RAW_DIR), "--out-dir", str(EXTRACTIONS_DIR), *only_args],
        stdout=sys.stderr.fileno(),
        timeout=1800,
    )
    if r.returncode != 0:
        print(f"EXTRACT-FAIL batch: rc={r.returncode}", file=sys.stderr)
        # Don't early-exit; ingest what we have

# Step 4: Ingest each new extraction JSON
for basename in new_basenames:
    extraction_json = EXTRACTIONS_DIR / (basename + ".json")
    if not extraction_json.exists():
        print(f"INGEST-SKIP {basename}: no extraction JSON emitted", file=sys.stderr)
        failed += 1
        continue
    photo = RAW_DIR / (basename + ".jpg")
    if not photo.exists():
        photo = RAW_DIR / (basename + ".jpeg")
    if not photo.exists():
        photo = RAW_DIR / (basename + ".png")
    rc = subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "scripts/bfl-ingest.py"),
         str(extraction_json), "--photo", str(photo)],
        stdout=sys.stderr.fileno(),
        timeout=60,
    ).returncode
    if rc not in (0, 2):
        print(f"INGEST-FAIL {basename}: rc={rc}", file=sys.stderr)
        failed += 1
        failed_items.append({"photo": str(photo), "extraction": str(extraction_json)})
        continue  # don't try FDC on a failed ingest

    # Step 4b: FDC enrichment — only runs for food_photo classification
    # (the enrich script filters internally). Wall-time budget generous
    # because it can make ~1 FDC API call per item.
    try:
        extraction_data = json.loads(extraction_json.read_text())
    except Exception:
        extraction_data = {}
    if extraction_data.get("classification") == "food_photo":
        fdc_rc = subprocess.run(
            [sys.executable, str(PLUGIN_DIR / "scripts/bfl-fdc-enrich.py"),
             "--only", basename],
            stdout=sys.stderr.fileno(),
            timeout=180,
        ).returncode
        if fdc_rc != 0:
            # FDC failure is not fatal for the overall heartbeat — photo is
            # already ingested with vision estimates.
            print(f"FDC-ENRICH-FAIL {basename}: rc={fdc_rc}", file=sys.stderr)

# Step 5: Update state file with newest message ID seen
if items:
    newest = items[-1]["message_id"]
    STATE_FILE.write_text(json.dumps({"last_message_id": newest}))

print(json.dumps({"count": failed, "failed": failed_items}))
PY
