#!/usr/bin/env python3
"""Tests for NotesAdapter.read_doc_source and SiYuan's `notebook:<id>` parent.

read_doc_source exists for docs whose links ARE the content (the second-brain
map), so every test here is about links surviving the read.

Run:
    python3 -m pytest chassis/second_brain/tests/test_read_doc_source.py -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain import notion as notion_module  # noqa: E402
from chassis.second_brain.notion import NotionNotes  # noqa: E402
from chassis.second_brain.obsidian import ObsidianNotes  # noqa: E402
from chassis.second_brain.siyuan import SiYuanError, SiYuanNotes  # noqa: E402


class _StubSiYuan(SiYuanNotes):
    def __init__(self, routes: dict) -> None:
        super().__init__("http://127.0.0.1:1", "stub", "nb-1", "siyuan://blocks/")
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def _post(self, path, payload):
        self.calls.append((path, payload))
        route = self.routes[path]
        return route(payload) if callable(route) else route


class SiYuanReadDocSourceTest(unittest.TestCase):
    def test_keeps_refs_and_strips_block_attributes(self):
        kramdown = (
            "# Map\n{: id=\"20240101120000-aaaaaaa\" updated=\"20240101\"}\n\n\n\n"
            "- ((20240101120000-bbbbbbb 'Alpha')) - the launch   \n"
            "{: id=\"20240101120000-ccccccc\"}\n"
        )
        notes = _StubSiYuan({"/api/block/getBlockKramdown": {"kramdown": kramdown}})
        text = notes.read_doc_source("20240101120000-aaaaaaa")
        self.assertIn("((20240101120000-bbbbbbb 'Alpha'))", text)
        self.assertNotIn("{:", text)
        self.assertNotIn("\n\n\n", text)
        self.assertEqual(notes.calls[0][0], "/api/block/getBlockKramdown")

    def test_empty_doc_raises(self):
        notes = _StubSiYuan({"/api/block/getBlockKramdown": {"kramdown": "  "}})
        with self.assertRaises(SiYuanError):
            notes.read_doc_source("20240101120000-aaaaaaa")


class SiYuanNotebookParentTest(unittest.TestCase):
    def routes(self, rows):
        return {
            "/api/notebook/lsNotebooks": {"notebooks": [{"id": "nb-projects", "name": "Projects"}]},
            "/api/query/sql": lambda payload: rows,
        }

    def test_notebook_parent_lists_that_notebooks_root_docs(self):
        rows = [
            {"id": "d-2", "hpath": "/Beta", "box": "nb-projects", "content": "Beta"},
            {"id": "d-1", "hpath": "/Alpha", "box": "nb-projects", "content": "Alpha"},
            {"id": "d-3", "hpath": "/Alpha/Child", "box": "nb-projects", "content": "Child"},
        ]
        notes = _StubSiYuan(self.routes(rows))
        hits = notes.list_children("notebook:nb-projects")
        self.assertEqual([h.id for h in hits], ["d-1", "d-2"])
        sql = [p["stmt"] for path, p in notes.calls if path == "/api/query/sql"][0]
        self.assertIn("box = 'nb-projects'", sql)
        self.assertIn("hpath LIKE '/%'", sql)

    def test_unknown_notebook_raises(self):
        notes = _StubSiYuan(self.routes([]))
        with self.assertRaises(SiYuanError) as ctx:
            notes.list_children("notebook:nb-missing")
        self.assertIn("nb-missing", str(ctx.exception))


class ObsidianReadDocSourceTest(unittest.TestCase):
    def test_returns_the_file_with_wikilinks(self):
        with tempfile.TemporaryDirectory() as vault:
            Path(vault, "_MAP.md").write_text("- [[Alpha]] - launch\n", encoding="utf-8")
            notes = ObsidianNotes(vault_path=vault)
            self.assertEqual(notes.read_doc_source("_MAP.md"), "- [[Alpha]] - launch\n")


class NotionReadDocSourceTest(unittest.TestCase):
    def setUp(self):
        original = notion_module._request
        self.addCleanup(lambda: setattr(notion_module, "_request", original))

    def test_page_links_render_with_their_ids(self):
        blocks = [
            {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [
                {"type": "mention", "plain_text": "Alpha",
                 "mention": {"type": "page", "page": {"id": "aaaaaaaa-1111-2222-3333-444444444444"}}},
                {"type": "text", "plain_text": " - the launch"},
            ]}},
            {"type": "link_to_page", "link_to_page": {"type": "page_id", "page_id": "bbbbbbbb-1111-2222-3333-444444444444"}},
        ]
        notion_module._request = lambda token, method, path, payload=None: {"results": blocks}
        text = NotionNotes("tok", "root").read_doc_source("map")
        self.assertIn("- Alpha - the launch", text)
        self.assertIn("notion:aaaaaaaa-1111-2222-3333-444444444444 Alpha", text)
        self.assertIn("notion:bbbbbbbb-1111-2222-3333-444444444444", text)


if __name__ == "__main__":
    unittest.main()
