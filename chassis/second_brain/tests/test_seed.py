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

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain.base import SearchHit  # noqa: E402
from chassis.second_brain.seed import (  # noqa: E402
    MAP_TITLE,
    SENTINEL_TITLE,
    SEED_DOCS,
    has_existing_para,
    main as seed_main,
    seed_default_content,
    set_map_doc_id,
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
    def test_map_is_seeded_half_written_with_para_sections(self):
        body = dict(SEED_DOCS)[MAP_TITLE]
        for heading in ("## Projects", "## Areas", "## Resources"):
            self.assertIn(heading, body)
        self.assertNotIn("## Archive", body)
        self.assertIn("one line and one link", body)
        result = seed_default_content(FakeAdapter())
        self.assertIn(MAP_TITLE, {c["title"] for c in result["created"]})

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


class ExistingParaTest(unittest.TestCase):
    def test_numbered_para_titles_count(self):
        self.assertTrue(has_existing_para(FakeAdapter(preexisting={"1 Projects": "x"})))
        self.assertTrue(has_existing_para(FakeAdapter(preexisting={MAP_TITLE: "x"})))

    def test_unrelated_titles_do_not(self):
        self.assertFalse(has_existing_para(FakeAdapter(preexisting={"Projects backlog notes": "x"})))
        self.assertFalse(has_existing_para(FakeAdapter()))


CONFIG = """second_brain:
  backend: obsidian                         # notion | siyuan | obsidian
  mode: direct
  map:
    doc_id: ""                              # Map doc id. Env SECOND_BRAIN_MAP_DOC overrides.
                                            # Loaded into every session
    roots: {}
  obsidian:
    vault_path: VAULT
"""


class SetMapDocIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "chassis.config.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fills_empty_value_and_keeps_comments(self):
        self.path.write_text(CONFIG)
        self.assertEqual(set_map_doc_id(self.path, "abc-123"), "set")
        lines = self.path.read_text().splitlines()
        line = next(l for l in lines if "doc_id:" in l)
        self.assertTrue(line.startswith('    doc_id: "abc-123"'))
        self.assertEqual(line.index("#"), CONFIG.splitlines()[4].index("#"))
        self.assertIn("# Loaded into every session", self.path.read_text())
        self.assertEqual(len(lines), len(CONFIG.splitlines()))

    def test_never_overwrites_a_set_value(self):
        self.path.write_text(CONFIG.replace('doc_id: ""', 'doc_id: "keep-me"'))
        self.assertEqual(set_map_doc_id(self.path, "abc"), "already_set")
        self.assertIn("keep-me", self.path.read_text())

    def test_missing_key_writes_nothing(self):
        text = 'second_brain:\n  backend: obsidian\nmap:\n  doc_id: ""\n'
        self.path.write_text(text)
        self.assertEqual(set_map_doc_id(self.path, "abc"), "no_key")
        self.assertEqual(self.path.read_text(), text)


class DirectModeMainTest(unittest.TestCase):
    """End to end through the real Obsidian adapter, no network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.vault = base / "vault"
        self.vault.mkdir()
        self.config = base / "chassis.config.yaml"
        self.config.write_text(CONFIG.replace("VAULT", str(self.vault)))

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(seed_main(["--config", str(self.config)]), 0)
        return buf.getvalue()

    def test_empty_vault_is_seeded_and_map_id_written(self):
        out = self.run_main()
        self.assertIn("created", out)
        self.assertTrue((self.vault / "_MAP.md").is_file())
        self.assertIn('doc_id: "_MAP.md"', self.config.read_text())
        before = sorted(p.name for p in self.vault.iterdir())
        out = self.run_main()
        self.assertIn("not seeding", out)
        self.assertEqual(sorted(p.name for p in self.vault.iterdir()), before)

    def test_existing_para_is_left_alone_but_map_id_is_found(self):
        (self.vault / "1 Projects.md").write_text("# mine\n")
        (self.vault / "_MAP.md").write_text("# _MAP\n")
        out = self.run_main()
        self.assertIn("not seeding", out)
        self.assertEqual(sorted(p.name for p in self.vault.iterdir()), ["1 Projects.md", "_MAP.md"])
        self.assertIn('doc_id: "_MAP.md"', self.config.read_text())

    def test_scaffold_off_means_no_seed(self):
        self.config.write_text(self.config.read_text().replace(
            "  mode: direct\n", "  mode: direct\n  scaffold_para_if_missing: false\n"))
        self.run_main()
        self.assertEqual(list(self.vault.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
