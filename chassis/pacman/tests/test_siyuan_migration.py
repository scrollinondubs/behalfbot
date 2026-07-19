#!/usr/bin/env python3
"""test_siyuan_migration.py - the one-time SiYuan backfill's planning stage.

This runs once, against a real queue, and gets one chance. The failure the
design note names explicitly is "a block containing two URLs being counted as
one entry" - a silent halving of the queue that nothing downstream would ever
notice, because the missing URLs exist nowhere else once the source Discord or
Telegram message has scrolled away.

So the planning stage is tested hard and separately from the insert: given the
blocks SiYuan returns, does it produce exactly the right rows, in the right
order, and does it REPORT rather than DROP anything it cannot handle?

The insert stage is tested against a live database in test_queue.py's opt-in
class. Splitting them means the part with all the reasoning in it is covered
with no database at all.

Run:
    python3 -m pytest chassis/pacman/tests/test_siyuan_migration.py -v
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "pacman_migrate_siyuan_queue",
    REPO_ROOT / "chassis" / "scripts" / "pacman-migrate-siyuan-queue.py",
)
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)


def block(block_id: str, content: str, created: str | None = None) -> dict:
    return {"id": block_id, "content": content, "created": created}


class TestUrlExtraction(unittest.TestCase):
    def test_one_url_becomes_one_row(self):
        rows, problems = migrate.plan([block("20260718120000-abc1234", "https://example.com/a")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://example.com/a")
        self.assertEqual(problems, [])

    def test_two_urls_in_one_block_become_two_rows(self):
        """The exact failure the design note's verification step guards against."""
        rows, _ = migrate.plan([
            block("20260718120000-abc1234", "https://example.com/a and https://example.com/b")
        ])
        self.assertEqual([r["url"] for r in rows], ["https://example.com/a", "https://example.com/b"])

    def test_urls_from_one_block_share_an_entry_group(self):
        rows, _ = migrate.plan([
            block("20260718120000-abc1234", "https://example.com/a https://example.com/b")
        ])
        self.assertEqual(len({r["entry_group"] for r in rows}), 1)

    def test_urls_from_different_blocks_do_not_share_an_entry_group(self):
        rows, _ = migrate.plan([
            block("20260718120000-abc1234", "https://example.com/a"),
            block("20260718130000-def5678", "https://example.com/b"),
        ])
        self.assertEqual(len({r["entry_group"] for r in rows}), 2)

    def test_duplicate_urls_within_one_block_collapse(self):
        rows, _ = migrate.plan([
            block("20260718120000-abc1234", "https://example.com/a https://example.com/a")
        ])
        self.assertEqual(len(rows), 1)

    def test_strips_trailing_punctuation(self):
        rows, _ = migrate.plan([block("20260718120000-abc1234", "see https://example.com/a.")])
        self.assertEqual(rows[0]["url"], "https://example.com/a")

    def test_extracts_from_markdown_link_syntax(self):
        rows, _ = migrate.plan([block("20260718120000-abc1234", "[title](https://example.com/a)")])
        self.assertEqual(rows[0]["url"], "https://example.com/a")


