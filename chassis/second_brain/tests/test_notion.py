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
    NotionPartialWriteError,
    _blocks_to_markdown,
    _chunk_blocks,
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


class TestChunkedCreate(NotionNotesTestCase):
    """THE Phase 1 bug: Notion caps block children at 100 per request, and an
    unchunked briefing-sized create_doc was an opaque HTTP 400. These tests
    assert the >100-block path explicitly."""

    def test_create_over_100_blocks_chunks_into_create_plus_appends(self):
        self.response = {"id": "page-1"}
        body = "\n".join(f"line {i}" for i in range(250))
        new_id = self.notes.create_doc("", "Morning Briefing", body)

        self.assertEqual(new_id, "page-1")
        self.assertEqual(len(self.calls), 3)

        _, method, path, payload = self.calls[0]
        self.assertEqual((method, path), ("POST", "/pages"))
        self.assertEqual(len(payload["children"]), 100)
        first_text = payload["children"][0]["paragraph"]["rich_text"][0]["text"]["content"]
        self.assertEqual(first_text, "line 0")

        _, method, path, payload = self.calls[1]
        self.assertEqual((method, path), ("PATCH", "/blocks/page-1/children"))
        self.assertEqual(len(payload["children"]), 100)
        self.assertEqual(
            payload["children"][0]["paragraph"]["rich_text"][0]["text"]["content"],
            "line 100",
        )

        _, method, path, payload = self.calls[2]
        self.assertEqual((method, path), ("PATCH", "/blocks/page-1/children"))
        self.assertEqual(len(payload["children"]), 50)
        self.assertEqual(
            payload["children"][-1]["paragraph"]["rich_text"][0]["text"]["content"],
            "line 249",
        )

    def test_create_exactly_100_blocks_is_a_single_request(self):
        self.response = {"id": "page-1"}
        self.notes.create_doc("", "T", "\n".join(f"l{i}" for i in range(100)))
        self.assertEqual(len(self.calls), 1)

    def test_create_101_blocks_appends_one_chunk_of_one(self):
        self.response = {"id": "page-1"}
        self.notes.create_doc("", "T", "\n".join(f"l{i}" for i in range(101)))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.calls[1][3]["children"]), 1)

    def test_no_block_ever_exceeds_100_children_per_request(self):
        self.response = {"id": "page-1"}
        self.notes.create_doc("", "T", "\n".join(f"l{i}" for i in range(731)))
        for _, _, _, payload in self.calls:
            self.assertLessEqual(len(payload["children"]), 100)

    def test_partial_create_failure_reports_progress_and_page_id(self):
        # Create succeeds, first append succeeds, second append blows up. The
        # page now holds 2 of 3 chunks - the error must say so, or a retry is
        # blind and either duplicates content or deletes good data.
        responses = iter(
            [
                {"id": "page-1"},
                {},
                NotionError("Notion PATCH /blocks/page-1/children → HTTP 500: boom"),
            ]
        )

        def scripted(token, method, path, payload=None):
            self.calls.append((token, method, path, payload))
            step = next(responses)
            if isinstance(step, Exception):
                raise step
            return step

        with mock.patch.object(notion_mod, "_request", side_effect=scripted):
            with self.assertRaises(NotionPartialWriteError) as ctx:
                self.notes.create_doc("", "T", "\n".join(f"l{i}" for i in range(250)))

        exc = ctx.exception
        self.assertEqual(exc.doc_id, "page-1")
        self.assertEqual(exc.chunks_written, 2)
        self.assertEqual(exc.chunks_total, 3)
        self.assertIn("2 of 3", str(exc))
        self.assertIn("page-1", str(exc))
        self.assertIsInstance(exc, NotionError)  # callers catching NotionError still see it


