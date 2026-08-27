#!/usr/bin/env python3
"""test_seed.py - seed_default_content() is idempotent and backend-agnostic.

new-jaxity#257. Drives seed.seed_default_content() against a minimal in-memory
fake that satisfies NotesAdapter, not against any real backend - the seeder is
written entirely against the adapter interface (base.py), so a fake is a
faithful test of it, and it keeps this suite free of live SiYuan/Notion/
Obsidian credentials.

Two things matter more than "does it write docs":
  1. A second run must be a no-op (the sentinel check), because a real re-run
     happens on every re-install and must never overwrite what the user has
     since written into a same-titled doc.
  2. One doc raising on create must not abort the batch - reported, not fatal.

Run:
    python3 -m pytest chassis/second_brain/tests/test_seed.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain.base import SearchHit  # noqa: E402
from chassis.second_brain.seed import (  # noqa: E402
    SENTINEL_TITLE,
    SEED_DOCS,
    seed_default_content,
)


class FakeAdapter:
    """In-memory NotesAdapter - just enough surface for the seeder to exercise."""

    def __init__(self, preexisting: dict[str, str] | None = None, fail_titles: set[str] | None = None):
        self.docs: dict[str, str] = dict(preexisting or {})
        self._fail_titles = fail_titles or set()
        self.create_calls: list[tuple[str, str, str]] = []

    def create_doc(self, parent: str, title: str, body: str) -> str:
        self.create_calls.append((parent, title, body))
        if title in self._fail_titles:
            raise RuntimeError(f"simulated failure creating {title!r}")
        doc_id = f"id-{len(self.docs) + 1}"
        self.docs[title] = doc_id
        return doc_id

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        return [
            SearchHit(id=doc_id, title=title, snippet="", deeplink="")
            for title, doc_id in self.docs.items()
            if query.lower() in title.lower()
        ][:limit]


class SeedDefaultContentTest(unittest.TestCase):
    def test_fresh_install_creates_every_doc_plus_sentinel(self):
        adapter = FakeAdapter()
        result = seed_default_content(adapter)
        self.assertTrue(result["seeded"])
        self.assertEqual(len(result["created"]), len(SEED_DOCS) + 1)  # + sentinel
        created_titles = {c["title"] for c in result["created"]}
        self.assertIn(SENTINEL_TITLE, created_titles)
        for title, _ in SEED_DOCS:
            self.assertIn(title, created_titles)

    def test_second_run_is_a_no_op(self):
        adapter = FakeAdapter()
        seed_default_content(adapter)
        calls_after_first_run = len(adapter.create_calls)

        result = seed_default_content(adapter)

        self.assertFalse(result["seeded"])
        self.assertEqual(result["reason"], "sentinel_exists")
        self.assertEqual(len(adapter.create_calls), calls_after_first_run)  # no new writes

    def test_sentinel_already_present_skips_without_touching_anything(self):
        adapter = FakeAdapter(preexisting={SENTINEL_TITLE: "user-written-id"})

        result = seed_default_content(adapter)

        self.assertFalse(result["seeded"])
        self.assertEqual(adapter.create_calls, [])

    def test_one_failing_doc_does_not_abort_the_batch(self):
        failing_title = SEED_DOCS[0][0]
        adapter = FakeAdapter(fail_titles={failing_title})

        result = seed_default_content(adapter)

        self.assertTrue(result["seeded"])
        failed_titles = {f["title"] for f in result.get("failed", [])}
        self.assertIn(failing_title, failed_titles)
        created_titles = {c["title"] for c in result["created"]}
        # every other doc, plus the sentinel, still landed
        self.assertEqual(len(created_titles), len(SEED_DOCS))  # all but the one failure, plus sentinel
        self.assertIn(SENTINEL_TITLE, created_titles)

    def test_never_calls_backend_specific_methods(self):
        # The whole point of writing against NotesAdapter is that nothing here
        # requires more surface than create_doc + search. A minimal fake with
        # only those two methods must be sufficient.
        adapter = FakeAdapter()
        self.assertTrue(hasattr(adapter, "create_doc"))
        self.assertTrue(hasattr(adapter, "search"))
        seed_default_content(adapter)  # must not raise AttributeError for anything else


if __name__ == "__main__":
    unittest.main()
