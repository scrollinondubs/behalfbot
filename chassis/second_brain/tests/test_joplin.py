#!/usr/bin/env python3
"""test_joplin.py - Unit tests for the Joplin second-brain adapter.

Everything the adapter sends goes through one chokepoint, `joplin._request`.
These tests replace it with `FakeJoplin`, a small in-memory model of the
Joplin Data API routes the adapter uses (folders, notes, folder notes,
pagination, 404s), so the adapter is exercised against API-shaped responses
with no Joplin process. The fake's shapes were taken from a live
`joplin server start` (terminal client 3.7.1).

A separate class drives the real `_request` with `urlopen` mocked, to pin the
one property the fake cannot: the token rides in the query string, and it
must never reach an exception message.

Run:
    python3 -m pytest chassis/second_brain/tests/test_joplin.py -v
"""
from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain import joplin as joplin_mod  # noqa: E402
from chassis.second_brain.base import NotImplementedDatabase  # noqa: E402
from chassis.second_brain.joplin import (  # noqa: E402
    JoplinAdapter,
    JoplinError,
    JoplinNotFoundError,
    JoplinNotes,
)
from chassis.second_brain.seed import SEED_DOCS, SENTINEL_TITLE, seed_default_content  # noqa: E402

TOKEN = "joplin-test-token-not-real"
BASE = "http://joplin.test:41184"


class FakeJoplin:
    """In-memory Joplin Data API. Ids are 32-hex like the real thing."""

    def __init__(self, page_limit: int = 100) -> None:
        self.folders: dict[str, dict] = {}
        self.notes: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.clock = 1_795_000_000_000
        self.page_limit = page_limit
        self._seq = 0

    def _id(self) -> str:
        self._seq += 1
        return f"{self._seq:032x}"

    def _tick(self) -> int:
        self.clock += 1000
        return self.clock

    def add_folder(self, title: str, parent_id: str = "") -> str:
        fid = self._id()
        self.folders[fid] = {"id": fid, "title": title, "parent_id": parent_id, "deleted_time": 0}
        return fid

    def add_note(self, title: str, body: str, parent_id: str, edited: int | None = None,
                 deleted: bool = False) -> str:
        nid = self._id()
        ts = edited if edited is not None else self._tick()
        self.notes[nid] = {
            "id": nid, "title": title, "body": body, "parent_id": parent_id,
            "updated_time": ts, "user_updated_time": ts, "created_time": ts,
            "deleted_time": ts if deleted else 0,
        }
        return nid

    @staticmethod
    def _fields(item: dict, params: dict) -> dict:
        wanted = (params.get("fields") or "id,parent_id,title").split(",")
        return {k: item[k] for k in wanted if k in item}

    def _page(self, items: list[dict], params: dict) -> dict:
        page = int(params.get("page", 1))
        limit = min(int(params.get("limit", self.page_limit)), self.page_limit)
        if params.get("order_by"):
            key = params["order_by"]
            items = sorted(items, key=lambda i: i.get(key, 0),
                           reverse=params.get("order_dir", "ASC").upper() == "DESC")
        chunk = items[(page - 1) * limit : page * limit]
        return {"items": [self._fields(i, params) for i in chunk],
                "has_more": page * limit < len(items)}

    def __call__(self, base_url, token, method, path, params=None, payload=None):
        params = dict(params or {})
        self.calls.append((method, path, params, payload))
        assert token == TOKEN
        parts = [p for p in path.split("/") if p]
        live_notes = [n for n in self.notes.values() if not n["deleted_time"]]
        if method == "GET" and parts == ["folders"]:
            return self._page(list(self.folders.values()), params)
        if method == "GET" and len(parts) == 2 and parts[0] == "folders":
            if parts[1] not in self.folders:
                raise JoplinNotFoundError(f"Joplin GET {path} -> HTTP 404: Not Found")
            return self._fields(self.folders[parts[1]], params)
        if method == "GET" and len(parts) == 3 and parts[0] == "folders" and parts[2] == "notes":
            if parts[1] not in self.folders:
                raise JoplinNotFoundError(f"Joplin GET {path} -> HTTP 404: Not Found")
            return self._page([n for n in live_notes if n["parent_id"] == parts[1]], params)
        if method == "POST" and parts == ["folders"]:
            return {"id": self.add_folder(payload["title"], payload.get("parent_id", ""))}
        if method == "GET" and parts == ["notes"]:
            return self._page(live_notes, params)
        if method == "GET" and len(parts) == 2 and parts[0] == "notes":
            note = self.notes.get(parts[1])
            if note is None:
                raise JoplinNotFoundError(f"Joplin GET {path} -> HTTP 404: Not Found")
            return self._fields(note, params)
        if method == "POST" and parts == ["notes"]:
            return {"id": self.add_note(payload["title"], payload["body"], payload["parent_id"])}
        if method == "PUT" and len(parts) == 2 and parts[0] == "notes":
            note = self.notes[parts[1]]
            note.update(payload)
            note["updated_time"] = note["user_updated_time"] = self._tick()
            return dict(note)
        raise AssertionError(f"unexpected call {method} {path}")


class JoplinTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeJoplin()
        patcher = mock.patch.object(joplin_mod, "_request", side_effect=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.api.add_folder("Second Brain")
        self.notes = JoplinNotes(BASE, TOKEN, "Second Brain")

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _, _ in self.api.calls if m != "GET"]


class TestCreateDoc(JoplinTestCase):
    def test_empty_parent_writes_into_the_root_notebook(self):
        doc_id = self.notes.create_doc("", "Brief", "# Brief\nbody")
        note = self.api.notes[doc_id]
        self.assertEqual(note["parent_id"], self.root)
        self.assertEqual((note["title"], note["body"]), ("Brief", "# Brief\nbody"))

    def test_title_path_creates_missing_notebooks_once(self):
        first = self.notes.create_doc("Projects/Client X", "Kickoff", "a")
        second = self.notes.create_doc("/Projects/Client X/", "Followup", "b")
        projects = [f for f in self.api.folders.values() if f["title"] == "Projects"]
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["parent_id"], "")
        client = [f for f in self.api.folders.values() if f["title"] == "Client X"]
        self.assertEqual(len(client), 1)
        self.assertEqual(client[0]["parent_id"], projects[0]["id"])
        self.assertEqual(self.api.notes[first]["parent_id"], client[0]["id"])
        self.assertEqual(self.api.notes[second]["parent_id"], client[0]["id"])

    def test_notebook_id_parent_is_used_as_is(self):
        fid = self.api.add_folder("Areas")
        doc_id = self.notes.create_doc(fid, "Health", "x")
        self.assertEqual(self.api.notes[doc_id]["parent_id"], fid)

    def test_unknown_notebook_id_raises_instead_of_writing(self):
        with self.assertRaises(JoplinError):
            self.notes.create_doc("f" * 32, "X", "y")
        self.assertEqual(self.writes(), [])

    def test_duplicate_title_in_same_notebook_is_refused(self):
        self.notes.create_doc("", "Priorities", "mine")
        with self.assertRaisesRegex(JoplinError, "already exists"):
            self.notes.create_doc("", "Priorities", "seed copy")
        self.assertEqual(
            [n["body"] for n in self.api.notes.values() if n["title"] == "Priorities"], ["mine"]
        )

    def test_same_title_in_another_notebook_is_fine(self):
        self.notes.create_doc("", "Notes", "a")
        self.notes.create_doc("Projects", "Notes", "b")

    def test_ambiguous_sibling_notebooks_are_refused(self):
        self.api.add_folder("Projects")
        self.api.add_folder("Projects")
        with self.assertRaisesRegex(JoplinError, "ambiguous"):
            self.notes.create_doc("Projects", "X", "y")

    def test_empty_title_raises(self):
        with self.assertRaises(JoplinError):
            self.notes.create_doc("", "  ", "y")

    def test_no_root_configured_raises_on_empty_parent(self):
        notes = JoplinNotes(BASE, TOKEN, "")
        with self.assertRaisesRegex(JoplinError, "root notebook"):
            notes.create_doc("", "X", "y")


