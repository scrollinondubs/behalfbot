#!/usr/bin/env python3
"""Apply the dating V2 state schema (dating_directives + dating_clearances tables).

Ported from <v1-reference-install> PR #522. Chassis adaptation: uses BEHALFBOT_PG_DSN; no
seeding from legacy files (no pending-instructions.md or cleared-matches.json
exists in a fresh chassis install). Installers migrating from a file-backed
V1 chassis install should seed manually or use the <v1-reference-install> source migration as
reference.

Run once per install:
    python3 plugins/dating/scripts/dating-state-migrate.py

Idempotent: uses IF NOT EXISTS for all DDL.
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
MIGRATION_SQL = PLUGIN_DIR / "db" / "migrations" / "001_dating_state.sql"

sys.path.insert(0, str(PLUGIN_DIR / "scripts"))
from _chassis_db import connect, get_pg_dsn  # noqa: E402


def main() -> int:
    print(f"Connecting to Postgres ({get_pg_dsn()[:40]}...)")
    conn = connect()
    cur = conn.cursor()

    sql = MIGRATION_SQL.read_text()
    print(f"Applying {MIGRATION_SQL.name} ...")

    # Execute as a multi-statement block (psycopg executes the full string
    # including the BEGIN/COMMIT from the SQL file).
    for statement in [s.strip() for s in sql.split(";") if s.strip()]:
        if statement.upper() in ("BEGIN", "COMMIT"):
            continue
        cur.execute(statement)

    conn.commit()
    conn.close()
    print("Migration complete: dating_directives and dating_clearances tables ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
