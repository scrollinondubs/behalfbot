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
    _strip_frontmatter,
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


_TEMPLATER_NOTE = (
    "---\n"
    "created: <% tp.date.now() %>\n"
    "tags:\n"
    "  - notary\n"
    "  - templater\n"
    "---\n"
    "\n"
    "Body about the notary appointment.\n"
    "\n"
    "```dataview\n"
    "LIST FROM #notary\n"
    "```\n"
)


class TestStripFrontmatter(unittest.TestCase):
    def test_strips_leading_fence(self):
        self.assertEqual(
            _strip_frontmatter("---\ntags: [a]\n---\nbody line\n"),
            "body line\n",
        )

    def test_no_frontmatter_untouched(self):
        self.assertEqual(_strip_frontmatter("plain body\n"), "plain body\n")

    def test_unterminated_fence_untouched(self):
        # Guessing where an unterminated fence ends risks eating body content.
        text = "---\ntags: [a]\nno closing fence\n"
        self.assertEqual(_strip_frontmatter(text), text)

    def test_templater_syntax_inside_fence_is_skipped_verbatim(self):
        body = _strip_frontmatter(_TEMPLATER_NOTE)
        self.assertTrue(body.startswith("Body about the notary"))
        self.assertNotIn("tp.date.now", body)
        # The dataview block is BODY content and must survive.
        self.assertIn("```dataview", body)

    def test_dots_closing_delimiter(self):
        self.assertEqual(_strip_frontmatter("---\na: 1\n...\nbody\n"), "body\n")

    def test_mid_document_rules_not_treated_as_frontmatter(self):
        text = "intro\n---\nnot frontmatter\n---\nmore\n"
        self.assertEqual(_strip_frontmatter(text), text)


class TestFrontmatterEmit(ObsidianVaultTestCase):
    def _fm_notes(self, **kwargs) -> ObsidianNotes:
        return ObsidianNotes(str(self.vault), frontmatter=True, **kwargs)

    def test_create_doc_emits_created_and_tags(self):
        notes = self._fm_notes(frontmatter_tags="jax, briefing")
        doc_id = notes.create_doc("Briefings/", "2026-07-21", "# Today\n")
        raw = notes.read_doc(doc_id)
        self.assertTrue(raw.startswith("---\ncreated: "))
        self.assertIn("tags:\n  - jax\n  - briefing\n", raw)
        # Frontmatter closes and the body survives below it.
        self.assertIn("---\n\n# Today\n", raw)

    def test_tags_accept_a_real_list_too(self):
        notes = self._fm_notes(frontmatter_tags=["a", "b"])
        raw = notes.read_doc(notes.create_doc("", "Listy", "body"))
        self.assertIn("  - a\n  - b\n", raw)

    def test_no_tags_config_omits_tags_key(self):
        raw = self._fm_notes().read_doc(self._fm_notes().create_doc("", "NoTags", "body"))
        self.assertIn("created: ", raw)
        self.assertNotIn("tags:", raw)

    def test_disabled_by_default(self):
        doc_id = self.notes.create_doc("", "Plain", "just the body\n")
        self.assertEqual(self.notes.read_doc(doc_id), "just the body\n")

    def test_body_with_own_frontmatter_is_not_double_wrapped(self):
        # A caller (or template) that already supplies frontmatter wins - the
        # adapter must never stack a second fence on top.
        notes = self._fm_notes(frontmatter_tags="jax")
        doc_id = notes.create_doc("", "OwnFm", _TEMPLATER_NOTE)
        raw = notes.read_doc(doc_id)
        self.assertEqual(raw, _TEMPLATER_NOTE)
        self.assertNotIn("created: 2", raw.split("---")[1])

    def test_append_never_adds_frontmatter(self):
        notes = self._fm_notes(frontmatter_tags="jax")
        notes.append_to_doc("Inbox.md", "appended line\n")
        body = notes.read_doc("Inbox.md")
        self.assertTrue(body.endswith("appended line\n"))
        self.assertNotIn("---", body)


class TestFrontmatterStrippedFromSnippets(ObsidianVaultTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.vault / "Templated.md").write_text(_TEMPLATER_NOTE, encoding="utf-8")

    def test_search_snippet_excludes_frontmatter(self):
        hits = [h for h in self.notes.search("notary appointment") if h.id == "Templated.md"]
        self.assertEqual(len(hits), 1)
        self.assertNotIn("tp.date.now", hits[0].snippet)
        self.assertNotIn("---", hits[0].snippet)
        self.assertIn("notary appointment", hits[0].snippet)

    def test_frontmatter_only_match_still_returns_the_note(self):
        # 'templater' appears ONLY inside the frontmatter (as a tag). The note
        # must still be findable; the snippet falls back to the body head.
        hits = [h for h in self.notes.search("templater") if h.id == "Templated.md"]
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].snippet.startswith("Body about the notary"))
        self.assertGreater(hits[0].score, 0.0)

    def test_search_does_not_corrupt_the_note(self):
        self.notes.search("notary")
        self.assertEqual(
            (self.vault / "Templated.md").read_text(encoding="utf-8"), _TEMPLATER_NOTE
        )

    def test_list_recent_snippet_excludes_frontmatter(self):
        from datetime import datetime, timedelta

        hits = self.notes.list_recent(
            since=datetime.now() - timedelta(hours=1),
            until=datetime.now() + timedelta(hours=1),
            limit=50,
        )
        templated = [h for h in hits if h.id == "Templated.md"]
        self.assertEqual(len(templated), 1)
        self.assertTrue(templated[0].snippet.startswith("Body about the notary"))
        self.assertNotIn("---", templated[0].snippet)