class TestNothingIsDroppedSilently(unittest.TestCase):
    def test_a_block_with_no_parseable_url_is_reported(self):
        """It matched '%http%' but yielded nothing. That must surface."""
        rows, problems = migrate.plan([block("20260718120000-abc1234", "mentions http but has no link")])
        self.assertEqual(rows, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("no parseable URL", problems[0]["reason"])
        self.assertEqual(problems[0]["block_id"], "20260718120000-abc1234")

    def test_a_problem_carries_enough_context_to_recover_by_hand(self):
        rows, problems = migrate.plan([block("20260718120000-abc1234", "mentions http somewhere")])
        self.assertIn("mentions http somewhere", problems[0]["content"])

    def test_an_unparseable_block_id_is_reported_not_guessed(self):
        rows, problems = migrate.plan([block("not-a-siyuan-id", "https://example.com/a")])
        self.assertEqual(rows, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("unparseable timestamp", problems[0]["reason"])

    def test_good_blocks_still_migrate_when_a_sibling_is_broken(self):
        """One bad block must not abort the batch, only be reported alongside it."""
        rows, problems = migrate.plan([
            block("20260718120000-abc1234", "https://example.com/good"),
            block("bad-id", "https://example.com/orphan"),
        ])
        self.assertEqual([r["url"] for r in rows], ["https://example.com/good"])
        self.assertEqual(len(problems), 1)


class TestTimestampReconstruction(unittest.TestCase):
    def test_created_at_comes_from_the_block_id_prefix(self):
        """The one time SiYuan's encoded timestamp is genuinely useful."""
        iso, used_fallback = migrate.created_at_from_block_id("20260718120000-abc1234", None)
        self.assertEqual(iso, "2026-07-18T12:00:00")
        self.assertFalse(used_fallback)

    def test_fifo_order_survives_the_move(self):
        rows, _ = migrate.plan([
            block("20260718120000-aaa1111", "https://example.com/first"),
            block("20260718130000-bbb2222", "https://example.com/second"),
            block("20260719090000-ccc3333", "https://example.com/third"),
        ])
        timestamps = [r["created_at"] for r in rows]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_falls_back_to_the_siyuan_created_column_and_flags_it(self):
        rows, problems = migrate.plan([block("bad-id", "https://example.com/a", created="20260718120000")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["created_at"], "2026-07-18T12:00:00")
        self.assertTrue(any("SiYuan created column" in p["reason"] for p in problems))

    def test_an_impossible_date_is_not_silently_accepted(self):
        iso, _ = migrate.created_at_from_block_id("20261345990000-abc1234", None)
        self.assertIsNone(iso)


class TestRowShape(unittest.TestCase):
    def test_every_row_carries_its_legacy_block_id(self):
        """That column is the idempotency key. Without it a re-run duplicates."""
        rows, _ = migrate.plan([block("20260718120000-abc1234", "https://example.com/a")])
        self.assertEqual(rows[0]["legacy_block_id"], "20260718120000-abc1234")

    def test_rows_carry_exactly_the_fields_the_insert_binds(self):
        rows, _ = migrate.plan([block("20260718120000-abc1234", "https://example.com/a")])
        self.assertEqual(set(rows[0]), {"url", "legacy_block_id", "entry_group", "created_at"})

    def test_migration_source_tag_is_distinguishable(self):
        """Migrated rows must be tellable from natively-enqueued ones afterwards."""
        self.assertEqual(migrate.MIGRATION_SOURCE, "siyuan-migration")

    def test_an_empty_queue_plans_nothing_and_reports_nothing(self):
        self.assertEqual(migrate.plan([]), ([], []))


class TestInsertIsIdempotent(unittest.TestCase):
    def test_insert_uses_on_conflict_do_nothing_against_the_block_url_key(self):
        """Re-running after a partial failure is the intended recovery path."""
        class Cur:
            def __init__(self):
                self.sql = []

            def execute(self, sql, params=()):
                self.sql.append(" ".join(sql.split()))

            def fetchone(self):
                return ("qhtnbz",)

        class Conn:
            def __init__(self):
                self.cur = Cur()
                self.commits = 0

            def cursor(self):
                return self.cur

            def commit(self):
                self.commits += 1

        conn = Conn()
        rows, _ = migrate.plan([block("20260718120000-abc1234", "https://example.com/a")])
        inserted, skipped = migrate.insert_rows(conn, rows)
        self.assertEqual((inserted, skipped), (1, 0))
        self.assertIn("ON CONFLICT (legacy_block_id, url)", conn.cur.sql[0])
        self.assertIn("DO NOTHING", conn.cur.sql[0])

    def test_a_row_that_already_exists_counts_as_skipped_not_inserted(self):
        class Cur:
            def execute(self, sql, params=()):
                pass

            def fetchone(self):
                return None  # ON CONFLICT DO NOTHING returned no row

        class Conn:
            def cursor(self):
                return Cur()

            def commit(self):
                pass

        rows, _ = migrate.plan([block("20260718120000-abc1234", "https://example.com/a")])
        self.assertEqual(migrate.insert_rows(Conn(), rows), (0, 1))


@unittest.skipUnless(
    __import__("os").environ.get("CHASSIS_TEST_PG_DSN"),
    "CHASSIS_TEST_PG_DSN is not set - skipping the live-Postgres backfill tests. "
    "These prove the ON CONFLICT idempotency key actually works. "
    "Set CHASSIS_TEST_PG_DSN to a throwaway database to run them.",
)
class TestBackfillAgainstRealPostgres(unittest.TestCase):
    """Idempotency is the property the operator relies on to retry a partial run."""

    @classmethod
    def setUpClass(cls):
        import os

        from chassis.db import apply_migrations, connect

        cls.conn = connect(dsn=os.environ["CHASSIS_TEST_PG_DSN"])
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM chassis_pacman_queue")
        self.conn.commit()

    def test_a_second_run_inserts_nothing(self):
        rows, _ = migrate.plan([
            block("20260718120000-abc1234", "https://example.com/a https://example.com/b"),
            block("20260718130000-def5678", "https://example.com/c"),
        ])
        self.assertEqual(migrate.insert_rows(self.conn, rows), (3, 0))
        self.assertEqual(migrate.insert_rows(self.conn, rows), (0, 3))

    def test_migrated_rows_are_immediately_claimable_in_original_order(self):
        from chassis.pacman import queue

        rows, _ = migrate.plan([
            block("20260718120000-aaa1111", "https://example.com/first"),
            block("20260718130000-bbb2222", "https://example.com/second"),
        ])
        migrate.insert_rows(self.conn, rows)
        claimed = [r["url"] for r in queue.claim(limit=10, conn=self.conn)]
        self.assertEqual(claimed, ["https://example.com/first", "https://example.com/second"])

    def test_natively_enqueued_rows_do_not_collide_with_each_other(self):
        """The partial index must only constrain migrated rows, not normal ones."""
        from chassis.pacman import queue

        queue.add("https://example.com/same", conn=self.conn)
        queue.add("https://example.com/same", conn=self.conn)
        self.assertEqual(queue.pending_count(conn=self.conn), 2)


class TestMigrationScriptDoesNotDelete(unittest.TestCase):
    """Rollback depends on the SiYuan blocks surviving the backfill untouched."""

    def test_no_delete_call_anywhere_in_the_script(self):
        source = (REPO_ROOT / "chassis" / "scripts" / "pacman-migrate-siyuan-queue.py").read_text(encoding="utf-8")
        for forbidden in ("removeDoc", "deleteBlock", "delete_block", "DELETE FROM"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
