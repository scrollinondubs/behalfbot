#!/usr/bin/env python3
"""strava-ingest.py — poll Strava for new activities since last-seen and ingest.

Lives between Strava's public API and `bfl_runs`. This is the Strava-first
layer of the BFL pipeline: activities get ingested on a heartbeat immediately
when they show up in Strava, so even if the BFL notebook photo journal is
late or lossy the run still lands in the DB.

Layers (least → most enriched):

  1. THIS SCRIPT — basic row with strava_activity_id, time, distance, pace, HR
     from the Strava list endpoint.
  2. `bfl-ingest.py` — notebook photo enriches the same row with planned /
     actual intensities and anecdotal profile. Match by same-day-same-type.
  3. `bfl-run-reconcile.py` — manual/deeper enrichment: per-minute HR buckets
     from Oura and the intensity 0-10 profile.

Flow:
  - Read state file (path under chassis state dir) for `last_seen_epoch`.
  - Refresh Strava access token if expired.
  - GET /athlete/activities?after=<last_seen>.
  - For each activity: UPSERT into bfl_runs (via strava_activity_id UNIQUE).
  - Post summary to the health channel webhook.
  - Update state file to max(start_ts) of seen activities.

Env vars:
    STRAVA_CLIENT_ID
    STRAVA_CLIENT_SECRET
    STRAVA_ACCESS_TOKEN
    STRAVA_REFRESH_TOKEN
    STRAVA_TOKEN_EXPIRES_AT      epoch seconds; auto-rotated on refresh
    HEALTH_WEBHOOK_URL           Discord webhook for activity summaries
    OPS_WEBHOOK_URL              optional; alerted after FAILURE_STREAK_THRESHOLD
                                  consecutive ticks with insert failures
    BEHALFBOT_STATE_DIR          state file location; default ~/behalfbot/state

Exit codes: 0 on success (even with 0 new activities). Nonzero on API or DB error.

Note on token storage: this plugin treats Strava OAuth tokens as plain env
vars sourced from whatever secret store the chassis configures. The V1
reference rotated them via Vaultwarden + a custom helper; the chassis
ships a generic `update_env_value()` that just rewrites a .env-style
file. Installers backed by Vaultwarden / 1Password / Doppler should
override `_persist_token_updates` in their bootstrap if they want the
rotation to round-trip through the canonical store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "scripts"))
from _chassis_db import connect as _chassis_db_connect, cursor as _chassis_db_cursor  # noqa: E402

# How many consecutive ticks with ≥1 insert failure before escalating to ops
FAILURE_STREAK_THRESHOLD = 3

STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_OAUTH = "https://www.strava.com/oauth/token"


def _state_dir() -> Path:
    explicit = os.environ.get("BEHALFBOT_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / "behalfbot" / "state"


def _state_path() -> Path:
    return _state_dir() / "strava-state.json"


# --- Env handling ------------------------------------------------------------

def load_env() -> dict[str, str]:
    """Snapshot relevant env vars. The chassis bootstrap is responsible for
    sourcing them from the canonical secret store (Vaultwarden, Doppler,
    1Password, plain .env, etc.) before invoking this script.
    """
    keys = (
        "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
        "STRAVA_ACCESS_TOKEN", "STRAVA_REFRESH_TOKEN", "STRAVA_TOKEN_EXPIRES_AT",
        "HEALTH_WEBHOOK_URL", "OPS_WEBHOOK_URL",
    )
    return {k: os.environ.get(k, "") for k in keys}


def _persist_token_updates(updates: dict[str, str]) -> None:
    """Persist rotated Strava tokens.

    Default implementation is a no-op + warning — the chassis V1 bootstrap
    ships generic os.environ-only token handling. In-process env values are
    updated by the caller. To round-trip rotated tokens to the canonical
    secret store, the installer's bootstrap should override this function
    (e.g. write to Vaultwarden via `bw edit`, Doppler via `doppler secrets
    set`, etc.).

    The previous V1 implementation hard-coded Vaultwarden item IDs and
    shelled out to `bw edit item`. That coupling is not portable. Until
    the chassis grows a generic secret-store abstraction, tokens rotate
    in-process only — when the script exits, the rotated tokens are lost
    and the next invocation must re-refresh.
    """
    sys.stderr.write(
        "WARN: Strava token rotation persisted in-memory only. "
        "Override _persist_token_updates() in your chassis bootstrap to "
        "round-trip rotated tokens to your secret store.\n"
    )


# --- Strava ------------------------------------------------------------------

def strava_refresh_if_needed(env: dict[str, str]) -> str:
    expires = int(env.get("STRAVA_TOKEN_EXPIRES_AT", "0") or "0")
    # Refresh if expired or within 5 min of expiry
    if expires and expires - 300 > time.time():
        return env["STRAVA_ACCESS_TOKEN"]
    data = urllib.parse.urlencode({
        "client_id": env["STRAVA_CLIENT_ID"],
        "client_secret": env["STRAVA_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": env["STRAVA_REFRESH_TOKEN"],
    }).encode()
    req = urllib.request.Request(STRAVA_OAUTH, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read().decode())
    rotations = {
        "STRAVA_ACCESS_TOKEN": body["access_token"],
        "STRAVA_REFRESH_TOKEN": body["refresh_token"],
        "STRAVA_TOKEN_EXPIRES_AT": str(body["expires_at"]),
    }
    # Update in-process env so this run uses the new token
    for k, v in rotations.items():
        os.environ[k] = v
        env[k] = v
    # Optionally round-trip to canonical store (no-op by default)
    _persist_token_updates(rotations)
    return body["access_token"]


def strava_list_activities(token: str, after_epoch: int) -> list[dict]:
    activities: list[dict] = []
    page = 1
    while True:
        url = f"{STRAVA_API}/athlete/activities?after={after_epoch}&per_page=50&page={page}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            batch = json.loads(r.read().decode())
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 50:
            break
        page += 1
        if page > 10:  # hard cap — 500 activities in a single poll is unlikely
            break
    return activities


# --- State -------------------------------------------------------------------

def read_state() -> dict:
    p = _state_path()
    if p.exists():
        return json.loads(p.read_text())
    return {"last_seen_epoch": int(time.time()) - 86400}


def write_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


def _backend_suffix() -> str:
    """Return 'pg' or 'sqlite' depending on active backend (mirrors _chassis_db logic)."""
    return "pg" if os.environ.get("USE_PG", "true").lower() in ("1", "true", "yes") else "sqlite"


def get_after_epoch(state: dict, since_override: str | None) -> int:
    if since_override:
        if since_override.isdigit():
            return int(since_override)
        return int(datetime.fromisoformat(since_override).replace(tzinfo=timezone.utc).timestamp())
    scoped_key = f"last_seen_epoch_{_backend_suffix()}"
    return state.get(scoped_key) or state.get("last_seen_epoch") or int(time.time()) - 86400


def _epoch_state_key() -> str:
    return f"last_seen_epoch_{_backend_suffix()}"


def _failure_streak_key() -> str:
    return f"failure_streak_{_backend_suffix()}"


# --- DB upsert ---------------------------------------------------------------

def ensure_day_id(db, start_ts: int) -> int | None:
    """Map activity date (UTC) to bfl_days.id, creating if absent."""
    cur = _chassis_db_cursor(db)
    day_str = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    cur.execute("SELECT id FROM bfl_days WHERE date = ?", (day_str,))
    row = cur.fetchone()
    if row:
        return row[0]
    try:
        cur.execute("INSERT INTO bfl_days(date, day_type) VALUES(?, 'aerobic') RETURNING id", (day_str,))
        new_id = cur.fetchone()[0]
        db.commit()
        return new_id
    except Exception:
        db.rollback()
        cur.execute("SELECT id FROM bfl_days WHERE date = ?", (day_str,))
        row = cur.fetchone()
        return row[0] if row else None


def upsert_run(db, row: dict) -> tuple[int, bool]:
    """Return (id, was_new). Uses strava_activity_id UNIQUE to dedupe."""
    cur = _chassis_db_cursor(db)
    cur.execute(
        "SELECT id FROM bfl_runs WHERE strava_activity_id = ?",
        (row["strava_activity_id"],),
    )
    existing = cur.fetchone()
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "strava_activity_id")
    cur.execute(
        f"INSERT INTO bfl_runs({','.join(cols)}) VALUES({placeholders}) "
        f"ON CONFLICT(strava_activity_id) DO UPDATE SET {updates} "
        f"RETURNING id",
        [row[c] for c in cols],
    )
    rid = cur.fetchone()[0]
    db.commit()
    return rid, existing is None


# --- Activity → row mapping --------------------------------------------------

def classify_activity(a: dict) -> tuple[str, str]:
    """Return (normalized_type, notes_string).

    Heuristic: if Strava reports `Ride` but the pace is slower than 3:00/km
    (180 s/km), override the normalized type to `Run` (pro tour riders
    climb at ~3:00/km on Alpe d'Huez; anything slower is not a real ride).
    Strava's original label kept in notes for audit.
    """
    raw_type = a.get("type", "Activity")
    raw_name = a.get("name", "")
    distance = a.get("distance") or 0
    elapsed = a.get("elapsed_time") or 0
    pace_s_per_km = (elapsed / (distance / 1000)) if distance and distance > 0 else None

    normalized = raw_type
    if raw_type == "Ride" and pace_s_per_km and pace_s_per_km > 180:
        normalized = "Run"
        notes = (
            f"Run (reclassified from Strava 'Ride' by pace heuristic — "
            f"{pace_s_per_km:.0f}s/km is too slow for cycling): {raw_name}"
        ).strip()
    else:
        notes = f"{raw_type}: {raw_name}".strip()
    return normalized, notes


def activity_to_row(a: dict, db) -> dict:
    start_iso = a["start_date"]
    start_ts = int(datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp())
    elapsed = int(a.get("elapsed_time", 0))
    end_ts = start_ts + elapsed
    distance = a.get("distance")
    _, notes = classify_activity(a)
    return {
        "strava_activity_id": str(a["id"]),
        "day_id": ensure_day_id(db, start_ts),
        "start_time": start_ts,
        "end_time": end_ts,
        "elapsed_seconds": elapsed,
        "distance_m": distance,
        "avg_pace_seconds_per_km": (elapsed / (distance / 1000)) if distance else None,
        "avg_hr": a.get("average_heartrate"),
        "max_hr": a.get("max_heartrate"),
        "notes": notes,
    }


# --- Discord summary ---------------------------------------------------------

def format_pace(sec_per_km: float | None) -> str:
    if not sec_per_km:
        return "-"
    m = int(sec_per_km // 60)
    s = int(sec_per_km % 60)
    return f"{m}:{s:02d}/km"


def format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_distance(meters: float | None) -> str:
    if not meters:
        return "-"
    km = meters / 1000
    return f"{km:.2f} km" if km >= 1 else f"{int(meters)} m"


def activity_summary(a: dict) -> str:
    act_type = a.get("type", "Activity")
    name = a.get("name", "")
    elapsed = int(a.get("elapsed_time", 0))
    distance = a.get("distance")
    pace = (elapsed / (distance / 1000)) if distance else None
    avg_hr = a.get("average_heartrate")
    parts = [
        f"**{act_type}**" + (f" - *{name}*" if name else ""),
        f"Distance: {format_distance(distance)}",
        f"Pace: {format_pace(pace)}",
        f"Duration: {format_duration(elapsed)}",
        f"Avg HR: {int(avg_hr)} bpm" if avg_hr else "Avg HR: -",
    ]
    return "\n".join(parts)


def _discord_post(webhook_url: str, content: str) -> None:
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps({"content": content}).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "behalfbot-strava-ingest/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


# --- Main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Fetch + print, don't write DB or Discord.")
    ap.add_argument("--since", help="Override last_seen_epoch (ISO date or epoch seconds).")
    args = ap.parse_args()

    env = load_env()
    if not env.get("STRAVA_REFRESH_TOKEN"):
        print("ERROR: STRAVA_REFRESH_TOKEN missing from env", file=sys.stderr)
        return 5
    if not env.get("STRAVA_CLIENT_SECRET"):
        print("ERROR: STRAVA_CLIENT_SECRET missing from env", file=sys.stderr)
        return 5

    webhook_health = env.get("HEALTH_WEBHOOK_URL")
    webhook_ops = env.get("OPS_WEBHOOK_URL")
    if not webhook_health and not args.dry_run:
        print("ERROR: HEALTH_WEBHOOK_URL missing from env", file=sys.stderr)
        return 2

    state = read_state()
    after_epoch = get_after_epoch(state, args.since)

    try:
        token = strava_refresh_if_needed(env)
    except Exception as e:
        print(f"ERROR: token refresh failed: {e}", file=sys.stderr)
        return 3

    try:
        activities = strava_list_activities(token, after_epoch)
    except Exception as e:
        print(f"ERROR: strava list failed: {e}", file=sys.stderr)
        return 4

    # Filter out wearable-generated pseudo-activities that mirror step-count
    # goals into Strava as tiny / zero-distance "Run" events. Any real walk /
    # run / ride has meaningful distance.
    def _is_real(a: dict) -> bool:
        dist = a.get("distance")
        if dist is None or dist <= 0:
            return False
        if dist <= 100:
            return False
        if (a.get("elapsed_time") or 0) <= 60:
            return False
        return True

    real_activities = [a for a in activities if _is_real(a)]
    skipped = len(activities) - len(real_activities)
    if skipped:
        print(f"Filtered {skipped} zero-distance/zero-duration activities (wearable mirrors etc.)")
    activities = real_activities

    if not activities:
        print(f"No new activities since {datetime.fromtimestamp(after_epoch, tz=timezone.utc).isoformat()}")
        return 0

    db = _chassis_db_connect()

    last_ok_ts = after_epoch
    ingested_new: list[dict] = []
    failed_strava_ids: list[str] = []

    for a in activities:
        row = activity_to_row(a, db)
        if args.dry_run:
            print(f"DRY-RUN would upsert: {a['id']} ({a.get('type')}) {a.get('name')}")
            continue
        try:
            rid, was_new = upsert_run(db, row)
            last_ok_ts = max(last_ok_ts, row["start_time"])
            if was_new:
                ingested_new.append(a)
                print(f"Ingested new bfl_runs id={rid} strava_activity_id={a['id']}")
            else:
                print(f"Updated existing bfl_runs id={rid} strava_activity_id={a['id']}")
        except Exception as e:
            failed_strava_ids.append(str(a["id"]))
            print(
                f"WARN: insert failed for strava_activity_id={a['id']}: {e}",
                file=sys.stderr,
            )

    db.close()

    if not args.dry_run and ingested_new:
        for a in ingested_new:
            try:
                _discord_post(webhook_health, activity_summary(a))
            except Exception as e:
                print(f"WARN: Discord post failed for {a['id']}: {e}", file=sys.stderr)

    if not args.dry_run:
        epoch_key = _epoch_state_key()
        failure_key = _failure_streak_key()

        state[epoch_key] = last_ok_ts
        state["last_run_at"] = int(time.time())

        if failed_strava_ids:
            streak = state.get(failure_key, 0) + 1
            state[failure_key] = streak
            if streak >= FAILURE_STREAK_THRESHOLD and webhook_ops:
                backend = _backend_suffix().upper()
                try:
                    _discord_post(
                        webhook_ops,
                        f"WARNING strava-ingest: {len(failed_strava_ids)} insert(s) failed "
                        f"for {streak} consecutive tick(s) on {backend} backend. "
                        f"Failed IDs: {', '.join(failed_strava_ids)}. "
                        f"State NOT advanced for failed rows — next tick will retry.",
                    )
                except Exception as e:
                    print(f"WARN: ops Discord alert failed: {e}", file=sys.stderr)
        else:
            state[failure_key] = 0

        write_state(state)

    print(
        f"Done. {len(ingested_new)} new, "
        f"{len(activities) - len(ingested_new) - len(failed_strava_ids)} updates, "
        f"{len(failed_strava_ids)} failed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