class TestDailyNotes(ObsidianVaultTestCase):
    def _daily_notes(self, **kwargs) -> ObsidianNotes:
        return ObsidianNotes(str(self.vault), daily_notes_dir="Journal/Daily", **kwargs)

    def test_daily_note_id_uses_configured_dir_and_date_format(self):
        from datetime import date

        notes = self._daily_notes()
        self.assertEqual(notes.daily_note_id(date(2026, 7, 21)), "Journal/Daily/2026-07-21.md")

    def test_daily_note_id_defaults_to_today(self):
        from datetime import datetime

        notes = self._daily_notes()
        today = datetime.now().date().strftime("%Y-%m-%d")
        self.assertEqual(notes.daily_note_id(), f"Journal/Daily/{today}.md")

    def test_daily_note_id_without_dir_lands_in_vault_root(self):
        from datetime import date

        self.assertEqual(self.notes.daily_note_id(date(2026, 7, 21)), "2026-07-21.md")

    def test_append_creates_then_appends(self):
        from datetime import date

        notes = self._daily_notes()
        day = date(2026, 7, 21)
        doc_id = notes.append_to_daily_note("- first entry\n", day)
        self.assertEqual(doc_id, "Journal/Daily/2026-07-21.md")
        self.assertEqual(notes.read_doc(doc_id), "- first entry\n")

        doc_id_2 = notes.append_to_daily_note("- second entry\n", day)
        self.assertEqual(doc_id_2, doc_id)
        body = notes.read_doc(doc_id)
        self.assertIn("- first entry\n", body)
        self.assertTrue(body.endswith("- second entry\n"))

    def test_created_daily_note_gets_frontmatter_when_enabled(self):
        from datetime import date

        notes = self._daily_notes(frontmatter=True, frontmatter_tags="daily")
        doc_id = notes.append_to_daily_note("- entry\n", date(2026, 7, 21))
        raw = notes.read_doc(doc_id)
        self.assertTrue(raw.startswith("---\ncreated: "))
        self.assertIn("  - daily\n", raw)
        self.assertTrue(raw.endswith("- entry\n"))

    def test_read_only_vault_refuses_daily_note_write(self):
        from datetime import date

        notes = ObsidianNotes(
            str(self.vault), read_only=True, daily_notes_dir="Journal/Daily"
        )
        with self.assertRaises(ObsidianReadOnlyError):
            notes.append_to_daily_note("- entry\n", date(2026, 7, 21))
        self.assertFalse((self.vault / "Journal").exists())


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

    def test_factory_passes_frontmatter_config_through(self):
        adapter = get_adapter(
            self._write_config(
                "    frontmatter: true\n"
                "    frontmatter_tags: jax, briefing\n"
            )
        )
        doc_id = adapter.notes.create_doc("", "FromFactory", "body")
        raw = adapter.notes.read_doc(doc_id)
        self.assertTrue(raw.startswith("---\ncreated: "))
        self.assertIn("  - jax\n  - briefing\n", raw)

    def test_factory_passes_daily_notes_dir_through(self):
        from datetime import date

        adapter = get_adapter(self._write_config("    daily_notes_dir: Journal/Daily\n"))
        self.assertEqual(
            adapter.notes.daily_note_id(date(2026, 7, 21)),
            "Journal/Daily/2026-07-21.md",
        )

    def test_factory_defaults_leave_frontmatter_off_and_daily_dir_root(self):
        from datetime import date

        adapter = get_adapter(self._write_config())
        doc_id = adapter.notes.create_doc("", "Defaults", "plain body\n")
        self.assertEqual(adapter.notes.read_doc(doc_id), "plain body\n")
        self.assertEqual(adapter.notes.daily_note_id(date(2026, 7, 21)), "2026-07-21.md")

    def test_factory_unknown_backend_lists_all_three(self):
        config = self._tmp / "chassis.config.yaml"
        config.write_text("second_brain:\n  backend: roam\n", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            get_adapter(config)
        self.assertIn("siyuan, notion, obsidian", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
