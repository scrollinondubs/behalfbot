#!/usr/bin/env python3
"""test_notion.py - Unit tests for the Notion second-brain adapter.

The Notion adapter talks to a remote HTTP API, so unlike the Obsidian tests
there is no filesystem to build a fixture from. Everything funnels through one
chokepoint - `notion._request` - so these tests patch that and assert on the
exact (method, path, payload) the adapter emits, plus how it maps responses
back.

That gives real contract coverage with no network and no account. What it
deliberately does NOT prove is that Notion accepts the payloads we send. For
that, the bottom of this file has a live section that only runs when
NOTION_TOKEN and NOTION_TEST_PAGE_ID are set - see `LiveNotionTestCase`. CI
runs the mocked tests; the live ones are opt-in.

Run:
    python3 -m pytest chassis/second_brain/tests/test_notion.py -v
    # including the live tests:
    NOTION_TOKEN=... NOTION_TEST_PAGE_ID=... python3 -m pytest chassis/second_brain/tests/test_notion.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain import notion as notion_mod  # noqa: E402
from chassis.second_brain.notion import (  # noqa: E402
    NotionError,
    NotionNotes,
    _blocks_to_markdown,
    _markdown_to_blocks,
)

TOKEN = "ntn_test_token_not_real"
ROOT_PAGE = "11111111-2222-3333-4444-555555555555"


def _para(text: str) -> dict:
    """A Notion paragraph block as the API returns it (plain_text populated)."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "plain_text": text}]},
    }


class NotionNotesTestCase(unittest.TestCase):
    """Patches notion._request and captures every call the adapter makes."""

    def setUp(self) -> None:
        self.calls: list[tuple] = []
        self.response: dict = {}

        def fake_request(token, method, path, payload=None):
            self.calls.append((token, method, path, payload))
            return self.response

        patcher = mock.patch.object(notion_mod, "_request", side_effect=fake_request)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.notes = NotionNotes(TOKEN, ROOT_PAGE)

    @property
    def last(self) -> tuple:
        return self.calls[-1]


class TestCreateDoc(NotionNotesTestCase):
    def test_posts_to_pages_with_parent_and_title(self):
        self.response = {"id": "abc-123"}
        new_id = self.notes.create_doc("", "Morning Briefing", "line one\n\nline two")

        token, method, path, payload = self.last
        self.assertEqual(token, TOKEN)
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/pages")
        self.assertEqual(new_id, "abc-123")
        # Empty parent must fall back to the configured notes root, not to "".
        self.assertEqual(payload["parent"], {"page_id": ROOT_PAGE})
        title = payload["properties"]["title"][0]["text"]["content"]
        self.assertEqual(title, "Morning Briefing")

    def test_explicit_parent_overrides_root(self):
        self.response = {"id": "x"}
        self.notes.create_doc("other-parent", "T", "body")
        self.assertEqual(self.last[3]["parent"], {"page_id": "other-parent"})

    def test_title_truncated_at_200(self):
        self.response = {"id": "x"}
        self.notes.create_doc("", "T" * 500, "body")
        title = self.last[3]["properties"]["title"][0]["text"]["content"]
        self.assertEqual(len(title), 200)

    def test_blank_lines_do_not_become_blocks(self):
        self.response = {"id": "x"}
        self.notes.create_doc("", "T", "one\n\n\ntwo\n")
        self.assertEqual(len(self.last[3]["children"]), 2)

    def test_missing_id_in_response_returns_empty_string(self):
        # The adapter uses .get("id", ""). Assert that stays true - a silent ""
        # is easier to trace than a KeyError from deep inside a heartbeat.
        self.response = {}
        self.assertEqual(self.notes.create_doc("", "T", "b"), "")


