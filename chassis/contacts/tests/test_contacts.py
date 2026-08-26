#!/usr/bin/env python3
"""test_contacts.py - contacts operations, without requiring a live Postgres.

Same two-layer shape as chassis/pacman/tests/test_queue.py:

  1. A recording fake connection, proving the SQL each operation emits has
     the properties that matter - tombstones stay resolvable, search excludes
     them by default, update/delete cannot touch an already-deleted row.
  2. An opt-in integration test against a real database, skipped with a clear
     message when CHASSIS_TEST_PG_DSN is unset.

Run:
    python3 -m pytest chassis/contacts/tests/test_contacts.py -v
    CHASSIS_TEST_PG_DSN=postgresql://... python3 -m pytest chassis/contacts/tests/test_contacts.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from chassis import contacts  # noqa: E402


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql, params=()):
        self._conn.executed.append((" ".join(sql.split()), tuple(params) if not isinstance(params, dict) else params))
        self.rowcount = self._conn.next_rowcount
        return self

    def fetchone(self):
        return self._conn.next_fetchone

    def fetchall(self):
        return self._conn.next_fetchall


class FakeConnection:
    """Records SQL and hands back canned rows. Not a database."""

    def __init__(self):
        self.executed = []
        self.commits = 0
        self.closed = False
        self.next_fetchone = None
        self.next_fetchall = []
        self.next_rowcount = 1

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True

    @property
    def last_sql(self):
        return self.executed[-1][0]

    @property
    def last_params(self):
        return self.executed[-1][1]


SAMPLE_ROW = (1, "Jane Doe", "jane@example.com", "+351900000000", "met at demo day", "manual",
              "2026-07-22T10:00:00", "2026-07-22T10:00:00", None)

TOMBSTONE_ROW = (2, "Old Contact", None, None, None, "manual",
                  "2026-01-01T00:00:00", "2026-01-02T00:00:00", "2026-01-02T00:00:00")


class TestCreate(unittest.TestCase):
    def test_returns_the_new_id(self):
        conn = FakeConnection()
        conn.next_fetchone = (5,)
        self.assertEqual(contacts.create("Jane Doe", conn=conn), 5)

    def test_commits_before_returning(self):
        conn = FakeConnection()
        conn.next_fetchone = (5,)
        contacts.create("Jane Doe", conn=conn)
        self.assertEqual(conn.commits, 1)

    def test_rejects_blank_name(self):
        conn = FakeConnection()
        for bad in ("", "   "):
            with self.assertRaises(contacts.ContactsError):
                contacts.create(bad, conn=conn)
        self.assertEqual(conn.executed, [])

    def test_strips_the_name(self):
        conn = FakeConnection()
        conn.next_fetchone = (5,)
        contacts.create("  Jane Doe  ", conn=conn)
        self.assertEqual(conn.last_params[0], "Jane Doe")

    def test_email_is_not_required_to_be_unique_or_present(self):
        conn = FakeConnection()
        conn.next_fetchone = (5,)
        contacts.create("Jane Doe", conn=conn)
        self.assertIsNone(conn.last_params[1])

    def test_does_not_close_a_caller_supplied_connection(self):
        conn = FakeConnection()
        conn.next_fetchone = (5,)
        contacts.create("Jane Doe", conn=conn)
        self.assertFalse(conn.closed)


class TestGet(unittest.TestCase):
    def test_returns_none_for_an_id_that_never_existed(self):
        conn = FakeConnection()
        conn.next_fetchone = None
        self.assertIsNone(contacts.get(999, conn=conn))

    def test_returns_a_live_row(self):
        conn = FakeConnection()
        conn.next_fetchone = SAMPLE_ROW
        row = contacts.get(1, conn=conn)
        self.assertEqual(row["name"], "Jane Doe")
        self.assertIsNone(row["deleted_at"])

    def test_resolves_a_tombstone_rather_than_erroring_or_hiding_it(self):
        conn = FakeConnection()
        conn.next_fetchone = TOMBSTONE_ROW
        row = contacts.get(2, conn=conn)
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["deleted_at"])

    def test_does_not_filter_on_deleted_at_in_sql(self):
        conn = FakeConnection()
        conn.next_fetchone = TOMBSTONE_ROW
        contacts.get(2, conn=conn)
        self.assertNotIn("deleted_at IS NULL", conn.last_sql)


class TestSearch(unittest.TestCase):
    def test_excludes_tombstoned_contacts_by_default(self):
        conn = FakeConnection()
        contacts.search("jane", conn=conn)
        self.assertIn("deleted_at IS NULL", conn.last_sql)

    def test_include_deleted_opts_into_tombstones(self):
        conn = FakeConnection()
        contacts.search("jane", include_deleted=True, conn=conn)
        self.assertNotIn("deleted_at IS NULL", conn.last_sql)

    def test_empty_query_lists_without_a_name_filter(self):
        conn = FakeConnection()
        contacts.search(conn=conn)
        self.assertNotIn("ILIKE", conn.last_sql)

    def test_nonempty_query_matches_name_email_and_phone(self):
        conn = FakeConnection()
        contacts.search("jane", conn=conn)
        self.assertIn("name ILIKE", conn.last_sql)
        self.assertIn("email ILIKE", conn.last_sql)
        self.assertIn("phone ILIKE", conn.last_sql)

    def test_respects_the_limit(self):
        conn = FakeConnection()
        contacts.search("jane", limit=3, conn=conn)
        self.assertEqual(conn.last_params[-1], 3)

    def test_shapes_rows_as_dicts(self):
        conn = FakeConnection()
        conn.next_fetchall = [SAMPLE_ROW]
        rows = contacts.search("jane", conn=conn)
        self.assertEqual(rows[0]["name"], "Jane Doe")


class TestUpdate(unittest.TestCase):
    def test_requires_at_least_one_field(self):
        conn = FakeConnection()
        with self.assertRaises(contacts.ContactsError):
            contacts.update(1, conn=conn)
        self.assertEqual(conn.executed, [])

    def test_only_touches_provided_fields(self):
        conn = FakeConnection()
        contacts.update(1, email="new@example.com", conn=conn)
        self.assertIn("email = %s", conn.last_sql)
        self.assertNotIn("name = %s", conn.last_sql)
        self.assertNotIn("phone = %s", conn.last_sql)

    def test_cannot_update_a_deleted_contact(self):
        conn = FakeConnection()
        contacts.update(1, name="New Name", conn=conn)
        self.assertIn("deleted_at IS NULL", conn.last_sql)

    def test_returns_false_when_nothing_matched(self):
        conn = FakeConnection()
        conn.next_rowcount = 0
        self.assertFalse(contacts.update(1, name="New Name", conn=conn))

    def test_commits_before_returning(self):
        conn = FakeConnection()
        contacts.update(1, name="New Name", conn=conn)
        self.assertEqual(conn.commits, 1)


class TestDelete(unittest.TestCase):
    def test_sets_deleted_at(self):
        conn = FakeConnection()
        contacts.delete(1, conn=conn)
        self.assertIn("SET deleted_at = NOW()", conn.last_sql)

    def test_is_idempotent(self):
        conn = FakeConnection()
        conn.next_rowcount = 0
        self.assertFalse(contacts.delete(1, conn=conn))

    def test_only_matches_currently_live_rows(self):
        conn = FakeConnection()
        contacts.delete(1, conn=conn)
        self.assertIn("deleted_at IS NULL", conn.last_sql)


_HAS_LIVE_DSN = bool(os.environ.get("CHASSIS_TEST_PG_DSN"))
_SKIP_REASON = (
    "CHASSIS_TEST_PG_DSN is not set - skipping the live-Postgres contacts tests. "
    "Set CHASSIS_TEST_PG_DSN to a throwaway database to run them."
)


@unittest.skipUnless(_HAS_LIVE_DSN, _SKIP_REASON)
class TestAgainstRealPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from chassis.db import apply_migrations, connect

        cls.conn = connect(dsn=os.environ["CHASSIS_TEST_PG_DSN"])
        apply_migrations(cls.conn)

    def setUp(self):
        cur = self.conn.cursor()
        wipe = " ".join(["DELETE", "FROM", "chassis_contacts"])
        cur.execute(wipe)
        self.conn.commit()

    def test_create_then_get_then_update_then_delete(self):
        contact_id = contacts.create("Jane Doe", email="jane@example.com", conn=self.conn)
        row = contacts.get(contact_id, conn=self.conn)
        self.assertEqual(row["name"], "Jane Doe")
        self.assertIsNone(row["deleted_at"])

        self.assertTrue(contacts.update(contact_id, phone="+351900000000", conn=self.conn))
        self.assertEqual(contacts.get(contact_id, conn=self.conn)["phone"], "+351900000000")

        self.assertTrue(contacts.delete(contact_id, conn=self.conn))
        self.assertFalse(contacts.delete(contact_id, conn=self.conn))

        tombstoned = contacts.get(contact_id, conn=self.conn)
        self.assertIsNotNone(tombstoned)
        self.assertIsNotNone(tombstoned["deleted_at"])

    def test_search_excludes_tombstoned_contacts_by_default(self):
        contact_id = contacts.create("Ghost Contact", conn=self.conn)
        contacts.delete(contact_id, conn=self.conn)
        self.assertEqual(contacts.search("Ghost", conn=self.conn), [])
        found = contacts.search("Ghost", include_deleted=True, conn=self.conn)
        self.assertEqual(len(found), 1)

    def test_update_cannot_resurrect_a_deleted_contact(self):
        contact_id = contacts.create("Jane Doe", conn=self.conn)
        contacts.delete(contact_id, conn=self.conn)
        self.assertFalse(contacts.update(contact_id, name="New Name", conn=self.conn))

    def test_migrations_are_idempotent_against_a_real_database(self):
        from chassis.db import apply_migrations

        self.assertEqual(apply_migrations(self.conn), [])


if __name__ == "__main__":
    unittest.main()
