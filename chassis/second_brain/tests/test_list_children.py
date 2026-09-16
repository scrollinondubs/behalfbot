#!/usr/bin/env python3
"""test_list_children.py - Unit tests for NotesAdapter.list_children on all three backends.

Same shape as test_list_recent.py: Obsidian runs against a throwaway temp-dir
vault, SiYuan and Notion against stubbed transports (`_post` / `_request`). No
network, no live backend, no account.

The SiYuan stub answers PER STATEMENT rather than returning one canned row set,
because `list_children` issues two queries - resolve the parent, then list its
children - and the point of several of these tests is that the second query's
rows are re-filtered in Python. A stub that ignored the statement could only
ever assert on the SQL string.

Run:
    python3 -m pytest chassis/second_brain/tests/test_list_children.py -v
    # or directly:
    python3 chassis/second_brain/tests/test_list_children.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain import notion as notion_module  # noqa: E402
from chassis.second_brain.base import SearchHit  # noqa: E402
from chassis.second_brain.notion import NotionNotes  # noqa: E402
from chassis.second_brain.obsidian import ObsidianError, ObsidianNotes  # noqa: E402
from chassis.second_brain.siyuan import SiYuanError, SiYuanNotes  # noqa: E402


# --------------------------------------------------------------------------
# SiYuan
# --------------------------------------------------------------------------


def _doc_row(block_id: str, hpath: str, box: str = "nb-1") -> dict:
    """A `type='d'` row shaped the way the blocks table returns one."""
    return {
        "id": block_id,
        "hpath": hpath,
        "box": box,
        "content": hpath.rsplit("/", 1)[-1],
        "updated": "20260916120000",
        "created": "20260916120000",
    }


class _StubbedSiYuanNotes(SiYuanNotes):
    """Answer each SQL statement from a responder; record what was asked."""

    def __init__(self, responder, notebook_id: str = "nb-1") -> None:
        super().__init__(
            base_url="http://127.0.0.1:1",
            token="stub",
            notebook_id=notebook_id,
            deeplink_template="siyuan://blocks/",
        )
        self._responder = responder
        self.statements: list[str] = []

    def _post(self, path: str, payload: dict):
        assert path == "/api/query/sql", f"unexpected endpoint {path}"
        stmt = payload["stmt"]
        self.statements.append(stmt)
        return self._responder(stmt)


def _responder(parent_rows: list[dict], child_rows: list[dict]):
    """Route the resolve query vs the children query by their shape.

    The children query is the one carrying LIKE; the resolve query is an exact
    `hpath =` or `id =` lookup.
    """

    def respond(stmt: str):
        return child_rows if "LIKE" in stmt else parent_rows

    return respond


class SiYuanListChildrenTest(unittest.TestCase):
    def test_direct_children_only_not_grandchildren(self) -> None:
        parent = [_doc_row("parent-id", "/1 Projects")]
        children = [
            _doc_row("c-1", "/1 Projects/Alpha"),
            _doc_row("g-1", "/1 Projects/Alpha/Deep"),
            _doc_row("c-2", "/1 Projects/Beta"),
        ]
        notes = _StubbedSiYuanNotes(_responder(parent, children))
        hits = notes.list_children("/1 Projects")
        self.assertEqual([hit.id for hit in hits], ["c-1", "c-2"])

    def test_like_wildcards_in_parent_do_not_widen_the_match(self) -> None:
        # `_` is a live single-char LIKE wildcard on SiYuan and cannot be
        # escaped, so SQL hands back `/1XProjects/...` for a `/1_Projects/%`
        # prefix. The Python re-check is what excludes it. `_MAP` is the real
        # doc name this guards - see behalfbot#208.
        parent = [_doc_row("parent-id", "/1_Projects")]
        children = [
            _doc_row("c-1", "/1_Projects/Alpha"),
            _doc_row("x-1", "/1XProjects/Impostor"),
        ]
        notes = _StubbedSiYuanNotes(_responder(parent, children))
        hits = notes.list_children("/1_Projects")
        self.assertEqual([hit.id for hit in hits], ["c-1"])

    def test_case_insensitive_like_matches_are_dropped(self) -> None:
        # sqlite LIKE is ASCII case-insensitive; the contract is case-sensitive.
        parent = [_doc_row("parent-id", "/3 Resources")]
        children = [
            _doc_row("c-1", "/3 Resources/Reading"),
            _doc_row("x-1", "/3 resources/Wrong Case"),
        ]
        notes = _StubbedSiYuanNotes(_responder(parent, children))
        hits = notes.list_children("/3 Resources")
        self.assertEqual([hit.id for hit in hits], ["c-1"])

    def test_ordering_is_code_point_ascending_regardless_of_row_order(self) -> None:
        parent = [_doc_row("parent-id", "/1 Projects")]
        children = [
            _doc_row("c-a", "/1 Projects/apple"),
            _doc_row("c-z", "/1 Projects/Zebra"),
            _doc_row("c-m", "/1 Projects/Mango"),
        ]
        notes = _StubbedSiYuanNotes(_responder(parent, children))
        hits = notes.list_children("/1 Projects")
        # Uppercase sorts before lowercase in code-point order.
        self.assertEqual([hit.title for hit in hits], ["Mango", "Zebra", "apple"])

    def test_empty_parent_returns_empty_list(self) -> None:
        parent = [_doc_row("parent-id", "/4 Archive")]
        notes = _StubbedSiYuanNotes(_responder(parent, []))
        self.assertEqual(notes.list_children("/4 Archive"), [])

    def test_nonexistent_hpath_raises(self) -> None:
        notes = _StubbedSiYuanNotes(_responder([], []))
        with self.assertRaises(SiYuanError) as ctx:
            notes.list_children("/Nope")
        self.assertIn("/Nope", str(ctx.exception))

    def test_nonexistent_block_id_raises(self) -> None:
        notes = _StubbedSiYuanNotes(_responder([], []))
        with self.assertRaises(SiYuanError) as ctx:
            notes.list_children("20260916120000-missing")
        self.assertIn("20260916120000-missing", str(ctx.exception))

    def test_block_id_parent_resolves_to_its_hpath(self) -> None:
        parent = [_doc_row("20260916120000-abcd123", "/1 Projects")]
        children = [_doc_row("c-1", "/1 Projects/Alpha")]
        notes = _StubbedSiYuanNotes(_responder(parent, children))
        hits = notes.list_children("20260916120000-abcd123")
        self.assertEqual([hit.id for hit in hits], ["c-1"])
        self.assertIn("id = '20260916120000-abcd123'", notes.statements[0])
        self.assertIn("hpath LIKE '/1 Projects/%'", notes.statements[1])

    def test_children_query_is_scoped_to_the_parents_notebook(self) -> None:
        parent = [_doc_row("parent-id", "/1 Projects", box="nb-other")]
        notes = _StubbedSiYuanNotes(_responder(parent, []))
        notes.list_children("/1 Projects")
        children_sql = notes.statements[1]
        self.assertIn("box = 'nb-other'", children_sql)
        self.assertIn("hpath NOT LIKE '/1 Projects/%/%'", children_sql)
        self.assertIn("type = 'd'", children_sql)

    def test_root_parent_uses_configured_notebook(self) -> None:
        children = [_doc_row("c-1", "/1 Projects")]
        notes = _StubbedSiYuanNotes(_responder([], children))
        hits = notes.list_children("")
        self.assertEqual([hit.id for hit in hits], ["c-1"])
        # Root needs no resolve query - there is no doc row for a notebook root.
        self.assertEqual(len(notes.statements), 1)
        self.assertIn("box = 'nb-1'", notes.statements[0])
        self.assertIn("hpath LIKE '/%'", notes.statements[0])

    def test_root_parent_without_notebook_id_raises(self) -> None:
        notes = _StubbedSiYuanNotes(_responder([], []), notebook_id="")
        with self.assertRaises(SiYuanError) as ctx:
            notes.list_children("/")
        self.assertIn("notebook_id", str(ctx.exception))

    def test_limit_applies_after_ordering(self) -> None:
        parent = [_doc_row("parent-id", "/1 Projects")]
        children = [
            _doc_row("c-z", "/1 Projects/Zulu"),
            _doc_row("c-a", "/1 Projects/Alpha"),
            _doc_row("c-m", "/1 Projects/Mike"),
        ]
        notes = _StubbedSiYuanNotes(_responder(parent, children))
        hits = notes.list_children("/1 Projects", limit=2)
        self.assertEqual([hit.title for hit in hits], ["Alpha", "Mike"])

    def test_hit_shape(self) -> None:
        parent = [_doc_row("parent-id", "/1 Projects")]
        children = [_doc_row("20260916120000-alpha01", "/1 Projects/Alpha")]
        notes = _StubbedSiYuanNotes(_responder(parent, children))
        hit = notes.list_children("/1 Projects")[0]
        self.assertIsInstance(hit, SearchHit)
        self.assertEqual(hit.title, "Alpha")
        self.assertEqual(hit.deeplink, "siyuan://blocks/20260916120000-alpha01")
        self.assertEqual(hit.raw["hpath"], "/1 Projects/Alpha")


# --------------------------------------------------------------------------
# Obsidian
# --------------------------------------------------------------------------


class ObsidianListChildrenTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="obsidian-list-children-"))
        self.vault = self._tmp / "vault"
        (self.vault / "1 Projects" / "Alpha").mkdir(parents=True)
        (self.vault / "4 Archive").mkdir()
        (self.vault / ".obsidian").mkdir()
        self.addCleanup(shutil.rmtree, self._tmp, True)

        self._write("Root Note.md", "at the vault root")
        self._write("1 Projects/Zebra.md", "---\ntags: [x]\n---\n\nzebra body")
        self._write("1 Projects/apple.md", "apple body")
        self._write("1 Projects/Mango.md", "mango body")
        self._write("1 Projects/Alpha/Deep.md", "a grandchild")
        self._write("1 Projects/notes.pdf", "not a note")
        self._write("1 Projects/board.canvas", "{}")
        self._write(".obsidian/workspace.md", "housekeeping")

        self.notes = ObsidianNotes(vault_path=str(self.vault))

    def _write(self, rel: str, body: str) -> None:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_direct_children_only_not_grandchildren(self) -> None:
        ids = [hit.id for hit in self.notes.list_children("1 Projects")]
        self.assertEqual(
            ids,
            ["1 Projects/Mango.md", "1 Projects/Zebra.md", "1 Projects/apple.md"],
        )
        self.assertNotIn("1 Projects/Alpha/Deep.md", ids)

    def test_non_note_files_and_directories_are_ignored(self) -> None:
        ids = [hit.id for hit in self.notes.list_children("1 Projects")]
        self.assertTrue(all(doc_id.endswith(".md") for doc_id in ids))
        self.assertNotIn("1 Projects/notes.pdf", ids)
        self.assertNotIn("1 Projects/board.canvas", ids)
        # The Alpha subdirectory is a folder, not a doc - never a hit.
        self.assertTrue(all("Alpha" not in doc_id for doc_id in ids))

    def test_trailing_slash_and_bare_name_agree(self) -> None:
        self.assertEqual(
            [hit.id for hit in self.notes.list_children("1 Projects/")],
            [hit.id for hit in self.notes.list_children("1 Projects")],
        )

    def test_empty_parent_means_vault_root(self) -> None:
        ids = [hit.id for hit in self.notes.list_children("")]
        self.assertEqual(ids, ["Root Note.md"])
        self.assertNotIn(".obsidian/workspace.md", ids)

    def test_empty_directory_returns_empty_list(self) -> None:
        self.assertEqual(self.notes.list_children("4 Archive"), [])

    def test_nonexistent_parent_raises(self) -> None:
        with self.assertRaises(ObsidianError) as ctx:
            self.notes.list_children("9 Nope")
        self.assertIn("9 Nope", str(ctx.exception))

    def test_parent_that_is_a_note_raises(self) -> None:
        with self.assertRaises(ObsidianError) as ctx:
            self.notes.list_children("Root Note.md")
        self.assertIn("not a directory", str(ctx.exception))

    def test_traversal_is_refused(self) -> None:
        with self.assertRaises(ObsidianError):
            self.notes.list_children("../")

    def test_read_only_vault_still_lists(self) -> None:
        read_only = ObsidianNotes(vault_path=str(self.vault), read_only=True)
        self.assertEqual(len(read_only.list_children("1 Projects")), 3)

    def test_hit_shape_strips_frontmatter_from_snippet(self) -> None:
        hits = {hit.title: hit for hit in self.notes.list_children("1 Projects")}
        zebra = hits["Zebra"]
        self.assertEqual(zebra.id, "1 Projects/Zebra.md")
        self.assertEqual(zebra.snippet, "zebra body")
        self.assertTrue(zebra.deeplink.startswith("obsidian://open?vault="))

    def test_limit_applies_after_ordering(self) -> None:
        hits = self.notes.list_children("1 Projects", limit=2)
        self.assertEqual([hit.title for hit in hits], ["Mango", "Zebra"])


# --------------------------------------------------------------------------
# Notion
# --------------------------------------------------------------------------


def _child_page(block_id: str, title: str) -> dict:
    return {
        "object": "block",
        "id": block_id,
        "type": "child_page",
        "child_page": {"title": title},
    }


def _paragraph(block_id: str, text: str) -> dict:
    return {
        "object": "block",
        "id": block_id,
        "type": "paragraph",
        "paragraph": {"rich_text": [{"plain_text": text}]},
    }


class NotionListChildrenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self._original_request = notion_module._request
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        notion_module._request = self._original_request

    def _install(self, responder) -> None:
        def fake_request(token, method, path, payload=None):
            self.requests.append((method, path, payload))
            return responder(method, path, payload)

        notion_module._request = fake_request

    def test_child_pages_only_sorted_by_title(self) -> None:
        blocks = [
            _paragraph("b-prose", "some prose on the parent page"),
            _child_page("p-zebra", "Zebra"),
            _child_page("p-apple", "apple"),
            _child_page("p-mango", "Mango"),
            {
                "object": "block",
                "id": "b-db",
                "type": "child_database",
                "child_database": {"title": "CRM"},
            },
        ]

        def responder(method, path, payload):
            return {"results": blocks, "has_more": False}

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root-page")
        hits = notes.list_children("parent-page")
        self.assertEqual([hit.title for hit in hits], ["Mango", "Zebra", "apple"])
        self.assertEqual([hit.id for hit in hits], ["p-mango", "p-zebra", "p-apple"])
        self.assertEqual(self.requests[0][0], "GET")
        self.assertIn("/blocks/parent-page/children", self.requests[0][1])

    def test_grandchildren_are_not_fetched(self) -> None:
        def responder(method, path, payload):
            if "parent-page" in path:
                return {"results": [_child_page("p-alpha", "Alpha")], "has_more": False}
            raise AssertionError(f"list_children must not recurse: {path}")

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root-page")
        hits = notes.list_children("parent-page")
        self.assertEqual([hit.id for hit in hits], ["p-alpha"])
        self.assertEqual(len(self.requests), 1)

    def test_paginates_through_all_children_before_sorting(self) -> None:
        def responder(method, path, payload):
            if "start_cursor=cursor-2" in path:
                return {"results": [_child_page("p-alpha", "Alpha")], "has_more": False}
            return {
                "results": [_child_page("p-zebra", "Zebra")],
                "has_more": True,
                "next_cursor": "cursor-2",
            }

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root-page")
        hits = notes.list_children("parent-page")
        # Alpha arrived on page two and still sorts first.
        self.assertEqual([hit.title for hit in hits], ["Alpha", "Zebra"])
        self.assertEqual(len(self.requests), 2)

    def test_empty_parent_falls_back_to_notes_root(self) -> None:
        def responder(method, path, payload):
            return {"results": [], "has_more": False}

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root-page")
        self.assertEqual(notes.list_children(""), [])
        self.assertIn("/blocks/root-page/children", self.requests[0][1])

    def test_nonexistent_parent_propagates_the_error(self) -> None:
        def responder(method, path, payload):
            raise notion_module.NotionError("Notion GET /blocks/nope/children -> HTTP 404")

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root-page")
        with self.assertRaises(notion_module.NotionError):
            notes.list_children("nope")

    def test_limit_applies_after_ordering(self) -> None:
        blocks = [
            _child_page("p-zulu", "Zulu"),
            _child_page("p-alpha", "Alpha"),
            _child_page("p-mike", "Mike"),
        ]

        def responder(method, path, payload):
            return {"results": blocks, "has_more": False}

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root-page")
        hits = notes.list_children("parent-page", limit=2)
        self.assertEqual([hit.title for hit in hits], ["Alpha", "Mike"])

    def test_hit_shape(self) -> None:
        def responder(method, path, payload):
            return {"results": [_child_page("11112222-3333", "Alpha")], "has_more": False}

        self._install(responder)
        notes = NotionNotes(token="stub", notes_root="root-page")
        hit = notes.list_children("parent-page")[0]
        self.assertIsInstance(hit, SearchHit)
        self.assertEqual(hit.deeplink, "https://www.notion.so/111122223333")
        self.assertEqual(hit.raw["type"], "child_page")


if __name__ == "__main__":
    unittest.main(verbosity=2)