class TestAppendAndRead(NotionNotesTestCase):
    def test_append_patches_block_children(self):
        self.notes.append_to_doc("doc-1", "appended line")
        _, method, path, payload = self.last
        self.assertEqual(method, "PATCH")
        self.assertEqual(path, "/blocks/doc-1/children")
        self.assertEqual(len(payload["children"]), 1)

    def test_read_doc_gets_children_and_returns_markdown(self):
        self.response = {"results": [_para("first"), _para("second")]}
        text = self.notes.read_doc("doc-1")
        _, method, path, payload = self.last
        self.assertEqual(method, "GET")
        self.assertIn("/blocks/doc-1/children", path)
        self.assertIsNone(payload)
        self.assertEqual(text, "first\nsecond")

    def test_read_doc_empty_page_returns_empty_string(self):
        self.response = {"results": []}
        self.assertEqual(self.notes.read_doc("doc-1"), "")


class TestDeeplink(NotionNotesTestCase):
    def test_dashes_stripped(self):
        link = self.notes.get_deeplink("1111aaaa-2222-3333-4444-555566667777")
        self.assertEqual(link, "https://www.notion.so/1111aaaa222233334444555566667777")
        self.assertNotIn("-", link.rsplit("/", 1)[1])


class TestLinkBlocks(NotionNotesTestCase):
    def test_emits_link_to_page_block(self):
        self.notes.link_blocks("from-1", "to-2")
        _, method, path, payload = self.last
        self.assertEqual(method, "PATCH")
        self.assertEqual(path, "/blocks/from-1/children")
        block = payload["children"][0]
        self.assertEqual(block["type"], "link_to_page")
        self.assertEqual(block["link_to_page"], {"type": "page_id", "page_id": "to-2"})


class TestSearch(NotionNotesTestCase):
    def _page(self, pid: str, title: str) -> dict:
        return {
            "id": pid,
            "properties": {
                "title": {"type": "title", "title": [{"plain_text": title}]},
            },
        }

    def test_posts_search_filtered_to_pages(self):
        self.response = {"results": []}
        self.notes.search("quinta", limit=5)
        _, method, path, payload = self.last
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/search")
        self.assertEqual(payload["query"], "quinta")
        self.assertEqual(payload["page_size"], 5)
        self.assertEqual(payload["filter"], {"value": "page", "property": "object"})

    def test_maps_results_to_search_hits(self):
        self.response = {"results": [self._page("aaaa-bbbb", "Quinta notes")]}
        hits = self.notes.search("quinta")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].id, "aaaa-bbbb")
        self.assertEqual(hits[0].title, "Quinta notes")
        self.assertEqual(hits[0].deeplink, "https://www.notion.so/aaaabbbb")

    def test_limit_is_enforced_client_side_too(self):
        # Notion honours page_size, but the adapter also slices. If the API ever
        # over-returns, the caller's limit must still hold.
        self.response = {"results": [self._page(f"id-{i}", f"t{i}") for i in range(10)]}
        self.assertEqual(len(self.notes.search("q", limit=3)), 3)


class TestMarkdownConversion(unittest.TestCase):
    def test_blank_lines_skipped(self):
        self.assertEqual(len(_markdown_to_blocks("a\n\n\nb")), 2)

    def test_long_line_truncated_at_2000(self):
        blocks = _markdown_to_blocks("x" * 5000)
        content = blocks[0]["paragraph"]["rich_text"][0]["text"]["content"]
        self.assertEqual(len(content), 2000)

    def test_paragraph_roundtrip(self):
        # _markdown_to_blocks emits text.content; the API echoes plain_text.
        # Simulate that echo so the round-trip is honest rather than trivially true.
        blocks = _markdown_to_blocks("hello world")
        echoed = [
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"plain_text": b["paragraph"]["rich_text"][0]["text"]["content"]}
                    ]
                },
            }
            for b in blocks
        ]
        self.assertEqual(_blocks_to_markdown(echoed), "hello world")

    def test_headings_render_with_hashes(self):
        blocks = [
            {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Top"}]}},
            {"type": "heading_3", "heading_3": {"rich_text": [{"plain_text": "Deep"}]}},
        ]
        self.assertEqual(_blocks_to_markdown(blocks), "# Top\n### Deep")

    def test_list_items_render_with_markers(self):
        blocks = [
            {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "a"}]}},
            {"type": "numbered_list_item", "numbered_list_item": {"rich_text": [{"plain_text": "b"}]}},
        ]
        self.assertEqual(_blocks_to_markdown(blocks), "- a\n1. b")

    def test_unknown_block_type_does_not_crash(self):
        # Notion adds block types over time. An unrecognised one must degrade,
        # not raise - reading a briefing should never die on a callout block.
        blocks = [{"type": "callout", "callout": {"rich_text": [{"plain_text": "note"}]}}]
        self.assertEqual(_blocks_to_markdown(blocks), "note")

    def test_block_with_no_type_does_not_crash(self):
        self.assertEqual(_blocks_to_markdown([{}]), "")