class TestReadAppend(JoplinTestCase):
    def test_read_returns_body(self):
        nid = self.api.add_note("T", "hello", self.root)
        self.assertEqual(self.notes.read_doc(nid), "hello")
        self.assertEqual(self.notes.read_doc_source(nid), "hello")

    def test_append_adds_newline_only_when_missing(self):
        nid = self.api.add_note("T", "line one", self.root)
        self.notes.append_to_doc(nid, "line two")
        self.assertEqual(self.api.notes[nid]["body"], "line one\nline two")
        self.notes.append_to_doc(nid, "line three\n")
        self.notes.append_to_doc(nid, "four")
        self.assertEqual(self.api.notes[nid]["body"], "line one\nline two\nline three\nfour")

    def test_missing_note_raises(self):
        with self.assertRaisesRegex(JoplinError, "not found"):
            self.notes.read_doc("a" * 32)

    def test_trashed_note_raises(self):
        nid = self.api.add_note("T", "x", self.root, deleted=True)
        with self.assertRaisesRegex(JoplinError, "trash"):
            self.notes.read_doc(nid)

    def test_non_id_is_refused_before_any_request(self):
        for bad in ("../folders", "Priorities", "A" * 32, ""):
            with self.assertRaises(JoplinError):
                self.notes.read_doc(bad)
        self.assertEqual(self.api.calls, [])


class TestLinksAndDeeplinks(JoplinTestCase):
    def test_link_blocks_appends_a_native_note_link(self):
        src = self.api.add_note("Map", "# Map", self.root)
        dst = self.api.add_note("Plan [v2]", "x", self.root)
        self.notes.link_blocks(src, dst)
        self.assertIn(f"[Plan [v2\\]](:/{dst})", self.api.notes[src]["body"])

    def test_deeplink_is_the_desktop_callback_url(self):
        nid = "0123456789abcdef0123456789abcdef"
        self.assertEqual(self.notes.get_deeplink(nid),
                         f"joplin://x-callback-url/openNote?id={nid}")

    def test_deeplink_template_override(self):
        notes = JoplinNotes(BASE, TOKEN, "Second Brain", deeplink_template="https://x/?id=")
        self.assertEqual(notes.get_deeplink("ab" * 16), "https://x/?id=" + "ab" * 16)


class TestSearch(JoplinTestCase):
    def test_finds_a_note_created_a_moment_ago(self):
        # The point of searching client-side: Joplin's own index would not
        # have this yet, and the seeder's sentinel check depends on it.
        nid = self.notes.create_doc("", SENTINEL_TITLE, "marker")
        hits = self.notes.search(SENTINEL_TITLE)
        self.assertEqual([h.id for h in hits], [nid])
        self.assertFalse(any(m == "GET" and p == "/search" for m, p, _, _ in self.api.calls))

    def test_title_hits_outrank_body_hits_and_case_is_ignored(self):
        body_only = self.api.add_note("Notes", "about the ROADMAP", self.root)
        title_only = self.api.add_note("Roadmap", "nothing", self.root)
        both = self.api.add_note("roadmap v2", "roadmap again", self.root)
        hits = self.notes.search("roadmap")
        self.assertEqual([h.id for h in hits], [both, title_only, body_only])
        self.assertEqual([h.score for h in hits], [3.0, 2.0, 1.0])

    def test_special_characters_are_literal(self):
        target = self.api.add_note("_MAP", "foo:bar -x 50%", self.root)
        self.api.add_note("MAPX", "nothing", self.root)
        self.assertEqual([h.id for h in self.notes.search("_MAP")], [target])
        self.assertEqual([h.id for h in self.notes.search("foo:bar")], [target])
        self.assertEqual([h.id for h in self.notes.search("-x 50%")], [target])

    def test_trashed_notes_are_not_found(self):
        self.api.add_note("Gone", "x", self.root, deleted=True)
        self.assertEqual(self.notes.search("Gone"), [])

    def test_paginates_past_one_page(self):
        self.api.page_limit = 3
        ids = [self.api.add_note(f"note {i}", "needle", self.root) for i in range(8)]
        hits = self.notes.search("needle", limit=50)
        self.assertEqual(sorted(h.id for h in hits), sorted(ids))

    def test_snippet_centres_on_the_match(self):
        self.api.add_note("Long", "x" * 500 + " needle " + "y" * 500, self.root)
        (hit,) = self.notes.search("needle")
        self.assertIn("needle", hit.snippet)
        self.assertLessEqual(len(hit.snippet), 200)


