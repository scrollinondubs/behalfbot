#!/usr/bin/env python3
"""test_queue.py - queue operations, without requiring a live Postgres.

Two layers, because neither alone is enough:

  1. A recording fake connection. It proves the code path runs, that the SQL
     each operation emits has the properties that matter (FIFO ordering, the
     exactly-once WHERE clause, SKIP LOCKED on the claim), and that count and
     claim share one predicate. It does not prove the SQL is valid Postgres.
  2. An opt-in integration test against a real database, skipped with a clear
     message when CHASSIS_TEST_PG_DSN is unset. That layer is what proves the
     SQL is valid and the semantics hold. CI does not need a database to run
     this file; a developer with one gets the stronger check for free.

The properties asserted in layer 1 are the ones with a known failure mode
behind them, not incidental string matches:

  - count and claim must use the same predicate, or the dispatcher fires
    Claude for work that does not exist or skips work that does.
  - complete must carry `processed_at IS NULL`, or a retried call resets a
    verdict that was already recorded.
  - claim must order oldest-first, or FIFO silently becomes arbitrary.

Run:
    python3 -m pytest chassis/pacman/tests/test_queue.py -v
    CHASSIS_TEST_PG_DSN=postgresql://... python3 -m pytest chassis/pacman/tests/test_queue.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from chassis.pacman import queue  # noqa: E402
from chassis.pacman.tokens import is_token  # noqa: E402


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql, params=()):
        self._conn.executed.append((" ".join(sql.split()), params))
        self.rowcount = self._conn.next_rowcount
        return self

    def fetchone(self):
        return self._conn.next_fetchone

    def fetchall(self):
        return self._conn.next_fetchall


class FakeConnection:
    """Records SQL and hands back canned rows. Not a database."""

    def __init__(self):
        self.executed: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.next_fetchone = None
        self.next_fetchall: list = []
        self.next_rowcount = 1

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    @property
    def last_sql(self) -> str:
        return self.executed[-1][0]

    @property
    def last_params(self):
        return self.executed[-1][1]


class TestAdd(unittest.TestCase):
    def test_returns_a_well_formed_token(self):
        conn = FakeConnection()
        conn.next_fetchone = ("qhtnbz",)
        token = queue.add("https://example.com/a", conn=conn)
        self.assertEqual(token, "qhtnbz")

    def test_generates_a_token_when_the_db_returns_none(self):
        conn = FakeConnection()
        conn.next_fetchone = None
        self.assertTrue(is_token(queue.add("https://example.com/a", conn=conn)))

    def test_commits_before_returning(self):
        """Durable before the caller acks. The whole point of the rewrite."""
        conn = FakeConnection()
        conn.next_fetchone = ("qhtnbz",)
        queue.add("https://example.com/a", conn=conn)
        self.assertEqual(conn.commits, 1)

    def test_rejects_non_http_urls(self):
        conn = FakeConnection()
        for bad in ("ftp://example.com", "example.com", "javascript:alert(1)", ""):
            with self.assertRaises(queue.QueueError):
                queue.add(bad, conn=conn)
        self.assertEqual(conn.executed, [])

    def test_stores_source_and_source_ref(self):
        conn = FakeConnection()
        conn.next_fetchone = ("qhtnbz",)
        queue.add("https://example.com/a", source="telegram-react-99", source_ref="99:12", conn=conn)
        params = conn.last_params
        self.assertIn("telegram-react-99", params)
        self.assertIn("99:12", params)

    def test_does_not_close_a_caller_supplied_connection(self):
        conn = FakeConnection()
        conn.next_fetchone = ("qhtnbz",)
        queue.add("https://example.com/a", conn=conn)
        self.assertFalse(conn.closed)

    def test_retries_on_a_token_collision_then_succeeds(self):
        class CollideOnce(FakeConnection):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def cursor(self):
                outer = self

                class Cur(FakeCursor):
                    def execute(self, sql, params=()):
                        outer.attempts += 1
                        outer.executed.append((" ".join(sql.split()), params))
                        if outer.attempts == 1:
                            raise RuntimeError('duplicate key value violates unique constraint "chassis_pacman_queue_token_key"')
                        return self

                return Cur(self)

        conn = CollideOnce()
        conn.next_fetchone = ("qhtnbz",)
        self.assertEqual(queue.add("https://example.com/a", conn=conn), "qhtnbz")
        self.assertEqual(conn.attempts, 2)
        self.assertEqual(conn.rollbacks, 1)

    def test_reraises_errors_that_are_not_token_collisions(self):
        class AlwaysFails(FakeConnection):
            def cursor(self):
                outer = self

                class Cur(FakeCursor):
                    def execute(self, sql, params=()):
                        outer.executed.append((" ".join(sql.split()), params))
                        raise RuntimeError('relation "chassis_pacman_queue" does not exist')

                return Cur(self)

        with self.assertRaises(RuntimeError):
            queue.add("https://example.com/a", conn=AlwaysFails())


class TestAddMany(unittest.TestCase):
    def test_urls_from_one_message_share_an_entry_group(self):
        """Requirement 5: a pasted batch is one entry until every URL is done."""
        conn = FakeConnection()
        conn.next_fetchone = ("qhtnbz",)
        queue.add_many(["https://a.example", "https://b.example"], conn=conn)
        groups = {params[4] for _, params in conn.executed}
        self.assertEqual(len(groups), 1)

    def test_inserts_one_row_per_url(self):
        conn = FakeConnection()
        conn.next_fetchone = ("qhtnbz",)
        queue.add_many(["https://a.example", "https://b.example", "https://c.example"], conn=conn)
        self.assertEqual(len(conn.executed), 3)


class TestPendingCount(unittest.TestCase):
    def test_uses_the_shared_pending_predicate(self):
        conn = FakeConnection()
        conn.next_fetchone = (7,)
        self.assertEqual(queue.pending_count(conn=conn), 7)
        self.assertIn(" ".join(queue.PENDING_PREDICATE.split()), conn.last_sql)

    def test_returns_zero_when_the_query_returns_nothing(self):
        conn = FakeConnection()
        conn.next_fetchone = None
        self.assertEqual(queue.pending_count(conn=conn), 0)

    def test_honours_the_claim_timeout_env_var(self):
        conn = FakeConnection()
        conn.next_fetchone = (0,)
        queue.pending_count(conn=conn, env={"PACMAN_CLAIM_TIMEOUT_MINUTES": "15"})
        self.assertEqual(conn.last_params["stale"], 15)


class TestClaim(unittest.TestCase):
    def test_orders_oldest_first(self):
        conn = FakeConnection()
        queue.claim(conn=conn)
        self.assertIn("ORDER BY created_at ASC", conn.last_sql)

    def test_the_returned_batch_is_ordered_by_an_outer_select(self):
        """Regression: `UPDATE ... RETURNING` does not preserve the inner ORDER BY.

        The first version of claim() ordered only inside the id-selection
        subquery, so Postgres returned the batch in update order and the drain
        processed out of FIFO order. Every fake-connection test still passed;
        only the live-Postgres test caught it. This asserts the CTE shape that
        fixes it, so the regression cannot return without a database present.
        """
        conn = FakeConnection()
        queue.claim(conn=conn)
        sql = conn.last_sql
        self.assertIn("WITH claimed AS", sql)
        self.assertTrue(
            sql.rstrip().endswith("ORDER BY created_at ASC, id ASC"),
            f"claim must apply its final ordering in the outer SELECT: {sql}",
        )

    def test_ordering_has_a_total_order_tiebreak(self):
        """created_at ties are real: add_many inserts a batch in a tight loop."""
        conn = FakeConnection()
        queue.claim(conn=conn)
        self.assertIn("ORDER BY created_at ASC, id ASC", conn.last_sql)

    def test_pending_listing_uses_the_same_total_order(self):
        conn = FakeConnection()
        queue.pending(conn=conn)
        self.assertIn("ORDER BY created_at ASC, id ASC", conn.last_sql)

    def test_uses_skip_locked_so_overlapping_drains_do_not_collide(self):
        conn = FakeConnection()
        queue.claim(conn=conn)
        self.assertIn("FOR UPDATE SKIP LOCKED", conn.last_sql)

    def test_sets_claimed_at(self):
        conn = FakeConnection()
        queue.claim(conn=conn)
        self.assertIn("SET claimed_at = NOW()", conn.last_sql)

    def test_respects_the_limit(self):
        conn = FakeConnection()
        queue.claim(limit=3, conn=conn)
        self.assertEqual(conn.last_params["limit"], 3)

    def test_shapes_rows_for_the_drain_prompt(self):
        conn = FakeConnection()
        conn.next_fetchall = [("qhtnbz", "https://a.example", "discord", "grp", "2026-07-19T10:00:00")]
        rows = queue.claim(conn=conn)
        self.assertEqual(rows[0]["token"], "qhtnbz")
        self.assertEqual(rows[0]["url"], "https://a.example")

    def test_empty_queue_yields_an_empty_list(self):
        conn = FakeConnection()
        conn.next_fetchall = []
        self.assertEqual(queue.claim(conn=conn), [])


class TestCountAndClaimAgree(unittest.TestCase):
    """The dispatcher gate and the drain must see the same queue.

    If these drift, the heartbeat either burns Claude tokens on an empty queue
    or leaves real work permanently unclaimed. Sharing one predicate constant
    is the mechanism; this is the test that the mechanism is actually used.
    """

    def test_both_statements_embed_the_shared_predicate(self):
        normalized = " ".join(queue.PENDING_PREDICATE.split())

        count_conn = FakeConnection()
        count_conn.next_fetchone = (0,)
        queue.pending_count(conn=count_conn)

        claim_conn = FakeConnection()
        queue.claim(conn=claim_conn)

        pending_conn = FakeConnection()
        queue.pending(conn=pending_conn)

        for conn in (count_conn, claim_conn, pending_conn):
            self.assertIn(normalized, conn.last_sql)

    def test_both_pass_the_same_stale_window(self):
        env = {"PACMAN_CLAIM_TIMEOUT_MINUTES": "45"}
        count_conn = FakeConnection()
        count_conn.next_fetchone = (0,)
        queue.pending_count(conn=count_conn, env=env)
        claim_conn = FakeConnection()
        queue.claim(conn=claim_conn, env=env)
        self.assertEqual(count_conn.last_params["stale"], claim_conn.last_params["stale"])


class TestComplete(unittest.TestCase):
    def test_is_exactly_once(self):
        """`processed_at IS NULL` is what stops a retry rewriting a verdict."""
        conn = FakeConnection()
        queue.complete("qhtnbz", "drop", gate=2, conn=conn)
        self.assertIn("processed_at IS NULL", conn.last_sql)

    def test_returns_false_when_the_row_was_already_processed(self):
        conn = FakeConnection()
        conn.next_rowcount = 0
        self.assertFalse(queue.complete("qhtnbz", "drop", conn=conn))

    def test_returns_true_on_first_completion(self):
        conn = FakeConnection()
        conn.next_rowcount = 1
        self.assertTrue(queue.complete("qhtnbz", "proposal", conn=conn))

    def test_rejects_an_unknown_verdict(self):
        conn = FakeConnection()
        with self.assertRaises(queue.QueueError):
            queue.complete("qhtnbz", "maybe", conn=conn)
        self.assertEqual(conn.executed, [])

    def test_accepts_every_documented_verdict(self):
        for verdict in queue.VALID_VERDICTS:
            conn = FakeConnection()
            queue.complete("qhtnbz", verdict, conn=conn)
            self.assertIn(verdict, conn.last_params)

    def test_stores_the_opaque_proposal_doc_id(self):
        """Whatever create_doc returned, unparsed. Block id, UUID, or path."""
        for doc_id in ("20260719120000-abc1234", "1f2e3d4c5b6a798899001122", "To Investigate/x.md"):
            conn = FakeConnection()
            queue.complete("qhtnbz", "proposal", proposal_doc_id=doc_id, conn=conn)
            self.assertIn(doc_id, conn.last_params)


class TestRelease(unittest.TestCase):
    def test_clears_the_claim_without_marking_processed(self):
        conn = FakeConnection()
        queue.release("qhtnbz", conn=conn)
        self.assertIn("SET claimed_at = NULL", conn.last_sql)
        self.assertIn("processed_at IS NULL", conn.last_sql)


class TestClaimTimeout(unittest.TestCase):
    def test_default(self):
        self.assertEqual(queue.claim_timeout_minutes(env={}), queue.DEFAULT_CLAIM_TIMEOUT_MINUTES)

    def test_override(self):
        self.assertEqual(queue.claim_timeout_minutes(env={"PACMAN_CLAIM_TIMEOUT_MINUTES": "5"}), 5)

    def test_garbage_and_nonpositive_values_fall_back_to_the_default(self):
        for bad in ("", "abc", "0", "-10"):
            self.assertEqual(
                queue.claim_timeout_minutes(env={"PACMAN_CLAIM_TIMEOUT_MINUTES": bad}),
                queue.DEFAULT_CLAIM_TIMEOUT_MINUTES,
                bad,
            )


@unittest.skipUnless(
    os.environ.get("CHASSIS_TEST_PG_DSN"),
    "CHASSIS_TEST_PG_DSN is not set - skipping the live-Postgres queue tests. "
    "These are the ones that prove the SQL is valid and the semantics hold. "
    "Set CHASSIS_TEST_PG_DSN to a throwaway database to run them.",
)
class TestAgainstRealPostgres(unittest.TestCase):
    """The layer that proves the SQL actually runs. Opt-in, never required in CI."""

    @classmethod
    def setUpClass(cls):
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

    def test_add_then_count_then_claim_then_complete(self):
        token = queue.add("https://example.com/first", source="discord", conn=self.conn)
        self.assertTrue(is_token(token))
        self.assertEqual(queue.pending_count(conn=self.conn), 1)

        claimed = queue.claim(limit=10, conn=self.conn)
        self.assertEqual([r["token"] for r in claimed], [token])

        # Claimed rows are no longer claimable, so the gate reads zero.
        self.assertEqual(queue.pending_count(conn=self.conn), 0)

        self.assertTrue(queue.complete(token, "drop", gate=2, conn=self.conn))
        self.assertFalse(queue.complete(token, "drop", gate=2, conn=self.conn))
        self.assertEqual(queue.pending_count(conn=self.conn), 0)

    def test_claim_is_fifo(self):
        first = queue.add("https://example.com/1", conn=self.conn)
        second = queue.add("https://example.com/2", conn=self.conn)
        third = queue.add("https://example.com/3", conn=self.conn)
        claimed = [r["token"] for r in queue.claim(limit=10, conn=self.conn)]
        self.assertEqual(claimed, [first, second, third])

    def test_claim_respects_the_batch_cap(self):
        for i in range(5):
            queue.add(f"https://example.com/{i}", conn=self.conn)
        self.assertEqual(len(queue.claim(limit=2, conn=self.conn)), 2)

    def test_release_returns_a_row_to_the_pending_set(self):
        token = queue.add("https://example.com/x", conn=self.conn)
        queue.claim(limit=1, conn=self.conn)
        self.assertEqual(queue.pending_count(conn=self.conn), 0)
        self.assertTrue(queue.release(token, conn=self.conn))
        self.assertEqual(queue.pending_count(conn=self.conn), 1)

    def test_stale_claims_become_claimable_again(self):
        """A drain that died mid-batch must not strand its rows forever."""
        token = queue.add("https://example.com/stale", conn=self.conn)
        queue.claim(limit=1, conn=self.conn)
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE chassis_pacman_queue SET claimed_at = NOW() - INTERVAL '2 hours' WHERE token = %s",
            (token,),
        )
        self.conn.commit()
        self.assertEqual(queue.pending_count(conn=self.conn), 1)
        self.assertEqual([r["token"] for r in queue.claim(limit=1, conn=self.conn)], [token])

    def test_tokens_are_unique_across_many_inserts(self):
        tokens = {queue.add(f"https://example.com/u{i}", conn=self.conn) for i in range(200)}
        self.assertEqual(len(tokens), 200)

    def test_a_pasted_batch_keeps_its_order(self):
        """add_many inserts in a tight loop, so created_at ties are likely here."""
        tokens = queue.add_many(
            [f"https://example.com/batch{i}" for i in range(12)],
            source="discord",
            conn=self.conn,
        )
        claimed = [r["token"] for r in queue.claim(limit=50, conn=self.conn)]
        self.assertEqual(claimed, tokens)

    def test_migrations_are_idempotent_against_a_real_database(self):
        from chassis.db import apply_migrations

        self.assertEqual(apply_migrations(self.conn), [])

    def test_pending_lists_without_claiming(self):
        queue.add("https://example.com/p", conn=self.conn)
        listed = queue.pending(conn=self.conn)
        self.assertEqual(len(listed), 1)
        self.assertEqual(queue.pending_count(conn=self.conn), 1)


if __name__ == "__main__":
    unittest.main()
