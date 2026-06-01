#!/usr/bin/env python3
"""bfl-backfill-meal.py — manual meal insertion for the Discord Backfill trigger.

The BFL ingest pipeline is photo-driven. Sometimes the installer eats a
meal they don't photograph (canonical case: a standard 9am protein shake).
The Backfill trigger lets the installer post a plain-English line in the
health channel and have the chassis record it without a photo.

This script is the database-write half of that flow. The Discord-side
parser (the agent reading the trigger message in the health channel)
extracts structured fields from the message and invokes this script with
them. The script:

  1. Resolves today's `bfl_days` row (insert with `day_type=NULL` if
     missing — photo-ingested workout/meal pages fill `day_type` later
     when the page is photographed).
  2. Picks the next unused `meal_num` (1..6) for today.
  3. Inserts the `bfl_meals` row.
  4. Prints a single JSON line to stdout with the inserted values, so
     the caller can read it back and post the confirmation.

Routes through the chassis `_chassis_db` selector so the active backend
(Postgres by default per `USE_PG=true`) is honoured.

Usage:

    plugins/bfl/scripts/bfl-backfill-meal.py \\
        --description "two scrambled eggs and a slice of toast" \\
        --time-actual "7:30 AM" \\
        --protein-portions 2 \\
        --carb-portions 1

    # No time provided — leaves time_actual NULL
    plugins/bfl/scripts/bfl-backfill-meal.py \\
        --description "handful of almonds" \\
        --protein-portions 0.5

Output (single JSON line on stdout):

    {"date":"2026-05-05","meal_num":3,"time_actual":"7:30 AM","description":"...",
     "protein_portions":2.0,"carb_portions":1.0}

Failure modes:
  - Empty --description           -> non-zero exit, error on stderr
  - All 6 meals taken for today   -> non-zero exit ('day full')
  - DB connection error           -> non-zero exit, traceback

Timezone behaviour:
  --date defaults to the chassis-local date (`date.today()`). The chassis
  installer is expected to set the system timezone correctly; explicit
  --date <YYYY-MM-DD> overrides if needed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "scripts"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--description", required=True, help="Free-text meal description")
    p.add_argument(
        "--time-actual",
        default=None,
        help="Normalized time string, e.g. '9:00 AM' or '15:00'. Optional; null if absent.",
    )
    p.add_argument(
        "--protein-portions",
        type=float,
        default=None,
        help="Optional float, e.g. 1.0, 1.5, 2.0",
    )
    p.add_argument(
        "--carb-portions",
        type=float,
        default=None,
        help="Optional float",
    )
    p.add_argument(
        "--date",
        default=None,
        help="Override the target date (YYYY-MM-DD). Defaults to today (chassis-local).",
    )
    return p.parse_args()


def today_local() -> str:
    """Today as YYYY-MM-DD using the chassis machine's local timezone.

    The chassis installer is responsible for setting the system tz correctly.
    On macOS / most Linux distros, `date.today()` honours /etc/localtime,
    which is what the installer's day-boundary should track.
    """
    return dt.date.today().isoformat()


def main() -> int:
    args = parse_args()

    description = args.description.strip()
    if not description:
        print("error: --description must not be empty", file=sys.stderr)
        return 2

    target_date = args.date or today_local()

    # _chassis_db selects the backend (PG by default).
    from _chassis_db import connect, is_pg  # type: ignore

    conn = connect()
    try:
        cur = conn.cursor()

        # 1. Ensure today's bfl_days row exists.
        if is_pg(conn):
            cur.execute(
                "INSERT INTO bfl_days (date) VALUES (%s) ON CONFLICT (date) DO NOTHING",
                (target_date,),
            )
        else:
            # SQLite: created_at + updated_at need explicit unixepoch() since
            # the legacy chassis SQLite schema didn't always have a default
            # expression. Keep the SQLite fallback robust.
            cur.execute(
                "INSERT INTO bfl_days (date, created_at, updated_at) "
                "VALUES (?, unixepoch(), unixepoch()) "
                "ON CONFLICT(date) DO NOTHING",
                (target_date,),
            )

        # 2. Resolve day_id for the target date.
        ph = "%s" if is_pg(conn) else "?"
        cur.execute(
            f"SELECT id FROM bfl_days WHERE date = {ph}",
            (target_date,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"bfl_days row missing for {target_date} after upsert")
        day_id = row[0]

        # 3. Pick next free meal_num.
        cur.execute(
            f"SELECT meal_num FROM bfl_meals WHERE day_id = {ph} ORDER BY meal_num",
            (day_id,),
        )
        used = {r[0] for r in cur.fetchall()}
        meal_num: int | None = None
        for n in range(1, 7):
            if n not in used:
                meal_num = n
                break
        if meal_num is None:
            print(
                f"error: all 6 meal slots are already used for {target_date}",
                file=sys.stderr,
            )
            return 3

        # 4. Insert the meal row.
        if is_pg(conn):
            cur.execute(
                "INSERT INTO bfl_meals "
                "(day_id, meal_num, time_actual, description, protein_portions, "
                " carb_portions, photo_matched) "
                "VALUES (%s, %s, %s, %s, %s, %s, 0)",
                (
                    day_id,
                    meal_num,
                    args.time_actual,
                    description,
                    args.protein_portions,
                    args.carb_portions,
                ),
            )
        else:
            cur.execute(
                "INSERT INTO bfl_meals "
                "(day_id, meal_num, time_actual, description, protein_portions, "
                " carb_portions, photo_matched, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, unixepoch())",
                (
                    day_id,
                    meal_num,
                    args.time_actual,
                    description,
                    args.protein_portions,
                    args.carb_portions,
                ),
            )

        conn.commit()
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "date": target_date,
                "meal_num": meal_num,
                "time_actual": args.time_actual,
                "description": description,
                "protein_portions": args.protein_portions,
                "carb_portions": args.carb_portions,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