class TestChunkedAppend(NotionNotesTestCase):
    def test_append_over_100_blocks_issues_sequential_patches(self):
        self.notes.append_to_doc("doc-1", "\n".join(f"line {i}" for i in range(250)))
        self.assertEqual(len(self.calls), 3)
        for _, method, path, payload in self.calls:
            self.assertEqual((method, path), ("PATCH", "/blocks/doc-1/children"))
            self.assertLessEqual(len(payload["children"]), 100)
        # Order preserved - chunk boundaries must not scramble the document.
        self.assertEqual(
            self.calls[2][3]["children"][-1]["paragraph"]["rich_text"][0]["text"]["content"],
            "line 249",
        )

    def test_append_blank_content_makes_no_request(self):
        # Notion 400s on an empty children array; an all-blank append is a no-op.
        self.notes.append_to_doc("doc-1", "\n\n\n")
        self.assertEqual(self.calls, [])

    def test_partial_append_failure_reports_progress(self):
        attempts = {"n": 0}

        def flaky(token, method, path, payload=None):
            attempts["n"] += 1
            if attempts["n"] == 2:
                raise NotionError("HTTP 502")
            return {}

        with mock.patch.object(notion_mod, "_request", side_effect=flaky):
            with self.assertRaises(NotionPartialWriteError) as ctx:
                self.notes.append_to_doc("doc-1", "\n".join(f"l{i}" for i in range(250)))

        self.assertEqual(ctx.exception.chunks_written, 1)
        self.assertEqual(ctx.exception.chunks_total, 3)
        self.assertIn("1 of 3", str(ctx.exception))


class TestChunkBlocks(unittest.TestCase):
    def test_empty_list_yields_no_chunks(self):
        self.assertEqual(_chunk_blocks([]), [])

    def test_chunk_sizes_and_order(self):
        blocks = [{"i": i} for i in range(205)]
        chunks = _chunk_blocks(blocks)
        self.assertEqual([len(c) for c in chunks], [100, 100, 5])
        self.assertEqual([b["i"] for c in chunks for b in c], list(range(205)))


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

    def test_long_line_splits_into_2000_char_rich_text_parts(self):
        # Notion caps a single rich_text content field at 2000 chars. A long
        # line used to be silently truncated there; now it splits into
        # multiple parts within the same paragraph and nothing is lost.
        blocks = _markdown_to_blocks("x" * 5000)
        self.assertEqual(len(blocks), 1)
        parts = blocks[0]["paragraph"]["rich_text"]
        self.assertEqual([len(p["text"]["content"]) for p in parts], [2000, 2000, 1000])
        self.assertEqual("".join(p["text"]["content"] for p in parts), "x" * 5000)

    def test_short_line_stays_a_single_part(self):
        blocks = _markdown_to_blocks("hello")
        self.assertEqual(len(blocks[0]["paragraph"]["rich_text"]), 1)

    def test_pathological_line_caps_at_100_parts(self):
        # Notion also caps rich_text arrays at 100 elements per block. A
        # 200k+ char single line truncates there rather than 400ing.
        blocks = _markdown_to_blocks("x" * 300_000)
        self.assertEqual(len(blocks[0]["paragraph"]["rich_text"]), 100)

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


def _http_error(code: int, body: bytes = b"{}", retry_after: str | None = None) -> urllib.error.HTTPError:
    import email.message

    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    err = urllib.error.HTTPError(
        url="https://api.notion.com/v1/pages",
        code=code,
        msg="err",
        hdrs=headers,
        fp=None,
    )
    err.read = lambda: body  # type: ignore[method-assign]
    return err


