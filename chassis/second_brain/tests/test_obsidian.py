#!/usr/bin/env python3
"""test_obsidian.py - Unit tests for the Obsidian second-brain adapter.

Everything runs against throwaway temp-dir vaults - no Obsidian process, no
network. Covers: read, search, deeplink, create, append, writes against a
read-only vault (config-declared and filesystem-enforced), path-traversal
rejection, and the factory branch.

Run:
    python3 -m pytest chassis/second_brain/tests/test_obsidian.py -v
    # or directly:
    python3 chassis/second_brain/tests/test_obsidian.py
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure the repo root is importable so `chassis.second_brain` resolves
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain.base import NotImplementedDatabase, SearchHit  # noqa: E402
from chassis.second_brain.factory import get_adapter  # noqa: E402
from chassis.second_brain.obsidian import (  # noqa: E402
    ObsidianAdapter,
    ObsidianError,
    ObsidianNotes,
    ObsidianReadOnlyError,
)


def _make_vault(root: Path) -> Path:
    """Build a small vault: two notes, one nested, plus housekeeping dirs."""
    vault = root / "second-brain"
    (vault / "Briefings").mkdir(parents=True)
    (vault / ".obsidian").mkdir()
    (vault / "Inbox.md").write_text(
        "# Inbox\n\nCall the notary about the apartment paperwork.\n",
        encoding="utf-8",
    )
    (vault / "Briefings" / "2026-07-09.md").write_text(
        "# Morning briefing\n\nThree items today - lead follow-ups, notary, padel.\n",
        encoding="utf-8",
    )
    (vault / ".obsidian" / "workspace.md").write_text("notary", encoding="utf-8")
    return vault


class ObsidianVaultTestCase(unittest.TestCase):
    """Shared temp-vault scaffolding."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="obsidian-adapter-test-"))
        self.vault = _make_vault(self._tmp)
        self.notes = ObsidianNotes(str(self.vault))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        # Restore write bits so rmtree can clean read-only test vaults
        for dirpath, _dirnames, _filenames in os.walk(self._tmp):
            os.chmod(dirpath, stat.S_IRWXU)
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestReadDoc(ObsidianVaultTestCase):
    def test_read_doc_returns_markdown_body(self):
        body = self.notes.read_doc("Briefings/2026-07-09.md")
        self.assertIn("# Morning briefing", body)

    def test_read_doc_md_suffix_optional(self):
        self.assertEqual(
            self.notes.read_doc("Inbox"),
            self.notes.read_doc("Inbox.md"),
        )

    def test_read_doc_missing_raises(self):
        with self.assertRaises(ObsidianError) as ctx:
            self.notes.read_doc("Briefings/nope.md")
        self.assertIn("not found", str(ctx.exception))


class TestSearch(ObsidianVaultTestCase):
    def test_search_matches_content(self):
        hits = self.notes.search("notary")
        ids = [hit.id for hit in hits]
        self.assertIn("Inbox.md", ids)
        self.assertIn("Briefings/2026-07-09.md", ids)

    def test_search_matches_filename(self):
        hits = self.notes.search("2026-07-09")
        self.assertEqual(hits[0].id, "Briefings/2026-07-09.md")
        self.assertEqual(hits[0].title, "2026-07-09")

    def test_search_returns_searchhit_shape(self):
        hit = self.notes.search("notary", limit=1)[0]
        self.assertIsInstance(hit, SearchHit)
        self.assertTrue(hit.snippet)
        self.assertTrue(hit.deeplink.startswith("obsidian://open?vault="))
        self.assertGreater(hit.score, 0.0)

    def test_search_respects_limit(self):
        self.assertEqual(len(self.notes.search("notary", limit=1)), 1)

    def test_search_skips_housekeeping_dirs(self):
        ids = [hit.id for hit in self.notes.search("notary", limit=50)]
        self.assertNotIn(".obsidian/workspace.md", ids)

    def test_search_no_match_returns_empty(self):
        self.assertEqual(self.notes.search("zzz-no-such-token"), [])


class TestDeeplink(ObsidianVaultTestCase):
    def test_deeplink_format_and_encoding(self):
        link = self.notes.get_deeplink("Briefings/2026-07-09.md")
        self.assertEqual(
            link,
            "obsidian://open?vault=second-brain&file=Briefings%2F2026-07-09.md",
        )

    def test_deeplink_uses_configured_vault_name(self):
        notes = ObsidianNotes(str(self.vault), vault_name="My Vault")
        self.assertIn("vault=My%20Vault", notes.get_deeplink("Inbox.md"))


class TestWrites(ObsidianVaultTestCase):
    def test_create_doc_writes_and_returns_id(self):
        doc_id = self.notes.create_doc("Briefings/", "2026-07-10", "# Tomorrow\n")
        self.assertEqual(doc_id, "Briefings/2026-07-10.md")
        self.assertEqual(self.notes.read_doc(doc_id), "# Tomorrow\n")

    def test_create_doc_empty_parent_uses_vault_root(self):
        doc_id = self.notes.create_doc("", "Scratch", "hello")
        self.assertEqual(doc_id, "Scratch.md")

    def test_create_doc_creates_missing_parent_dirs(self):
        doc_id = self.notes.create_doc("Projects/Chassis", "Notes", "body")
        self.assertEqual(doc_id, "Projects/Chassis/Notes.md")

    def test_create_doc_refuses_overwrite(self):
        with self.assertRaises(ObsidianError) as ctx:
            self.notes.create_doc("", "Inbox", "clobber")
        self.assertIn("already exists", str(ctx.exception))
        self.assertIn("notary", self.notes.read_doc("Inbox.md"))

    def test_append_to_doc(self):
        self.notes.append_to_doc("Inbox.md", "New line.\n")
        body = self.notes.read_doc("Inbox.md")
        self.assertTrue(body.endswith("New line.\n"))
        self.assertIn("notary", body)

    def test_append_to_missing_doc_raises(self):
        with self.assertRaises(ObsidianError):
            self.notes.append_to_doc("nope.md", "content")

    def test_link_blocks_not_implemented(self):
        with self.assertRaises(NotImplementedError) as ctx:
            self.notes.link_blocks("Inbox.md", "Briefings/2026-07-09.md")
        self.assertIn("obsidian", str(ctx.exception).lower())


