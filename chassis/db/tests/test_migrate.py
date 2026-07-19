#!/usr/bin/env python3
"""test_migrate.py - the migration runner, and the shipped migration's shape.

Two things get asserted and they fail in different ways:

  1. The runner is idempotent, ordered, and locked. A second boot must not
     re-apply 001, and two containers starting together must not race.
  2. The shipped 001_pacman_queue.sql actually declares the columns the queue
     code writes to. Config promising more than code delivers is the recurring
     failure in this codebase; a schema that has drifted from the INSERT
     statements is the same failure wearing a different hat.

Run:
    python3 -m pytest chassis/db/tests/test_migrate.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from chassis.db.migrate import (  # noqa: E402
    ADVISORY_LOCK_ID,
    MIGRATIONS_DIR,
    apply_migrations,
    discover_migrations,
)


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        self._conn.executed.append((" ".join(sql.split()), params))
        return self

    def fetchall(self):
        return [(name,) for name in self._conn.already_applied]


class FakeConnection:
    def __init__(self, already_applied: set[str] | None = None):
        self.executed: list[tuple[str, object]] = []
        self.commits = 0
        self.already_applied = already_applied or set()

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    @property
    def sql_log(self) -> list[str]:
        return [sql for sql, _ in self.executed]


class TestDiscovery(unittest.TestCase):
    def test_finds_the_pacman_migration(self):
        names = [p.name for p in discover_migrations()]
        self.assertIn("001_pacman_queue.sql", names)

    def test_returns_files_in_lexical_order(self):
        names = [p.name for p in discover_migrations()]
        self.assertEqual(names, sorted(names))

    def test_rejects_filenames_that_would_sort_out_of_apply_order(self):
        """`10_x.sql` sorts before `2_x.sql`. Reject rather than mis-order."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "2_thing.sql").write_text("SELECT 1")
            (directory / "10_thing.sql").write_text("SELECT 1")
            with self.assertRaises(ValueError):
                discover_migrations(directory)

    def test_ignores_non_sql_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "001_ok.sql").write_text("SELECT 1")
            (directory / "README.md").write_text("notes")
            self.assertEqual([p.name for p in discover_migrations(directory)], ["001_ok.sql"])

    def test_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(discover_migrations(Path("/nonexistent/migrations")), [])


class TestApply(unittest.TestCase):
    def test_applies_pending_migrations(self):
        conn = FakeConnection()
        self.assertEqual(apply_migrations(conn), ["001_pacman_queue.sql"])

    def test_is_idempotent(self):
        """Migrations run on every container boot. The second boot is a no-op."""
        conn = FakeConnection(already_applied={"001_pacman_queue.sql"})
        self.assertEqual(apply_migrations(conn), [])

    def test_creates_the_ledger_table_before_reading_it(self):
        conn = FakeConnection()
        apply_migrations(conn)
        ledger_index = next(i for i, s in enumerate(conn.sql_log) if "CREATE TABLE IF NOT EXISTS chassis_schema_migrations" in s)
        select_index = next(i for i, s in enumerate(conn.sql_log) if "SELECT name FROM chassis_schema_migrations" in s)
        self.assertLess(ledger_index, select_index)

    def test_records_each_applied_migration_in_the_ledger(self):
        conn = FakeConnection()
        apply_migrations(conn)
        inserts = [params for sql, params in conn.executed if "INSERT INTO chassis_schema_migrations" in sql]
        self.assertEqual(inserts, [("001_pacman_queue.sql",)])

    def test_takes_and_releases_the_advisory_lock(self):
        """Two containers booting together serialize instead of racing."""
        conn = FakeConnection()
        apply_migrations(conn)
        self.assertIn(("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_ID,)), conn.executed)
        self.assertIn(("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,)), conn.executed)

    def test_releases_the_lock_even_when_a_migration_raises(self):
        class Exploding(FakeConnection):
            def cursor(self):
                outer = self

                class Cur(FakeCursor):
                    def execute(self, sql, params=()):
                        outer.executed.append((" ".join(sql.split()), params))
                        if "CREATE TABLE IF NOT EXISTS chassis_pacman_queue" in sql:
                            raise RuntimeError("boom")
                        return self

                return Cur(self)

        conn = Exploding()
        with self.assertRaises(RuntimeError):
            apply_migrations(conn)
        self.assertIn(("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,)), conn.executed)

    def test_dry_run_lists_without_applying(self):
        conn = FakeConnection()
        self.assertEqual(apply_migrations(conn, dry_run=True), ["001_pacman_queue.sql"])
        self.assertFalse(any("CREATE TABLE IF NOT EXISTS chassis_pacman_queue" in s for s in conn.sql_log))
        self.assertFalse(any("INSERT INTO chassis_schema_migrations" in s for s in conn.sql_log))


class TestPacmanQueueSchema(unittest.TestCase):
    """The shipped DDL must declare what chassis/pacman/queue.py writes."""

    @classmethod
    def setUpClass(cls):
        cls.sql = (MIGRATIONS_DIR / "001_pacman_queue.sql").read_text(encoding="utf-8")

    def test_declares_every_column_the_queue_code_uses(self):
        for column in (
            "token",
            "url",
            "source",
            "source_ref",
            "entry_group",
            "created_at",
            "claimed_at",
            "processed_at",
            "verdict",
            "gate",
            "proposal_doc_id",
            "legacy_block_id",
        ):
            self.assertIn(column, self.sql, column)

    def test_token_is_unique(self):
        """The approval token is a handle a human types. Two rows cannot share one."""
        self.assertRegex(self.sql, r"token\s+TEXT\s+NOT NULL UNIQUE")

    def test_pending_rows_are_indexed(self):
        """The gather script runs this predicate every dispatcher tick."""
        self.assertIn("ix_pacman_queue_pending", self.sql)
        self.assertIn("WHERE processed_at IS NULL", self.sql)

    def test_migration_idempotency_key_is_block_plus_url(self):
        """A block holding two URLs becomes two rows, so the block alone is not a key."""
        self.assertIn("ux_pacman_queue_legacy_block_url", self.sql)
        self.assertIn("(legacy_block_id, url)", self.sql)

    def test_is_rerunnable(self):
        """Every statement guarded, so a partially-applied migration can be retried."""
        self.assertNotIn("CREATE TABLE chassis_pacman_queue", self.sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS chassis_pacman_queue", self.sql)
        for index in ("ix_pacman_queue_pending", "ux_pacman_queue_legacy_block_url"):
            self.assertIn(f"IF NOT EXISTS {index}", self.sql)


if __name__ == "__main__":
    unittest.main()