class TestListChildren(JoplinTestCase):
    def test_direct_notes_only_sorted_code_point(self):
        sub = self.api.add_folder("Sub", self.root)
        self.api.add_note("zebra", "", self.root)
        self.api.add_note("Apple", "", self.root)
        self.api.add_note("Zebra", "", self.root)
        self.api.add_note("nested", "", sub)
        titles = [h.title for h in self.notes.list_children("")]
        self.assertEqual(titles, ["Apple", "Zebra", "zebra"])

    def test_title_path_parent(self):
        projects = self.api.add_folder("Projects")
        nid = self.api.add_note("Launch", "body", projects)
        (hit,) = self.notes.list_children("Projects/")
        self.assertEqual((hit.id, hit.snippet), (nid, "body"))

    def test_empty_notebook_returns_empty_list(self):
        self.api.add_folder("Empty")
        self.assertEqual(self.notes.list_children("Empty"), [])

    def test_missing_notebook_raises_and_creates_nothing(self):
        with self.assertRaisesRegex(JoplinError, "not found"):
            self.notes.list_children("Nope/Deeper")
        self.assertEqual(self.writes(), [])

    def test_limit_applies_after_ordering(self):
        for title in ("c", "a", "b"):
            self.api.add_note(title, "", self.root)
        self.assertEqual([h.title for h in self.notes.list_children("", limit=2)], ["a", "b"])


