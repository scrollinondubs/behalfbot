#!/usr/bin/env python3
"""bfl-ingest.py — take a vision-extraction JSON and upsert rows into the BFL DB.

Consumes the JSON blobs written by `bfl-vision-extract.py`. Does NOT call any
vision model itself — that work already happened. Pure DB + file correlation
+ Discord notification.

Usage:
    plugins/bfl/scripts/bfl-ingest.py <extraction.json> [--photo path/to/raw.jpg]

Exit codes:
    0  ingested successfully
    1  extraction JSON malformed or DB write failed (caller should trigger
       Claude fallback via the heartbeat's subagent prompt)
    2  classification == "other" — nothing to do, not an error

Sister docs: plugins/bfl/skills/bfl.md, plugins/bfl/db/migrations/001_bfl.sql.
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
HEALTH_WEBHOOK_ENV = "HEALTH_WEBHOOK_URL"


def load_env() -> dict:
    """Load chassis-side env. Reads ${CHASSIS_HOME}/.env when CHASSIS_HOME set,
    else falls back to plugin-relative .env. Path-portable across installers."""
    env = {}
    chassis_home = os.environ.get("CHASSIS_HOME")
    candidates = []
    if chassis_home:
        candidates.append(pathlib.Path(chassis_home) / ".env")
    candidates.append(PLUGIN_DIR.parent.parent / ".env")  # chassis root
    for env_file in candidates:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
            break
    return env


def _read_message_timestamp(photo: pathlib.Path) -> datetime | None:
    """Sibling `.timestamp.txt` written by gather-new-health-attachments.sh.

    Contents: ISO-8601 UTC string from the Discord message timestamp. Discord
    strips EXIF from image attachments, so the message-send time is the
    closest reliable proxy for meal time we have.
    """
    sidecar = photo.with_name(photo.stem + ".timestamp.txt")
    if not sidecar.exists():
        return None
    try:
        s = sidecar.read_text().strip()
        # Python <3.11 fromisoformat doesn't handle Z suffix; normalize it.
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (OSError, ValueError):
        return None


def _read_pil_exif_datetime(photo: pathlib.Path) -> datetime | None:
    """Pull DateTimeOriginal from EXIF via PIL.

    No external binary dependency. Discord-uploaded images usually have no
    EXIF, but iCloud Photo Library exports / direct file uploads do — so
    this is a real fallback path, not just a placeholder.
    """
    try:
        from PIL import Image, ExifTags
    except ImportError:
        return None
    try:
        with Image.open(photo) as img:
            exif = img._getexif() or {}
    except Exception:
        return None
    tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
    raw = tags.get("DateTimeOriginal") or tags.get("DateTime")
    if not raw:
        return None
    try:
        # EXIF format: "YYYY:MM:DD HH:MM:SS"
        dt = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    # EXIF lacks timezone unless OffsetTimeOriginal is present. Treat as
    # local-naive and assume Lisbon (Europe/Lisbon) since that's the
    # installer's location. If we want to support other installers later,
    # read timezone from chassis config.
    offset = tags.get("OffsetTimeOriginal") or tags.get("OffsetTime")
    if offset:
        try:
            sign = 1 if offset[0] == "+" else -1
            hh, mm = offset[1:].split(":")
            tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
            return dt.replace(tzinfo=tz).astimezone(timezone.utc)
        except (ValueError, IndexError):
            pass
    # Fallback: assume the photo was taken in the same timezone the script
    # runs in. Convert local-naive → local-aware → UTC.
    try:
        local = dt.astimezone()  # 3.6+: attaches local tz
        return local.astimezone(timezone.utc)
    except (ValueError, OSError):
        return None


def _read_date_override_sidecar(photo: pathlib.Path) -> str | None:
    """Sibling `.date_override.txt` written by gather-new-health-attachments.sh
    when the user's caption matches a date-override pattern (`for: yesterday`,
    `for: 2026-05-08`, `@2026-05-08`). Lets Sean attribute meals to the right
    day when uploading after midnight (#528).

    Returns YYYY-MM-DD or None.
    """
    sidecar = photo.with_name(photo.stem + ".date_override.txt")
    if not sidecar.exists():
        return None
    try:
        s = sidecar.read_text().strip()
        # Validate format
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            datetime.strptime(s, "%Y-%m-%d")  # raises if invalid
            return s
    except (OSError, ValueError):
        pass
    return None


def _read_time_override_sidecar(
    photo: pathlib.Path, bfl_date: str, msg_dt: datetime | None
) -> datetime | None:
    """Sibling `.time_override.txt` written by the install's photo-ingest
    pipeline when the caption contains an explicit time annotation (e.g.
    "at 6:30pm", "@18:30", "noon"). Solves the case where Discord iOS
    strips EXIF so the Discord upload timestamp is all we would otherwise
    have. Brings <v1-reference-install> PR #553 upstream.

    Contents are time-only in `H:MM AM/PM` format (matches exif_time() output).
    Combined with `bfl_date` (YYYY-MM-DD, install-local TZ) to produce a
    full UTC datetime.

    Returns UTC datetime or None.
    """
    sidecar = photo.with_name(photo.stem + ".time_override.txt")
    if not sidecar.exists():
        return None
    try:
        time_str = sidecar.read_text().strip()
    except OSError:
        return None
    try:
        naive = datetime.strptime(f"{bfl_date} {time_str}", "%Y-%m-%d %I:%M %p")
    except ValueError:
        return None
    tz = _local_tz()
    if tz is not None:
        try:
            aware = naive.replace(tzinfo=tz)
        except Exception:
            aware = naive.astimezone()
    else:
        aware = naive.astimezone()
    result = aware.astimezone(timezone.utc)
    print(
        f"[bfl-ingest] using caption time-override for {photo.name}: "
        f"{time_str} (Discord upload was "
        f"{msg_dt.isoformat() if msg_dt else 'unavailable'})",
        file=sys.stderr,
    )
    return result


def photo_capture_dt(photo: pathlib.Path) -> datetime | None:
    """Best-effort UTC datetime for when the photo was captured / sent.

    Resolution order (<v1-reference-install> PR #552 + #553 — EXIF-primary, caption-time fallback):
      1. EXIF DateTimeOriginal via PIL - preferred when present and sane.
         File-attachment uploads, desktop drag-drop, and "send as file" paths
         all preserve EXIF; only iOS photo-library uploads strip it.
      2. Sibling .time_override.txt (caption-derived time) - addresses the case
         where Discord iOS strips EXIF on all upload paths. The installer types
         the meal time inline in their caption; the install's gather script
         parses + writes the sidecar. Combined with the BFL date derived from
         the Discord message timestamp (or wall clock) to form a full UTC
         datetime.
      3. Sibling .timestamp.txt (Discord message-send time) - fallback when
         EXIF is absent/stripped and no caption time was given.
      4. None - caller falls back to mtime for date-only purposes.

    Staleness guard: if EXIF DateTimeOriginal is more than 90 days older than
    the Discord message timestamp (or wall clock when no sidecar exists), the
    EXIF value is discarded and we fall through to step 2/3.
    Rationale: screenshots of old photos and iCloud download artifacts can
    carry stale EXIF that would incorrectly back-date a meal entry.
    """
    msg_dt = _read_message_timestamp(photo)
    exif_dt = _read_pil_exif_datetime(photo)

    if exif_dt is not None:
        now_ref = msg_dt if msg_dt is not None else datetime.now(timezone.utc)
        age_days = (now_ref - exif_dt).days
        if age_days > 90:
            print(
                f"[bfl-ingest] EXIF DateTimeOriginal stale for {photo.name}: "
                f"{exif_dt.isoformat()} ({age_days} days old, > 90 day sanity "
                f"threshold) - falling back to Discord timestamp",
                file=sys.stderr,
            )
            exif_dt = None
        else:
            print(
                f"[bfl-ingest] using EXIF DateTimeOriginal for {photo.name}: "
                f"{exif_dt.isoformat()} (Discord upload was "
                f"{msg_dt.isoformat() if msg_dt else 'unavailable'})",
                file=sys.stderr,
            )
            return exif_dt

    # EXIF absent or discarded. Try caption-derived time override before
    # falling back to the raw Discord upload timestamp.
    ref_dt = msg_dt if msg_dt is not None else datetime.now(timezone.utc)
    tz = _local_tz()
    ref_local = ref_dt.astimezone(tz) if tz is not None else ref_dt.astimezone()
    bfl_date = _bfl_day_for(ref_local)

    # Also respect the date_override sidecar so that a "for: yesterday" caption
    # tag correctly anchors the time_override to the right day.
    sidecar_date = _read_date_override_sidecar(photo)
    if sidecar_date:
        bfl_date = sidecar_date

    time_override_dt = _read_time_override_sidecar(photo, bfl_date, msg_dt)
    if time_override_dt is not None:
        return time_override_dt

    return msg_dt


def _local_tz():
    """Installer's local timezone. Reads CHASSIS_TIMEZONE env var (set by
    chassis bootstrap from chassis.config.yaml) and resolves via zoneinfo.
    Falls back to system local when CHASSIS_TIMEZONE is unset, which is the
    right behavior for a chassis with no installer-config TZ."""
    tz_name = os.environ.get("CHASSIS_TIMEZONE", "")
    if not tz_name:
        return None  # caller falls back to .astimezone() (system local)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:
        return None


def exif_time(photo: pathlib.Path) -> str | None:
    """Return meal time as `H:MM AM/PM` in the installer's local TZ, or None.

    Output matches the format bfl-backfill-meal.py accepts so downstream
    consumers can treat manual + photo-derived rows uniformly.
    """
    dt = photo_capture_dt(photo)
    if dt is None:
        return None
    tz = _local_tz()
    if tz is not None:
        local = dt.astimezone(tz)
    else:
        local = dt.astimezone()
    return local.strftime("%-I:%M %p")  # e.g. "8:34 AM"


def _bfl_day_for(local_dt: datetime) -> str:
    """BFL day boundary is 04:00 local. Photos arriving 00:00-04:00 belong to
    the previous calendar day — BFL meals don't normally happen at 1am, so a
    photo that lands then almost certainly represents a meal from the prior
    day uploaded after midnight (#528).

    Edge case: workout / aerobic photos at 1am are also unusual, so the same
    rollback applies uniformly regardless of day_type.
    """
    if local_dt.hour < 4:
        return (local_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    return local_dt.strftime("%Y-%m-%d")


def exif_date(photo: pathlib.Path) -> str | None:
    """Return YYYY-MM-DD for the photo's BFL day.

    Resolution order matches photo_capture_dt; falls back to mtime when no
    EXIF / sibling timestamp available so daily rollup still works.

    Date is the **BFL day** the meal/workout belongs to, not strictly the
    calendar day of capture. See `_bfl_day_for` — photos arriving 00:00-04:00
    local roll back to the previous day per #528.
    """
    dt = photo_capture_dt(photo)
    if dt is not None:
        tz = _local_tz()
        local = dt.astimezone(tz) if tz is not None else dt.astimezone()
        return _bfl_day_for(local)
    ts = datetime.fromtimestamp(photo.stat().st_mtime, timezone.utc)
    tz = _local_tz()
    if tz is not None:
        ts = ts.astimezone(tz)
    return _bfl_day_for(ts)


def post_to_discord(webhook_url: str, text: str) -> None:
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps({"content": text}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "<assistant>-bfl-ingest (1.0)"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"WARN: discord post failed: {e}", file=sys.stderr)


def upsert_day(cur, date: str, day_type: str | None, workout_start: str | None,
               notebook_workout_photo: str | None, notebook_meal_photo: str | None,
               total_protein_portions: float | None, total_carb_portions: float | None,
               total_water_cups: float | None, ocr_raw_json: str) -> int:
    cur.execute(
        "INSERT INTO bfl_days (date, day_type, workout_start, notebook_workout_photo, "
        "notebook_meal_photo, total_protein_portions, total_carb_portions, "
        "total_water_cups, ocr_raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET "
        "  day_type = COALESCE(excluded.day_type, bfl_days.day_type), "
        "  workout_start = COALESCE(excluded.workout_start, bfl_days.workout_start), "
        "  notebook_workout_photo = COALESCE(excluded.notebook_workout_photo, bfl_days.notebook_workout_photo), "
        "  notebook_meal_photo = COALESCE(excluded.notebook_meal_photo, bfl_days.notebook_meal_photo), "
        "  total_protein_portions = COALESCE(excluded.total_protein_portions, bfl_days.total_protein_portions), "
        "  total_carb_portions = COALESCE(excluded.total_carb_portions, bfl_days.total_carb_portions), "
        "  total_water_cups = COALESCE(excluded.total_water_cups, bfl_days.total_water_cups), "
        "  ocr_raw_json = excluded.ocr_raw_json, "
        "  updated_at = EXTRACT(EPOCH FROM NOW())::bigint",
        (date, day_type, workout_start, notebook_workout_photo, notebook_meal_photo,
         total_protein_portions, total_carb_portions, total_water_cups, ocr_raw_json),
    )
    return cur.execute("SELECT id FROM bfl_days WHERE date = ?", (date,)).fetchone()[0]


def ingest_workout(cur, day_id: int, extraction: dict) -> tuple[int, int]:
    """Returns (exercises_count, sets_count)."""
    exercises = extraction.get("exercises") or []
    cur.execute("DELETE FROM bfl_workouts WHERE day_id = ?", (day_id,))
    total_sets = 0
    for order, ex in enumerate(exercises, start=1):
        name = (ex.get("name") or "").strip()
        if not name:
            continue
        muscle = ex.get("muscle_group")
        is_main = 1 if ex.get("is_main") else 0
        for s in ex.get("sets", []) or []:
            cur.execute(
                "INSERT INTO bfl_workouts (day_id, exercise_order, exercise_name, muscle_group, "
                "is_main, set_number, reps, weight_kg, intensity_level, up_arrow) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (day_id, order, name, muscle, is_main,
                 s.get("set"), s.get("reps"), s.get("weight_kg"), s.get("intensity"),
                 1 if s.get("up_arrow") else 0),
            )
            total_sets += 1
    return len(exercises), total_sets


def ingest_meals(cur, day_id: int, extraction: dict) -> int:
    meals = extraction.get("meals") or []
    cur.execute("DELETE FROM bfl_meals WHERE day_id = ?", (day_id,))
    for m in meals:
        cur.execute(
            "INSERT INTO bfl_meals (day_id, meal_num, time_actual, description, "
            "protein_portions, carb_portions) VALUES (?, ?, ?, ?, ?, ?)",
            (day_id, m.get("meal_num"), m.get("time_actual"), m.get("description"),
             m.get("protein_portions"), m.get("carb_portions")),
        )
    return len(meals)


def ingest_aerobic(cur, day_id: int, extraction: dict) -> int:
    """Upsert one row into bfl_aerobic for this day. Returns minutes logged."""
    cur.execute("DELETE FROM bfl_aerobic WHERE day_id = ?", (day_id,))
    activity = extraction.get("activity_type") or "run"
    total_min = extraction.get("total_minutes")
    actual = extraction.get("actual_intensities_json")
    notes = extraction.get("notes")
    cur.execute(
        "INSERT INTO bfl_aerobic (day_id, activity_type, total_minutes, actual_intensities_json, notes) "
        "VALUES (?, ?, ?, ?, ?)",
        (day_id, activity, total_min,
         json.dumps(actual) if isinstance(actual, list) else None, notes),
    )
    return total_min or 0


def ingest_food_photo(cur, date: str, extraction: dict, photo_path: str) -> int:
    """Insert a food photo as a new bfl_meals row.

    Sean stopped logging meals in the paper journal 2026-04-24 — the photo +
    optional caption is now the authoritative source. We insert every
    food_photo directly, no journal matching. If a sibling `.caption.txt`
    exists next to the photo, its contents become the description (Sean's
    explanation of ambiguous ingredients).

    Returns the new bfl_meals.id.
    """
    # Ensure a bfl_days row exists for the date; create stub if not (food
    # photos can land on days with no workout log).
    day_row = cur.execute("SELECT id FROM bfl_days WHERE date = ?", (date,)).fetchone()
    if day_row:
        day_id = day_row[0]
    else:
        cur.execute(
            "INSERT INTO bfl_days (date) VALUES (?) RETURNING id", (date,)
        )
        day_id = cur.fetchone()[0]

    # Read description from sibling sidecar files. Priority (<v1-reference-install> PR #553):
    #   1. description_clean.txt - caption with time-override phrase stripped
    #      (written by the install's gather script when a time annotation like
    #      "at 6:30pm" is removed so the time string doesn't pollute the meal
    #      description in the digest). Raw caption.txt is preserved for audit.
    #   2. caption.txt - raw Discord message caption (legacy + no-time-tag cases)
    description = None
    if photo_path:
        p_photo = pathlib.Path(photo_path)
        desc_clean = p_photo.with_name(p_photo.stem + ".description_clean.txt")
        caption_path = p_photo.with_suffix(p_photo.suffix + ".caption.txt")
        alt_caption = p_photo.with_name(p_photo.stem + ".caption.txt")
        for p in (desc_clean, caption_path, alt_caption):
            if p.exists():
                description = p.read_text().strip()
                break

    items = extraction.get("items") or []

    # Idempotency by food_photo_path: if a row already exists for this photo
    # on this day, UPDATE its vision-derived fields rather than inserting a
    # duplicate (#489). Without this, an orphan-retry from #485 (or any case
    # where the extraction JSON gets re-generated) creates duplicate
    # bfl_meals rows. We deliberately do NOT touch manual_* / fdc_* fields —
    # those represent post-ingest enrichment that should survive re-ingest.
    if photo_path:
        cur.execute(
            "SELECT id FROM bfl_meals WHERE food_photo_path = ? AND day_id = ?",
            (photo_path, day_id),
        )
        existing = cur.fetchone()
    else:
        existing = None

    # Derive meal time from sibling .timestamp.txt or EXIF (Discord strips
    # EXIF from image attachments, so the message-send time is the proxy).
    time_actual = exif_time(pathlib.Path(photo_path)) if photo_path else None

    if existing:
        # Only set time_actual on UPDATE if the existing row doesn't already
        # have one — preserve a manually-corrected time over re-ingestion.
        cur.execute(
            "UPDATE bfl_meals SET description = ?, est_calories = ?, "
            "est_protein_g = ?, est_carbs_g = ?, est_fat_g = ?, "
            "est_portion_g = ?, vision_confidence = ?, vision_items_json = ?, "
            "vision_notes = ?, photo_matched = 1, "
            "time_actual = COALESCE(time_actual, ?) WHERE id = ?",
            (
                description,
                extraction.get("estimated_calories"),
                extraction.get("estimated_protein_g"),
                extraction.get("estimated_carbs_g"),
                extraction.get("estimated_fat_g"),
                extraction.get("estimated_portion_grams"),
                extraction.get("confidence"),
                json.dumps(items),
                extraction.get("notes"),
                time_actual,
                existing[0],
            ),
        )
        return existing[0]

    # No existing row → INSERT. meal_num is NOT NULL in the schema. Without
    # a journal we don't know the "true" meal ordering, so assign the next
    # sequence number for the day.
    cur.execute(
        "SELECT COALESCE(MAX(meal_num), 0) + 1 FROM bfl_meals WHERE day_id = ?",
        (day_id,),
    )
    next_meal_num = cur.fetchone()[0]

    cur.execute(
        "INSERT INTO bfl_meals "
        "(day_id, meal_num, time_actual, description, food_photo_path, "
        " est_calories, est_protein_g, est_carbs_g, est_fat_g, est_portion_g, "
        " vision_confidence, vision_items_json, vision_notes, photo_matched) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) RETURNING id",
        (
            day_id,
            next_meal_num,
            time_actual,
            description,
            photo_path,
            extraction.get("estimated_calories"),
            extraction.get("estimated_protein_g"),
            extraction.get("estimated_carbs_g"),
            extraction.get("estimated_fat_g"),
            extraction.get("estimated_portion_grams"),
            extraction.get("confidence"),
            json.dumps(items),
            extraction.get("notes"),
        ),
    )
    return cur.fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("extraction_json")
    ap.add_argument("--photo", help="original photo path (for bfl_days.notebook_*_photo + EXIF date override)")
    ap.add_argument("--quiet", action="store_true", help="skip Discord post")
    ap.add_argument("--date-override", help="YYYY-MM-DD; forces this date, ignoring extraction + EXIF. Used by bulk backfill where dates are interpolated from sequence.")
    args = ap.parse_args()

    data = json.loads(pathlib.Path(args.extraction_json).read_text())
    classification = data.get("classification")
    extraction = data.get("extraction") or {}
    photo_path = args.photo or data.get("source")

    if classification == "other" or not extraction:
        print(f"skip: classification={classification}", file=sys.stderr)
        return 2

    # Date resolution:
    #   1. `--date-override` flag wins (bulk backfill uses this with interpolated dates)
    #   2. Sidecar `.date_override.txt` (caption-derived, e.g. "for: yesterday" — #528)
    #   3. Extraction date (Opus reads handwriting accurately)
    #   4. EXIF photo-capture date (fallback when extraction has no date)
    date = None
    if args.date_override:
        date = args.date_override
    if not date and photo_path:
        sidecar_override = _read_date_override_sidecar(pathlib.Path(photo_path))
        if sidecar_override:
            date = sidecar_override
            print(f"INFO: date overridden by sidecar -> {date}", file=sys.stderr)
    ext_date = extraction.get("date") if not date else None
    if ext_date and len(ext_date) == 10 and ext_date[4] == "-" and ext_date[7] == "-":
        # Reasonable sanity check: year within ±1 of EXIF year if we have one
        if photo_path:
            p = pathlib.Path(photo_path)
            if p.exists():
                exif = exif_date(p)
                if exif and abs(int(ext_date[:4]) - int(exif[:4])) <= 1:
                    date = ext_date
                elif exif:
                    # Extraction date looks off (hallucination). Fall back to EXIF.
                    print(f"WARN: extraction date {ext_date} > 1y from EXIF {exif}, using EXIF", file=sys.stderr)
                    date = exif
                else:
                    date = ext_date
            else:
                date = ext_date
        else:
            date = ext_date
    # Last-ditch fallback to EXIF if extraction has no date
    if not date and photo_path:
        p = pathlib.Path(photo_path)
        if p.exists():
            date = exif_date(p)
    if not date:
        print(f"FAIL: no date resolvable for {args.extraction_json}", file=sys.stderr)
        return 1

    env = load_env()
    # Extraction cost breadcrumb from the vision-extract step
    cost_usd = data.get("cost_usd")
    extract_model = data.get("extract_model") or "?"
    cost_tag = f" · {extract_model} · ${cost_usd:.4f}" if cost_usd is not None else ""

    # Backend selected via USE_PG (see plugins/bfl/scripts/_chassis_db.py). SQLite
    # path unchanged; PG path uses a cursor wrapper that rewrites `?`
    # placeholders to `%s` so the existing SQL strings in upsert_day /
    # ingest_workout / etc work on both dialects without duplication.
    sys.path.insert(0, str(PLUGIN_DIR / "scripts"))
    from _chassis_db import connect as _chassis_db_connect, cursor as _chassis_db_cursor, is_pg
    conn = _chassis_db_connect()
    cur = _chassis_db_cursor(conn)

    # Capture connection identity for the post-commit verification log line.
    # On PG this returns the DSN host:port + database name + transaction id;
    # on SQLite it returns the file path. The only diagnostic that surfaces
    # the cause if a future ingest claims success but data isn't readable
    # post-commit (scrollinondubs/new-jaxity#67 root cause: bake-env stale env
    # can point at a different DB than operators expect).
    conn_info = "?"
    try:
        if is_pg(conn):
            with conn.cursor() as ic:
                ic.execute(
                    "SELECT current_database(), inet_server_addr()::text, "
                    "inet_server_port(), txid_current()"
                )
                row = ic.fetchone()
                conn_info = (
                    f"pg db={row[0]} server={row[1]}:{row[2]} txid={row[3]}"
                )
        else:
            with conn:
                row = conn.execute("PRAGMA database_list").fetchone()
                conn_info = f"sqlite path={row[2] if row else '?'}"
    except Exception as info_err:  # noqa: BLE001
        conn_info = f"unknown ({info_err})"
    print(f"[bfl-ingest] writing to {conn_info}", file=sys.stderr)

    day_id_for_verify: int | None = None
    meal_id_for_verify: int | None = None
    try:
        msg = None
        if classification == "workout_log":
            day_id = upsert_day(
                cur, date,
                day_type=extraction.get("day_type"),
                workout_start=extraction.get("workout_start"),
                notebook_workout_photo=photo_path,
                notebook_meal_photo=None,
                total_protein_portions=None,
                total_carb_portions=None,
                total_water_cups=None,
                ocr_raw_json=json.dumps(data),
            )
            n_ex, n_sets = ingest_workout(cur, day_id, extraction)
            msg = f"✅ BFL workout {date}: {n_ex} exercises, {n_sets} sets logged{cost_tag}"
            day_id_for_verify = day_id
        elif classification == "aerobic_log":
            day_id = upsert_day(
                cur, date,
                day_type="aerobic",
                workout_start=extraction.get("workout_start"),
                notebook_workout_photo=photo_path,
                notebook_meal_photo=None,
                total_protein_portions=None,
                total_carb_portions=None,
                total_water_cups=None,
                ocr_raw_json=json.dumps(data),
            )
            mins = ingest_aerobic(cur, day_id, extraction)
            activity = extraction.get("activity_type") or "run"
            intensities = extraction.get("actual_intensities_json") or []
            msg = f"✅ BFL aerobic {date}: {activity} {mins}min ({len(intensities)} intensity samples){cost_tag}"
            day_id_for_verify = day_id
        elif classification == "meal_log":
            day_id = upsert_day(
                cur, date,
                day_type=None,
                workout_start=None,
                notebook_workout_photo=None,
                notebook_meal_photo=photo_path,
                total_protein_portions=extraction.get("total_protein_portions"),
                total_carb_portions=extraction.get("total_carb_portions"),
                total_water_cups=extraction.get("water_tick_marks") or extraction.get("total_water_cups"),
                ocr_raw_json=json.dumps(data),
            )
            n_meals = ingest_meals(cur, day_id, extraction)
            water = extraction.get("water_tick_marks") or extraction.get("total_water_cups") or 0
            msg = f"✅ BFL diet {date}: {n_meals} meals, {water} tick marks water{cost_tag}"
            day_id_for_verify = day_id
        elif classification == "food_photo":
            meal_id = ingest_food_photo(cur, date, extraction, photo_path or "")
            items = ", ".join((extraction.get("items") or [])[:3])
            kcal = extraction.get("estimated_calories", "?")
            msg = f"✅ BFL meal photo {date}: [{items}] ~{kcal} kcal (meal id={meal_id}){cost_tag}"
            meal_id_for_verify = meal_id
        else:
            print(f"unknown classification: {classification}", file=sys.stderr)
            return 1

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"FAIL: {e}", file=sys.stderr)
        return 1

    # Post-commit verification — open a FRESH connection (separate session,
    # picks up only committed data) and confirm the row is readable. Catches
    # the "ingest reported success but data didn't land" failure mode: if
    # .env.baked points at a stale or rotated DSN, the writer commits to one
    # DB while readers go to another, and only an out-of-process verifier
    # notices. Fail loudly here so callers (gather scripts that check
    # `rc not in (0, 2)`) escalate instead of swallowing.
    try:
        verify_conn = _chassis_db_connect()
        try:
            verify_cur = _chassis_db_cursor(verify_conn)
            if classification == "food_photo" and meal_id_for_verify is not None:
                verify_cur.execute("SELECT id FROM bfl_meals WHERE id = ?", (meal_id_for_verify,))
                if verify_cur.fetchone() is None:
                    print(
                        f"VERIFY-FAIL: bfl_meals row id={meal_id_for_verify} not visible from a fresh connection. "
                        f"Writer reported success but reader can't see the row. Writer was: {conn_info}. "
                        f"Check CHASSIS_PG_DSN / .env.baked for staleness.",
                        file=sys.stderr,
                    )
                    return 1
            elif day_id_for_verify is not None:
                verify_cur.execute("SELECT id FROM bfl_days WHERE id = ?", (day_id_for_verify,))
                if verify_cur.fetchone() is None:
                    print(
                        f"VERIFY-FAIL: bfl_days row id={day_id_for_verify} not visible from a fresh connection. "
                        f"Writer reported success but reader can't see the row. Writer was: {conn_info}. "
                        f"Check CHASSIS_PG_DSN / .env.baked for staleness.",
                        file=sys.stderr,
                    )
                    return 1
        finally:
            verify_conn.close()
    except Exception as verify_err:  # noqa: BLE001
        print(
            f"VERIFY-ERROR: post-commit verification crashed: {verify_err}. "
            f"Writer reported success ({conn_info}) but verification couldn't run.",
            file=sys.stderr,
        )
        return 1
    finally:
        conn.close()

    print(msg)
    if not args.quiet:
        webhook = env.get(HEALTH_WEBHOOK_ENV) or os.environ.get(HEALTH_WEBHOOK_ENV)
        if webhook:
            post_to_discord(webhook, msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
