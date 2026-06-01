#!/usr/bin/env python3
"""bfl-run-reconcile.py — correlate Strava run(s) with Oura heart-rate data.

Flow:
  1. Fetch Strava activities in the time window (default: last 24h).
  2. For each Run activity, query Oura /v2/usercollection/heartrate in the
     same window.
  3. Re-sample HR to 1-min buckets.
  4. Compute 0-10 intensity per minute (HR-reserve / Karvonen-style).
  5. UPSERT into bfl_runs.

The user must explicitly start **Workout Heart Rate** mode on the Oura
ring before the run, otherwise /heartrate returns only 5-min-interval
samples which won't resolve a typical 4-peak intensity profile.

Usage:
    plugins/bfl/scripts/bfl-run-reconcile.py                     # last 24h
    plugins/bfl/scripts/bfl-run-reconcile.py --after 2026-04-21  # activities since date
    plugins/bfl/scripts/bfl-run-reconcile.py --activity-id 12345 # single activity
    plugins/bfl/scripts/bfl-run-reconcile.py --dry-run           # print, don't write

Env vars:
    STRAVA_CLIENT_ID
    STRAVA_CLIENT_SECRET
    STRAVA_ACCESS_TOKEN
    STRAVA_REFRESH_TOKEN
    STRAVA_TOKEN_EXPIRES_AT
    OURA_TOKEN

HR_MAX_DEFAULT and HR_REST_DEFAULT are configurable defaults for the
Karvonen-style intensity calculation. Override per-call with --hr-max /
--hr-rest, or per-installer in chassis.config.yaml modules.bfl.hr_max /
hr_rest (the heartbeat that calls this script should pass them through).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "scripts"))
import _chassis_db  # type: ignore

STRAVA_API = "https://www.strava.com/api/v3"
OURA_API = "https://api.ouraring.com/v2"

# HR-to-intensity mapping (Karvonen-ish). Tunable per athlete via CLI flags
# or chassis.config.yaml modules.bfl.{hr_max,hr_rest}.
HR_MAX_DEFAULT = 190
HR_REST_DEFAULT = 55


def load_env() -> dict[str, str]:
    keys = (
        "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
        "STRAVA_ACCESS_TOKEN", "STRAVA_REFRESH_TOKEN", "STRAVA_TOKEN_EXPIRES_AT",
        "OURA_TOKEN",
    )
    return {k: os.environ.get(k, "") for k in keys}


# ---- Strava ----------------------------------------------------------------

def strava_refresh_if_needed(env: dict) -> str:
    now = int(time.time())
    exp = int(env.get("STRAVA_TOKEN_EXPIRES_AT", "0") or "0")
    if exp - now > 300:
        return env["STRAVA_ACCESS_TOKEN"]
    data = urllib.parse.urlencode({
        "client_id": env["STRAVA_CLIENT_ID"],
        "client_secret": env["STRAVA_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": env["STRAVA_REFRESH_TOKEN"],
    }).encode()
    req = urllib.request.Request("https://www.strava.com/oauth/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read().decode())
    # In-process update only. Persistence to canonical secret store is the
    # installer's responsibility (see strava-ingest.py _persist_token_updates).
    for k, v in (
        ("STRAVA_ACCESS_TOKEN", body["access_token"]),
        ("STRAVA_REFRESH_TOKEN", body["refresh_token"]),
        ("STRAVA_TOKEN_EXPIRES_AT", str(body["expires_at"])),
    ):
        os.environ[k] = v
        env[k] = v
    return body["access_token"]


def strava_list_activities(access_token: str, after_epoch: int) -> list[dict]:
    url = f"{STRAVA_API}/athlete/activities?after={after_epoch}&per_page=30"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def strava_get_activity(access_token: str, activity_id: int) -> dict:
    url = f"{STRAVA_API}/activities/{activity_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


# ---- Oura ------------------------------------------------------------------

def oura_heartrate(token: str, start_iso: str, end_iso: str) -> list[dict]:
    """GET /v2/usercollection/heartrate?start_datetime=...&end_datetime=...
    Returns list of {timestamp, bpm, source} samples.
    """
    qs = urllib.parse.urlencode({"start_datetime": start_iso, "end_datetime": end_iso})
    url = f"{OURA_API}/usercollection/heartrate?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read().decode())
    return body.get("data", [])


# ---- Bucketing + intensity -------------------------------------------------

def bucket_1min(start_ts: int, end_ts: int, samples: list[dict]) -> list[float | None]:
    n_min = max(1, (end_ts - start_ts + 59) // 60)
    buckets: list[list[float]] = [[] for _ in range(n_min)]
    for s in samples:
        ts_iso = s.get("timestamp")
        bpm = s.get("bpm")
        if not ts_iso or bpm is None:
            continue
        try:
            ts = int(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        if ts < start_ts or ts >= end_ts:
            continue
        idx = (ts - start_ts) // 60
        if 0 <= idx < n_min:
            buckets[idx].append(float(bpm))
    return [round(sum(b) / len(b), 1) if b else None for b in buckets]


def intensity_0_10(hr: float | None, hr_max: int = HR_MAX_DEFAULT, hr_rest: int = HR_REST_DEFAULT) -> int | None:
    if hr is None:
        return None
    pct = (hr - hr_rest) / max(1, hr_max - hr_rest)
    return max(0, min(10, round(pct * 10)))


# ---- DB upsert --------------------------------------------------------------

def upsert_run(db, cur, row: dict) -> int:
    cols = ",".join(row.keys())
    placeholders = ",".join("?" for _ in row)
    updates = ",".join(f"{k}=excluded.{k}" for k in row.keys() if k != "strava_activity_id")
    sql = f"""INSERT INTO bfl_runs ({cols}) VALUES ({placeholders})
              ON CONFLICT(strava_activity_id) DO UPDATE SET {updates}
              RETURNING id"""
    cur.execute(sql, list(row.values()))
    rid = cur.fetchone()[0]
    db.commit()
    return rid


def promote_to_aerobic(db, cur, run_id: int) -> None:
    """Mirror the just-ingested bfl_runs row into bfl_aerobic so the BFL
    dashboard Workout column renders a proper aerobic entry on Strava-only
    days (days without a notebook photo upload).

    Also flips the parent bfl_days.day_type to 'aerobic' when it's still
    NULL. If a workout-log photo later classifies the day as 'upper' or
    'lower', the OCR path will overwrite day_type — promotion is only
    load-bearing when the notebook hasn't arrived yet.

    Idempotent: re-running on an existing (day_id, strava_activity_id) is a
    no-op.
    """
    run = cur.execute(
        "SELECT day_id, start_time, end_time, elapsed_seconds, "
        "strava_activity_id, notes, hr_1min_buckets_json FROM bfl_runs "
        "WHERE id = ?",
        (run_id,),
    ).fetchone()
    if not run or not run[0]:
        return
    day_id, start_ts, end_ts, elapsed, strava_id, notes, hr_buckets = run
    total_min = round(elapsed / 60) if elapsed else None

    day_row = cur.execute(
        "SELECT day_type FROM bfl_days WHERE id = ?", (day_id,)
    ).fetchone()
    if day_row and (day_row[0] is None or day_row[0] == ""):
        cur.execute(
            "UPDATE bfl_days SET day_type = ? WHERE id = ?",
            ("aerobic", day_id),
        )

    existing = cur.execute(
        "SELECT id FROM bfl_aerobic "
        "WHERE day_id = ? AND strava_activity_id = ?",
        (day_id, strava_id),
    ).fetchone()
    if existing:
        if hr_buckets:
            cur.execute(
                "UPDATE bfl_aerobic SET hr_1min_buckets_json = ?, "
                "total_minutes = ?, start_time = ?, end_time = ? WHERE id = ?",
                (hr_buckets, total_min, start_ts, end_ts, existing[0]),
            )
    else:
        cur.execute(
            "INSERT INTO bfl_aerobic "
            "(day_id, activity_type, start_time, end_time, total_minutes, "
            "strava_activity_id, notes, hr_1min_buckets_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (day_id, "run", start_ts, end_ts, total_min, strava_id, notes, hr_buckets),
        )
    db.commit()


# ---- Main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", type=str, default=None, help="Only activities after this date (YYYY-MM-DD). Default: last 24h.")
    ap.add_argument("--activity-id", type=int, default=None, help="Reconcile a single Strava activity by ID.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hr-max", type=int, default=HR_MAX_DEFAULT)
    ap.add_argument("--hr-rest", type=int, default=HR_REST_DEFAULT)
    args = ap.parse_args()

    env = load_env()
    strava_token = strava_refresh_if_needed(env)
    oura_token = env.get("OURA_TOKEN")
    if not oura_token:
        print("ERROR: OURA_TOKEN missing from env", file=sys.stderr)
        return 2

    if args.activity_id:
        activities = [strava_get_activity(strava_token, args.activity_id)]
    else:
        if args.after:
            after_epoch = int(datetime.fromisoformat(args.after).replace(tzinfo=timezone.utc).timestamp())
        else:
            after_epoch = int(time.time()) - 86400
        activities = strava_list_activities(strava_token, after_epoch)

    # Include Strava-labelled Ride activities that look like mislabeled runs
    # (wearable auto-detect misfires). Mirror the strava-ingest pace heuristic.
    def _looks_like_run(a: dict) -> bool:
        t = a.get("type", "").lower()
        if t in ("run", "trailrun", "virtualrun"):
            return True
        if t == "ride":
            dist = a.get("distance") or 0
            elapsed = a.get("elapsed_time") or 0
            if dist > 0 and elapsed > 0:
                pace = elapsed / (dist / 1000)
                return pace > 180
        return False

    runs = [a for a in activities if _looks_like_run(a)]
    print(f"Found {len(runs)} run activit{'y' if len(runs) == 1 else 'ies'}")

    if not runs:
        return 0

    db = _chassis_db.connect()
    cur = _chassis_db.cursor(db)

    for a in runs:
        aid = a["id"]
        start_iso = a["start_date"]
        start_ts = int(datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp())
        elapsed = int(a.get("elapsed_time", 0))
        end_ts = start_ts + elapsed
        end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")

        print(f"\nActivity {aid}: {a.get('name', '?')} - {start_iso} - {elapsed}s - {a.get('distance', 0)}m")
        print(f"  Fetching Oura HR for {start_iso}..{end_iso}")
        try:
            samples = oura_heartrate(oura_token, start_iso, end_iso)
        except Exception as e:
            print(f"  OURA ERROR: {e}")
            samples = []

        buckets = bucket_1min(start_ts, end_ts, samples)
        intensity = [intensity_0_10(b, args.hr_max, args.hr_rest) for b in buckets]
        covered = sum(1 for b in buckets if b is not None)
        status = "ok" if covered >= max(1, len(buckets) * 0.5) else ("coarse" if samples else "missing")

        print(f"  HR samples: {len(samples)} | 1-min buckets filled: {covered}/{len(buckets)} | status={status}")
        if buckets:
            print(f"  HR (min-by-min): {buckets[:22]}")
            print(f"  Intensity 0-10:  {intensity[:22]}")

        row = {
            "strava_activity_id": str(aid),
            "start_time": start_ts,
            "end_time": end_ts,
            "elapsed_seconds": elapsed,
            "distance_m": a.get("distance"),
            "avg_pace_seconds_per_km": (elapsed / (a.get("distance", 0) / 1000)) if a.get("distance") else None,
            "avg_hr": a.get("average_heartrate"),
            "max_hr": a.get("max_heartrate"),
            "hr_samples_json": json.dumps(samples) if samples else None,
            "hr_1min_buckets_json": json.dumps(buckets),
            "intensity_1min_json": json.dumps(intensity),
            "oura_pull_status": status,
        }

        if args.dry_run:
            print(f"  DRY-RUN would upsert bfl_runs row for activity {aid}")
        else:
            rid = upsert_run(db, cur, row)
            print(f"  Upserted bfl_runs id={rid}")
            promote_to_aerobic(db, cur, rid)

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