class TestRequestErrorMapping(unittest.TestCase):
    """_request must convert urllib failures into NotionError, not leak them."""

    def test_http_error_becomes_notion_error_with_status(self):
        err = urllib.error.HTTPError(
            url="https://api.notion.com/v1/pages",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        err.read = lambda: b'{"message":"API token is invalid."}'  # type: ignore[method-assign]
        with mock.patch.object(notion_mod.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(NotionError) as ctx:
                notion_mod._request(TOKEN, "POST", "/pages", {})
        self.assertIn("401", str(ctx.exception))
        self.assertIn("API token is invalid", str(ctx.exception))

    def test_url_error_becomes_notion_error(self):
        with mock.patch.object(
            notion_mod.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
        ):
            with self.assertRaises(NotionError):
                notion_mod._request(TOKEN, "GET", "/pages/x")

    def test_sends_required_headers(self):
        captured = {}

        class FakeResp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            captured["method"] = req.get_method()
            return FakeResp()

        with mock.patch.object(notion_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            notion_mod._request(TOKEN, "POST", "/pages", {"a": 1})

        # urllib title-cases header keys.
        self.assertEqual(captured["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(captured["headers"]["Notion-version"], notion_mod.NOTION_API_VERSION)
        self.assertEqual(captured["method"], "POST")


@unittest.skipUnless(
    os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_TEST_PAGE_ID"),
    "live Notion tests need NOTION_TOKEN and NOTION_TEST_PAGE_ID",
)
class LiveNotionTestCase(unittest.TestCase):
    """Opt-in tests against a real Notion workspace.

    These catch what mocks cannot: Notion rejecting a payload we believe is
    well-formed. Credentials live in Vaultwarden under `notion-adapter-test`;
    export them before running.

    Each test writes into the configured test page and leaves its output there.
    That is deliberate - the page is a scratch workspace, and being able to
    eyeball what the adapter actually produced is worth more than tidiness.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.notes = NotionNotes(
            os.environ["NOTION_TOKEN"], os.environ["NOTION_TEST_PAGE_ID"]
        )

    def test_create_read_append_roundtrip(self):
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        doc_id = self.notes.create_doc("", f"adapter test {stamp}", "first line")
        self.assertTrue(doc_id, "create_doc returned no page id")

        self.assertIn("first line", self.notes.read_doc(doc_id))

        self.notes.append_to_doc(doc_id, "second line")
        body = self.notes.read_doc(doc_id)
        self.assertIn("first line", body)
        self.assertIn("second line", body)

    def test_search_returns_wellformed_hits(self):
        hits = self.notes.search("adapter test", limit=5)
        self.assertIsInstance(hits, list)
        for hit in hits:
            self.assertTrue(hit.deeplink.startswith("https://www.notion.so/"))
            self.assertTrue(hit.id)

    def test_bad_token_raises_notion_error(self):
        bad = NotionNotes("ntn_definitely_invalid", os.environ["NOTION_TEST_PAGE_ID"])
        with self.assertRaises(NotionError):
            bad.search("anything")


if __name__ == "__main__":
    unittest.main(verbosity=2)
