#!/usr/bin/env python3
"""test_list_recent.py - Unit tests for NotesAdapter.list_recent on all three backends.

Obsidian runs against a throwaway temp-dir vault with controlled mtimes.
SiYuan and Notion run against stubbed HTTP transports (the adapters' `_post` /
`_request` seams) - no network, no live backend.

Run:
    python3 -m pytest chassis/second_brain/tests/test_list_recent.py -v
    # or directly:
    python3 chassis/second_brain/tests/test_list_recent.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain import notion as notion_module  # noqa: E402
from chassis.second_brain.base import SearchHit  # noqa: E402
from chassis.second_brain.notion import NotionNotes  # noqa: E402
from chassis.second_brain.obsidian import ObsidianNotes  # noqa: E402
from chassis.second_brain.siyuan import SiYuanNotes, _siyuan_stamp  # noqa: E402


class ObsidianListRecentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="obsidian-list-recent-"))
        self.vault = self._tmp / "vault"
        (self.vault / "Briefings").mkdir(parents=True)
        (self.vault / ".obsidian").mkdir()
        self.now = datetime(2026, 7, 9, 12, 0, 0)  # naive == local by convention

        self._write("old.md", "ancient note " * 10, self.now - timedelta(days=30))
        self._write("Briefings/yesterday.md", "y" * 500, self.now - timedelta(hours=20))
        self._write("Briefings/today.md", "t" * 500, self.now - timedelta(hours=2))
        self._write("tiny.md", "x", self.now - timedelta(hours=1))
        self._write(".obsidian/workspace.md", "w" * 500, self.now - timedelta(hours=1))

        self.notes = ObsidianNotes(vault_path=str(self.vault))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, rel: str, body: str, mtime: datetime) -> None:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))

    def test_window_filters_and_orders_newest_first(self) -> None:
        hits = self.notes.list_recent(self.now - timedelta(days=1), self.now)
        ids = [hit.id for hit in hits]
        self.assertEqual(
            ids, ["tiny.md", "Briefings/today.md", "Briefings/yesterday.md"]
        )
        self.assertNotIn("old.md", ids)
        self.assertTrue(all(".obsidian" not in hit.id for hit in hits))

    def test_window_is_half_open(self) -> None:
        # until is exclusive: a file whose mtime equals `until` must not appear.
        boundary = self.now - timedelta(hours=2)
        hits = self.notes.list_recent(self.now - timedelta(days=1), boundary)
        self.assertEqual([hit.id for hit in hits], ["Briefings/yesterday.md"])

    def test_min_content_len_uses_file_size(self) -> None:
        hits = self.notes.list_recent(
            self.now - timedelta(days=1), self.now, min_content_len=100
        )
        ids = [hit.id for hit in hits]
        self.assertNotIn("tiny.md", ids)
        self.assertIn("Briefings/today.md", ids)

    def test_limit_and_hit_shape(self) -> None:
        hits = self.notes.list_recent(self.now - timedelta(days=1), self.now, limit=1)
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertIsInstance(hit, SearchHit)
        self.assertEqual(hit.id, "tiny.md")
        self.assertTrue(hit.deeplink.startswith("obsidian://open?vault="))
        self.assertIn("mtime", hit.raw)
        self.assertIn("size_bytes", hit.raw)

    def test_empty_window_returns_empty(self) -> None:
        hits = self.notes.list_recent(
            self.now + timedelta(days=1), self.now + timedelta(days=2)
        )
        self.assertEqual(hits, [])


class _StubbedSiYuanNotes(SiYuanNotes):
    """Capture SQL statements; return canned rows instead of hitting a kernel."""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__(
            base_url="http://127.0.0.1:1",
            token="stub",
            notebook_id="stub-notebook",
            deeplink_template="siyuan://blocks/",
        )
        self.rows = rows
        self.statements: list[str] = []

    def _post(self, path: str, payload: dict):
        assert path == "/api/query/sql", f"unexpected endpoint {path}"
        self.statements.append(payload["stmt"])
        return self.rows


class SiYuanListRecentTest(unittest.TestCase):
    def test_sql_carries_window_and_length_filter(self) -> None:
        notes = _StubbedSiYuanNotes(rows=[])
        since = datetime(2026, 7, 8, 2, 0, 0)
        until = datetime(2026, 7, 9, 2, 0, 0)
        notes.list_recent(since, until, min_content_len=200, limit=7)
        stmt = notes.statements[0]
        self.assertIn("updated >= '20260708020000'", stmt)
        self.assertIn("updated < '20260709020000'", stmt)
        self.assertIn("type = 'd'", stmt)
        self.assertIn("LIMIT 7", stmt)
        # min_content_len must NOT filter on the doc row's own content column
        # (that column holds the title) - it must aggregate child blocks.
        self.assertIn("SUM(LENGTH(b2.content))", stmt)
        self.assertIn(">= 200", stmt)

    def test_rows_map_to_search_hits(self) -> None:
        rows = [
            {
                "id": "20260709031008-ummgaxh",
                "hpath": "/Introspection/Daily Logs/2026-07-09",
                "content": "2026-07-09",
                "updated": "20260709031008",
                "created": "20260709031008",
                "body_len": 3811,
            }
        ]
        notes = _StubbedSiYuanNotes(rows=rows)
        hits = notes.list_recent(datetime(2026, 7, 9), datetime(2026, 7, 10))
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.id, "20260709031008-ummgaxh")
        self.assertEqual(hit.title, "2026-07-09")
        self.assertEqual(hit.deeplink, "siyuan://blocks/20260709031008-ummgaxh")
        self.assertEqual(hit.raw["body_len"], 3811)

    def test_aware_datetimes_convert_to_local_stamp(self) -> None:
        aware = datetime(2026, 7, 9, 10, 30, 0, tzinfo=timezone.utc)
        expected = aware.astimezone().strftime("%Y%m%d%H%M%S")
        self.assertEqual(_siyuan_stamp(aware), expected)

    def test_naive_datetime_passes_through(self) -> None:
        self.assertEqual(_siyuan_stamp(datetime(2026, 7, 9, 10, 30, 0)), "20260709103000")


def _notion_page(page_id: str, edited_iso: str, title: str) -> dict:
    return {
        "id": page_id,
        "last_edited_time": edited_iso,
        "properties": {
            "title": {
                "type": "title",
                "title": [{"plain_text": title}],
            }
        },
    }


class NotionListRecentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self._original_request = notion_module._request

    def tearDown(self) -> None:
        notion_module._request = self._original_request

    def _install(self, responder) -> None:
        def fake_request(token, method, path, payload=None):
            self.requests.append((method, path, payload))
            return responder(method, path, payload)

        notion_module._request = fake_request

    def test_windows_client_side_and_stops_at_since(self) -> None:
        pages = [
            _notion_page("p-future", "2026-07-09T12:00:00.000Z", "too new"),
            _notion_page("p-in-1", "2026-07-09T01:00:00.000Z", "in window 1"),
            _notion_page("p-in-2", "2026-07-08T20:00:00.000Z", "in window 2"),
            _notion_page("p-old", "2026-07-01T00:00:00.000Z", "older than since"),
            _notion_page("p-older", "2026-06-01T00:00:00.000Z", "never reached"),
        ]

        def responder(method, path, payload):
            assert path == "/search"
            return {"results": pages, "has_more": False}

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root")
        since = datetime(2026, 7, 8, 2, 0, 0, tzinfo=timezone.utc)
        until = datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc)
        hits = notes.list_recent(since, until)
        self.assertEqual([hit.id for hit in hits], ["p-in-1", "p-in-2"])
        self.assertEqual(hits[0].title, "in window 1")
        self.assertEqual(hits[0].deeplink, "https://www.notion.so/pin1")
        # descending sort requested from the API
        self.assertEqual(
            self.requests[0][2]["sort"],
            {"direction": "descending", "timestamp": "last_edited_time"},
        )

    def test_paginates_until_window_exhausted(self) -> None:
        page_one = [_notion_page("p-1", "2026-07-09T01:00:00.000Z", "one")]
        page_two = [_notion_page("p-2", "2026-07-08T23:00:00.000Z", "two")]

        def responder(method, path, payload):
            if payload.get("start_cursor") == "cursor-2":
                return {"results": page_two, "has_more": False}
            return {"results": page_one, "has_more": True, "next_cursor": "cursor-2"}

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root")
        hits = notes.list_recent(
            datetime(2026, 7, 8, 2, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual([hit.id for hit in hits], ["p-1", "p-2"])
        self.assertEqual(len(self.requests), 2)

    def test_min_content_len_fetches_body(self) -> None:
        pages = [
            _notion_page("p-long", "2026-07-09T01:00:00.000Z", "long"),
            _notion_page("p-short", "2026-07-09T00:30:00.000Z", "short"),
        ]

        def responder(method, path, payload):
            if path == "/search":
                return {"results": pages, "has_more": False}
            if path.startswith("/blocks/p-long"):
                return {
                    "results": [
                        {
                            "type": "paragraph",
                            "paragraph": {"rich_text": [{"plain_text": "b" * 300}]},
                        }
                    ]
                }
            return {"results": []}

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root")
        hits = notes.list_recent(
            datetime(2026, 7, 8, 2, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc),
            min_content_len=200,
        )
        self.assertEqual([hit.id for hit in hits], ["p-long"])

    def test_limit_short_circuits(self) -> None:
        pages = [
            _notion_page(f"p-{n}", "2026-07-09T01:00:00.000Z", f"page {n}")
            for n in range(5)
        ]

        def responder(method, path, payload):
            return {"results": pages, "has_more": False}

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root")
        hits = notes.list_recent(
            datetime(2026, 7, 8, 2, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc),
            limit=2,
        )
        self.assertEqual(len(hits), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