class TestReadOnlyVault(ObsidianVaultTestCase):
    def test_config_read_only_blocks_create(self):
        notes = ObsidianNotes(str(self.vault), read_only=True)
        with self.assertRaises(ObsidianReadOnlyError) as ctx:
            notes.create_doc("Briefings/", "2026-07-10", "body")
        self.assertIn("read_only: true", str(ctx.exception))
        self.assertFalse((self.vault / "Briefings" / "2026-07-10.md").exists())

    def test_config_read_only_blocks_append(self):
        notes = ObsidianNotes(str(self.vault), read_only=True)
        before = (self.vault / "Inbox.md").read_text(encoding="utf-8")
        with self.assertRaises(ObsidianReadOnlyError):
            notes.append_to_doc("Inbox.md", "should never land")
        self.assertEqual((self.vault / "Inbox.md").read_text(encoding="utf-8"), before)

    def test_config_read_only_still_reads(self):
        notes = ObsidianNotes(str(self.vault), read_only=True)
        self.assertIn("notary", notes.read_doc("Inbox.md"))
        self.assertTrue(notes.search("notary"))

    @unittest.skipIf(os.geteuid() == 0, "root ignores permission bits")
    def test_filesystem_read_only_blocks_write_and_names_cause(self):
        # Config claims writable; the filesystem says otherwise. The filesystem wins.
        for dirpath, _dirnames, _filenames in os.walk(self.vault):
            os.chmod(dirpath, stat.S_IRUSR | stat.S_IXUSR)
        with self.assertRaises(ObsidianReadOnlyError) as ctx:
            self.notes.append_to_doc("Inbox.md", "should never land")
        self.assertIn("not writable", str(ctx.exception))
        with self.assertRaises(ObsidianReadOnlyError):
            self.notes.create_doc("", "Scratch", "body")

    @unittest.skipIf(os.geteuid() == 0, "root ignores permission bits")
    def test_filesystem_read_only_leaves_no_partial_files(self):
        for dirpath, _dirnames, _filenames in os.walk(self.vault):
            os.chmod(dirpath, stat.S_IRUSR | stat.S_IXUSR)
        with self.assertRaises(ObsidianReadOnlyError):
            self.notes.create_doc("", "Scratch", "body")
        os.chmod(self.vault, stat.S_IRWXU)
        leftovers = [p.name for p in self.vault.iterdir() if p.name.startswith(".obsidian-adapter-")]
        self.assertEqual(leftovers, [])
        self.assertFalse((self.vault / "Scratch.md").exists())


class TestPathTraversal(ObsidianVaultTestCase):
    def test_read_traversal_rejected(self):
        with self.assertRaises(ObsidianError) as ctx:
            self.notes.read_doc("../../etc/passwd")
        self.assertIn("outside the vault root", str(ctx.exception))

    def test_absolute_path_rejected(self):
        with self.assertRaises(ObsidianError) as ctx:
            self.notes.read_doc("/etc/passwd")
        self.assertIn("absolute path", str(ctx.exception))

    def test_write_traversal_rejected(self):
        outside = self._tmp / "escape.md"
        with self.assertRaises(ObsidianError):
            self.notes.create_doc("..", "escape", "body")
        self.assertFalse(outside.exists())

    def test_sneaky_nested_traversal_rejected(self):
        with self.assertRaises(ObsidianError):
            self.notes.read_doc("Briefings/../../outside.md")


class TestFactory(ObsidianVaultTestCase):
    def _write_config(self, extra: str = "") -> Path:
        config = self._tmp / "chassis.config.yaml"
        config.write_text(
            "second_brain:\n"
            "  backend: obsidian\n"
            "  obsidian:\n"
            f"    vault_path: {self.vault}\n"
            f"{extra}",
            encoding="utf-8",
        )
        return config

    def test_factory_returns_obsidian_adapter(self):
        adapter = get_adapter(self._write_config())
        self.assertIsInstance(adapter, ObsidianAdapter)
        self.assertEqual(adapter.backend, "obsidian")
        self.assertIsInstance(adapter.database, NotImplementedDatabase)
        self.assertIn("notary", adapter.notes.read_doc("Inbox.md"))

    def test_factory_passes_read_only_through(self):
        adapter = get_adapter(self._write_config("    read_only: true\n"))
        with self.assertRaises(ObsidianReadOnlyError):
            adapter.notes.create_doc("", "Scratch", "body")

    def test_factory_unknown_backend_lists_all_three(self):
        config = self._tmp / "chassis.config.yaml"
        config.write_text("second_brain:\n  backend: roam\n", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            get_adapter(config)
        self.assertIn("siyuan, notion, obsidian", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
