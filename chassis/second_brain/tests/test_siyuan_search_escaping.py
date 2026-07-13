#!/usr/bin/env python3
"""test_siyuan_search_escaping.py - SiYuanNotes.search() escapes its LIKE pattern.

search() is exposed as an MCP tool by second_brain/mcp_server.py, so `query` is
arbitrary model-supplied or user-supplied text. It is interpolated into a sqlite
`LIKE '%...%'` pattern, where a bare quote-escape leaves the wildcards live: a
query containing `%` or `_` would silently match far more than the user asked
for, and a `'` would break out of the literal.

No network - _post is stubbed to capture the SQL the adapter would have sent.

Run:
    python3 -m pytest chassis/second_brain/tests/test_siyuan_search_escaping.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain.siyuan import LIKE_ESCAPE_CHAR, SiYuanNotes  # noqa: E402


class CapturingNotes(SiYuanNotes):
    """SiYuanNotes with the HTTP call replaced by a recorder."""

    def __init__(self) -> None:
        super().__init__(
            base_url="http://kernel.invalid:6806",
            token="fake-token",
            notebook_id="20231101120000-abc123",
            deeplink_template="siyuan://blocks/",
        )
        self.statements: list[str] = []

    def _post(self, path: str, payload: dict):  # type: ignore[override]
        self.statements.append(payload.get("stmt", ""))
        return []

    def sql_for(self, query: str) -> str:
        self.statements.clear()
        self.search(query)
        assert len(self.statements) == 1
        return self.statements[0]


class SearchEscapingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.notes = CapturingNotes()

    def test_escape_clause_is_present(self) -> None:
        sql = self.notes.sql_for("briefing")
        self.assertIn(f"ESCAPE '{LIKE_ESCAPE_CHAR}'", sql)
        self.assertIn("LIKE '%briefing%'", sql)

    def test_percent_is_escaped_not_left_as_a_wildcard(self) -> None:
        # "100% cotton" must match that literal string, not "100<anything>cotton".
        sql = self.notes.sql_for("100% cotton")
        self.assertIn(f"LIKE '%100{LIKE_ESCAPE_CHAR}% cotton%'", sql)

    def test_underscore_is_escaped_not_left_as_a_single_char_wildcard(self) -> None:
        sql = self.notes.sql_for("snake_case")
        self.assertIn(f"LIKE '%snake{LIKE_ESCAPE_CHAR}_case%'", sql)

    def test_escape_char_itself_is_doubled(self) -> None:
        # A literal backslash in the query must not consume the next character.
        sql = self.notes.sql_for("C:\\Users")
        self.assertIn(f"LIKE '%C:{LIKE_ESCAPE_CHAR * 2}Users%'", sql)

    def test_single_quote_cannot_break_out_of_the_literal(self) -> None:
        sql = self.notes.sql_for("O'Brien")
        self.assertIn("LIKE '%O''Brien%'", sql)

    def test_injection_attempt_stays_inside_the_literal(self) -> None:
        sql = self.notes.sql_for("' OR 1=1 --")
        # The quote is doubled, so the payload never terminates the string.
        self.assertIn("LIKE '%'' OR 1=1 --%'", sql)
        self.assertEqual(sql.count("SELECT"), 1)

    def test_wildcards_survive_the_round_trip_only_as_escaped_literals(self) -> None:
        sql = self.notes.sql_for("%_%")
        pattern = sql.split("LIKE '", 1)[1].split("'", 1)[0]
        # Every wildcard inside the user's text is preceded by the escape char;
        # the only bare wildcards are the two the adapter adds itself.
        body = pattern[1:-1]
        self.assertEqual(
            body, f"{LIKE_ESCAPE_CHAR}%{LIKE_ESCAPE_CHAR}_{LIKE_ESCAPE_CHAR}%"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
