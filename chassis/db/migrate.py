"""Chassis-core migration runner.

Applies `chassis/db/migrations/*.sql` in lexical filename order and records
each in a ledger table so re-running is a no-op. Deliberately the smallest
thing that is safe:

  - No down-migrations. Rolling back a chassis release rolls back code, not
    schema; every migration here must be additive or the release is wrong.
  - No checksums. A migration file that changes after being applied is an
    operator error the ledger cannot fix, and pretending otherwise invites
    "just delete the ledger row" as a workaround.
  - A session-level advisory lock so two containers booting at once cannot
    both apply 001. The lock id is an arbitrary fixed constant, namespaced to
    this runner.

Ordering note: files sort lexically, which is why they are numbered with a
fixed-width prefix (001_, 002_). A file named `10_foo.sql` would sort before
`2_foo.sql` and apply out of order; the runner rejects filenames that do not
match the expected prefix rather than silently mis-ordering them.

Usage:
    python3 -m chassis.db.migrate            # apply pending, print what ran
    python3 -m chassis.db.migrate --dry-run  # list pending, touch nothing
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from chassis.db.connection import ChassisDBUnavailable, connect

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Arbitrary but fixed. Two chassis containers booting simultaneously serialize
# on this rather than racing to CREATE TABLE.
ADVISORY_LOCK_ID = 8_140_251_193

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS chassis_schema_migrations (
    name        TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

MIGRATION_NAME_RE = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")


def discover_migrations(migrations_dir: Path | None = None) -> list[Path]:
    """Return migration files in apply order, rejecting mis-numbered names."""
    directory = migrations_dir or MIGRATIONS_DIR
    if not directory.is_dir():
        return []
    files = sorted(p for p in directory.iterdir() if p.suffix == ".sql")
    bad = [p.name for p in files if not MIGRATION_NAME_RE.match(p.name)]
    if bad:
        raise ValueError(
            "Migration filenames must match NNN_lower_snake.sql so lexical sort "
            f"equals apply order. Offending: {', '.join(bad)}"
        )
    return files


def applied_migrations(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute(LEDGER_DDL)
    cur.execute("SELECT name FROM chassis_schema_migrations")
    return {row[0] for row in cur.fetchall()}


def apply_migrations(conn=None, migrations_dir: Path | None = None, *, dry_run: bool = False) -> list[str]:
    """Apply pending migrations. Returns the names applied (or pending, if dry_run).

    Passing `conn` is how the tests drive this without a live database; the
    caller owns the connection's lifetime in that case.
    """
    owns_connection = conn is None
    if owns_connection:
        conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
        try:
            done = applied_migrations(conn)
            pending = [p for p in discover_migrations(migrations_dir) if p.name not in done]
            if dry_run:
                return [p.name for p in pending]
            ran: list[str] = []
            for path in pending:
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute(
                    "INSERT INTO chassis_schema_migrations (name) VALUES (%s)",
                    (path.name,),
                )
                conn.commit()
                ran.append(path.name)
            return ran
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
            conn.commit()
    finally:
        if owns_connection:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply chassis-core Postgres migrations.")
    parser.add_argument("--dry-run", action="store_true", help="List pending migrations without applying them.")
    args = parser.parse_args(argv)

    try:
        names = apply_migrations(dry_run=args.dry_run)
    except ChassisDBUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not names:
        print("chassis migrations: up to date")
        return 0
    verb = "pending" if args.dry_run else "applied"
    print(f"chassis migrations {verb}: {', '.join(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