class TestListRecent(JoplinTestCase):
    def _ms(self, dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    def test_window_newest_first_with_hpath(self):
        now = datetime(2026, 11, 21, 12, 0, 0)
        sub = self.api.add_folder("Projects")
        old = self.api.add_note("old", "x", self.root, edited=self._ms(now - timedelta(days=2)))
        inside1 = self.api.add_note("one", "x", sub, edited=self._ms(now - timedelta(hours=3)))
        inside2 = self.api.add_note("two", "x", self.root, edited=self._ms(now - timedelta(hours=1)))
        future = self.api.add_note("future", "x", self.root, edited=self._ms(now + timedelta(hours=1)))
        hits = self.notes.list_recent(now - timedelta(days=1), now)
        self.assertEqual([h.id for h in hits], [inside2, inside1])
        self.assertNotIn(old, [h.id for h in hits])
        self.assertNotIn(future, [h.id for h in hits])
        self.assertEqual(hits[1].raw["hpath"], "/Projects/one")
        self.assertEqual(hits[0].raw["hpath"], "/Second Brain/two")
        self.assertTrue(hits[0].raw["updated"].startswith("2026-11-21"))

    def test_min_content_len_and_limit(self):
        now = datetime(2026, 11, 21, 12, 0, 0)
        for i, body in enumerate(["short", "a" * 50, "b" * 60, "c" * 70]):
            self.api.add_note(f"n{i}", body, self.root, edited=self._ms(now - timedelta(minutes=i + 1)))
        hits = self.notes.list_recent(now - timedelta(hours=1), now, min_content_len=40, limit=2)
        self.assertEqual([h.title for h in hits], ["n1", "n2"])
        self.assertEqual(hits[0].raw["body_len"], 50)

    def test_stops_paging_once_past_the_window(self):
        self.api.page_limit = 2
        now = datetime(2026, 11, 21, 12, 0, 0)
        for i in range(10):
            self.api.add_note(f"n{i}", "x", self.root, edited=self._ms(now - timedelta(days=i)))
        self.notes.list_recent(now - timedelta(hours=36), now + timedelta(seconds=1))
        note_pages = [c for c in self.api.calls if c[0] == "GET" and c[1] == "/notes"]
        self.assertLessEqual(len(note_pages), 2)


class TestSeedOnJoplin(JoplinTestCase):
    def test_seed_writes_once_and_rerun_is_a_noop(self):
        first = seed_default_content(self.notes)
        self.assertTrue(first["seeded"])
        self.assertEqual(len(first["created"]), len(SEED_DOCS) + 1)
        self.assertNotIn("failed", first)
        second = seed_default_content(self.notes)
        self.assertEqual(second["reason"], "sentinel_exists")
        titles = [h.title for h in self.notes.list_children("")]
        self.assertEqual(len(titles), len(SEED_DOCS) + 1)


class TestAdapter(unittest.TestCase):
    def test_shape(self):
        adapter = JoplinAdapter(BASE, TOKEN, "Second Brain")
        self.assertEqual(adapter.backend, "joplin")
        self.assertIsInstance(adapter.database, NotImplementedDatabase)
        with self.assertRaises(NotImplementedError):
            adapter.database.query()


class TestRequestTokenHygiene(unittest.TestCase):
    """Drives the real `_request` with urlopen mocked."""

    def _http_error(self, code: int, body: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            f"{BASE}/notes?token={TOKEN}", code, "err", {}, io.BytesIO(body.encode())
        )

    def test_token_goes_in_the_query_string(self):
        resp = mock.MagicMock()
        resp.read.return_value = b'{"items": [], "has_more": false}'
        with mock.patch.object(joplin_mod.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = resp
            joplin_mod._request(BASE, TOKEN, "GET", "/notes", {"fields": "id"})
        req = urlopen.call_args[0][0]
        self.assertIn(f"token={TOKEN}", req.full_url)
        self.assertTrue(req.full_url.startswith(f"{BASE}/notes?"))

    def test_http_error_never_carries_the_token(self):
        body = json.dumps({"error": f"Invalid token {TOKEN}\n    at Api.checkToken_"})
        with mock.patch.object(joplin_mod.urllib.request, "urlopen",
                               side_effect=self._http_error(403, body)):
            with self.assertRaises(JoplinError) as ctx:
                joplin_mod._request(BASE, TOKEN, "GET", "/notes")
        message = str(ctx.exception)
        self.assertNotIn(TOKEN, message)
        self.assertIn("HTTP 403", message)
        self.assertIn("wrong JOPLIN_TOKEN", message)
        self.assertNotIn("checkToken_", message)  # stack trace trimmed
        self.assertIsNone(ctx.exception.__cause__)

    def test_404_is_the_not_found_subclass(self):
        with mock.patch.object(joplin_mod.urllib.request, "urlopen",
                               side_effect=self._http_error(404, '{"error":"Not Found"}')):
            with self.assertRaises(JoplinNotFoundError):
                joplin_mod._request(BASE, TOKEN, "GET", "/notes/x")

    def test_connection_error_never_carries_the_token(self):
        err = urllib.error.URLError(f"refused for {BASE}/notes?token={TOKEN}")
        with mock.patch.object(joplin_mod.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(JoplinError) as ctx:
                joplin_mod._request(BASE, TOKEN, "GET", "/notes")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("request failed", str(ctx.exception))

    def test_empty_body_is_none(self):
        resp = mock.MagicMock()
        resp.read.return_value = b""
        with mock.patch.object(joplin_mod.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = resp
            self.assertIsNone(joplin_mod._request(BASE, TOKEN, "DELETE", "/notes/x"))


if __name__ == "__main__":
    unittest.main()