class TestRateLimitRetry(unittest.TestCase):
    """429 handling in _request: bounded retry honouring Retry-After.

    Retrying 429 is safe for writes because Notion's limiter rejects the
    request BEFORE executing it - a rate-limited append never half-landed.
    That is also why ONLY 429 retries: a 5xx or network error may have
    applied the write, and retrying those would double-write.
    """

    def test_429_retries_and_honours_retry_after(self):
        sleeps: list[float] = []
        outcomes = iter([_http_error(429, retry_after="7"), _http_error(429, retry_after="2"), None])

        class FakeResp:
            def read(self):
                return b'{"id": "ok"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            step = next(outcomes)
            if step is not None:
                raise step
            return FakeResp()

        with mock.patch.object(notion_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            with mock.patch.object(notion_mod.time, "sleep", side_effect=sleeps.append):
                result = notion_mod._request(TOKEN, "PATCH", "/blocks/x/children", {"children": []})

        self.assertEqual(result, {"id": "ok"})
        self.assertEqual(sleeps, [7.0, 2.0])

    def test_429_exhaustion_raises_with_attempt_count(self):
        def always_429(req, timeout=None):
            raise _http_error(429, body=b'{"message":"rate limited"}', retry_after="1")

        with mock.patch.object(notion_mod.urllib.request, "urlopen", side_effect=always_429):
            with mock.patch.object(notion_mod.time, "sleep") as sleep:
                with self.assertRaises(NotionError) as ctx:
                    notion_mod._request(TOKEN, "GET", "/pages/x")

        self.assertEqual(sleep.call_count, notion_mod._MAX_RATE_LIMIT_RETRIES)
        message = str(ctx.exception)
        self.assertIn("429", message)
        self.assertIn(f"{notion_mod._MAX_RATE_LIMIT_RETRIES + 1} attempts", message)
        self.assertIn("rate limited", message)

    def test_missing_retry_after_falls_back_to_backoff(self):
        sleeps: list[float] = []

        def always_429(req, timeout=None):
            raise _http_error(429)

        with mock.patch.object(notion_mod.urllib.request, "urlopen", side_effect=always_429):
            with mock.patch.object(notion_mod.time, "sleep", side_effect=sleeps.append):
                with self.assertRaises(NotionError):
                    notion_mod._request(TOKEN, "GET", "/pages/x")

        self.assertEqual(sleeps, [1.0, 2.0, 3.0, 4.0, 5.0])

    def test_absurd_retry_after_is_capped(self):
        sleeps: list[float] = []
        outcomes = iter([_http_error(429, retry_after="86400")])

        class FakeResp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            try:
                raise next(outcomes)
            except StopIteration:
                return FakeResp()

        with mock.patch.object(notion_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            with mock.patch.object(notion_mod.time, "sleep", side_effect=sleeps.append):
                notion_mod._request(TOKEN, "GET", "/pages/x")

        self.assertEqual(sleeps, [notion_mod._MAX_RETRY_AFTER_SECONDS])

    def test_non_429_http_errors_are_not_retried(self):
        attempts = {"n": 0}

        def fail_500(req, timeout=None):
            attempts["n"] += 1
            raise _http_error(500, body=b'{"message":"server error"}')

        with mock.patch.object(notion_mod.urllib.request, "urlopen", side_effect=fail_500):
            with mock.patch.object(notion_mod.time, "sleep") as sleep:
                with self.assertRaises(NotionError):
                    notion_mod._request(TOKEN, "PATCH", "/blocks/x/children", {"children": []})

        self.assertEqual(attempts["n"], 1)
        sleep.assert_not_called()

    def test_network_errors_are_not_retried(self):
        attempts = {"n": 0}

        def offline(req, timeout=None):
            attempts["n"] += 1
            raise urllib.error.URLError("offline")

        with mock.patch.object(notion_mod.urllib.request, "urlopen", side_effect=offline):
            with self.assertRaises(NotionError):
                notion_mod._request(TOKEN, "POST", "/pages", {})

        self.assertEqual(attempts["n"], 1)


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

    def test_chunked_create_over_100_blocks_live(self):
        # The Phase 1 bug: >100 blocks in one request was HTTP 400. Prove the
        # chunked path against the real API and read the whole doc back.
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = "\n".join(f"chunk test line {i}" for i in range(120))
        doc_id = self.notes.create_doc("", f"adapter chunk test {stamp}", body)
        self.assertTrue(doc_id, "create_doc returned no page id")
        readback = self.notes.read_doc(doc_id)
        self.assertIn("chunk test line 0", readback)
        # read_doc fetches the first 100 blocks only (documented V1 limit), so
        # assert block 99 rather than 119 - the write itself is what is under test.
        self.assertIn("chunk test line 99", readback)

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
