#!/usr/bin/env python3
"""test_siyuan_search_escaping.py - what SiYuanNotes may and may not put in its SQL.

Two contracts, both learned the hard way against a live kernel:

1. NO `ESCAPE` CLAUSE. SiYuan's /api/query/sql does not accept one. A query with
   `ESCAPE '\\'` (or any escape char) comes back `{"code": 0, "data": null}` -
   zero rows - while the identical query without it returns hundreds. An earlier
   revision of this adapter added ESCAPE to neutralize LIKE wildcards and thereby
   broke search() completely: every query returned no hits. The wildcards stay
   live instead; `_escape` alone is the injection defense.

2. `data: null` FROM THE SQL ENDPOINT IS AN ERROR, NOT AN EMPTY RESULT. That is
   what made (1) silent. Callers used to do `result if isinstance(result, list)
   else []`, so a query the kernel refused looked exactly like "no matches". A
   genuinely empty result set is `[]`.

The transport is stubbed - no network, no kernel.

Run:
    python3 -m pytest chassis/second_brain/tests/test_siyuan_search_escaping.py -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain.siyuan import SiYuanError, SiYuanNotes  # noqa: E402


class StubNotes(SiYuanNotes):
    """SiYuanNotes with the HTTP call replaced by a recorder + canned reply."""

    _UNSET = object()

    def __init__(self, reply=_UNSET) -> None:
        super().__init__(
            base_url="http://kernel.invalid:6806",
            token="fake-token",
            notebook_id="20231101120000-abc123",
            deeplink_template="siyuan://blocks/",
        )
        self.statements: list[str] = []
        # `reply=None` must mean "the kernel answered data: null", so the default
        # needs a sentinel rather than None.
        self._reply = [] if reply is self._UNSET else reply

    def _post(self, path: str, payload: dict):  # type: ignore[override]
        if path == "/api/query/sql":
            self.statements.append(payload.get("stmt", ""))
        return self._reply

    def sql_for(self, query: str) -> str:
        self.statements.clear()
        self.search(query)
        assert len(self.statements) == 1
        return self.statements[0]


class NoEscapeClauseTest(unittest.TestCase):
    """SiYuan rejects ESCAPE. Emitting one silently zeroes out every search."""

    def setUp(self) -> None:
        self.notes = StubNotes()

    def test_search_sql_contains_no_escape_token(self) -> None:
        sql = self.notes.sql_for("briefing")
        self.assertNotIn("ESCAPE", sql.upper())
        self.assertIn("LIKE '%briefing%'", sql)

    def test_no_escape_clause_even_when_the_query_holds_wildcards(self) -> None:
        # The tempting-but-fatal case: wildcards present, so an implementation
        # might reach for ESCAPE. It must not.
        for query in ("100% cotton", "snake_case", "C:\\Users", "%_%"):
            sql = self.notes.sql_for(query)
            self.assertNotIn("ESCAPE", sql.upper(), f"ESCAPE emitted for {query!r}")

    def test_wildcards_pass_through_unescaped(self) -> None:
        # Documented, accepted tradeoff: '%' and '_' stay live LIKE wildcards.
        self.assertIn("LIKE '%100% cotton%'", self.notes.sql_for("100% cotton"))
        self.assertIn("LIKE '%snake_case%'", self.notes.sql_for("snake_case"))

    def test_single_quote_cannot_break_out_of_the_literal(self) -> None:
        # Quote-doubling is the injection defense, and it survives.
        self.assertIn("LIKE '%O''Brien%'", self.notes.sql_for("O'Brien"))

    def test_injection_attempt_stays_inside_the_literal(self) -> None:
        sql = self.notes.sql_for("' OR 1=1 --")
        self.assertIn("LIKE '%'' OR 1=1 --%'", sql)
        self.assertEqual(sql.count("SELECT"), 1)


class NullDataIsAnErrorTest(unittest.TestCase):
    """`{"code": 0, "data": null}` means REFUSED, not "no rows"."""

    def test_search_raises_on_null_data(self) -> None:
        notes = StubNotes(reply=None)
        with self.assertRaises(SiYuanError) as ctx:
            notes.search("Vibecode")
        message = str(ctx.exception)
        self.assertIn("data=null", message)
        self.assertIn("SELECT", message)  # the offending statement is surfaced

    def test_list_recent_raises_on_null_data(self) -> None:
        notes = StubNotes(reply=None)
        now = datetime.now()
        with self.assertRaises(SiYuanError):
            notes.list_recent(now - timedelta(days=1), now)

    def test_block_lookups_raise_on_null_data(self) -> None:
        notes = StubNotes(reply=None)
        with self.assertRaises(SiYuanError):
            notes._block_to_hpath("20231101120000-abc123")
        with self.assertRaises(SiYuanError):
            notes._block_title("20231101120000-abc123")

    def test_empty_list_is_still_a_legitimate_empty_result(self) -> None:
        # The distinction the fix rests on: [] is "no matches", null is "refused".
        notes = StubNotes(reply=[])
        self.assertEqual(notes.search("nothing matches this"), [])

    def test_non_list_payload_raises(self) -> None:
        notes = StubNotes(reply={"unexpected": "shape"})
        with self.assertRaises(SiYuanError):
            notes.search("Vibecode")


if __name__ == "__main__":
    unittest.main(verbosity=2)
