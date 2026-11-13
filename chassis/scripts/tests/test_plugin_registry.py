#!/usr/bin/env python3
"""test_plugin_registry.py - plugin-registry.py list and link-skills (behalfbot#264).

Two bugs this locks down:
  1. Fetched plugin skills were never exposed to Claude Code: they sat under
     vendored-plugins/<p>/skills/<s>/ and nothing linked them into
     .claude/skills/.
  2. A DORMANT registry entry (supplier gone) was still offered to installers.

Runs against temp directories only.

Run:
    python3 -m pytest chassis/scripts/tests/test_plugin_registry.py -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "plugin-registry.py"
spec = importlib.util.spec_from_file_location("plugin_registry", SCRIPT)
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)


def make_plugin(root: Path, name: str, skill: str | None = None, flat_skill: bool = False) -> None:
    pdir = root / name
    pdir.mkdir(parents=True)
    (pdir / "openclaw.plugin.json").write_text(json.dumps({"id": name}))
    if skill:
        (pdir / "skills" / skill).mkdir(parents=True)
        (pdir / "skills" / skill / "SKILL.md").write_text(f"---\nname: {skill}\n---\n")
    if flat_skill:
        (pdir / "skills").mkdir(parents=True, exist_ok=True)
        (pdir / "skills" / f"{name}.md").write_text("legacy flat skill doc\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.root = self.home / "vendored-plugins"
        self.root.mkdir()
        make_plugin(self.root, "flight-search", "flight-search")
        make_plugin(self.root, "parse-youtube", "parse-youtube")
        make_plugin(self.root, "accommodation-search", "accommodation-search")
        make_plugin(self.root, "chef", "chef")
        make_plugin(self.root, "bfl", flat_skill=True)
        self.registry = [
            {"name": "flight-search", "path": "flight-search", "description": "Search flights"},
            {"name": "parse-youtube", "path": "parse-youtube", "description": "Transcripts"},
            {"name": "accommodation-search", "path": "accommodation-search",
             "description": "DORMANT - SUPPLIER GONE: Amadeus decommissioned its portal"},
            {"name": "chef", "path": "chef", "description": "Meal variety", "status": "dormant"},
            {"name": "bfl", "path": "bfl", "description": "Food log"},
        ]
        self.write_registry()
        env = {"CUSTOMER_HOME": str(self.home)}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("PLUGINS_FETCH_ROOT", None)
        os.environ.pop("CHASSIS_CONFIG", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_registry(self):
        (self.root / "registry.json").write_text(json.dumps({"plugins": self.registry}))

    def run_cli(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pr.main(list(argv))
        return rc, buf.getvalue()

    @property
    def skills(self) -> Path:
        return self.home / ".claude" / "skills"


class DormantTest(Base):
    def test_description_prefix_and_status_both_mark_dormant(self):
        self.assertTrue(pr.is_dormant({"description": "DORMANT - gone"}))
        self.assertTrue(pr.is_dormant({"description": "  dormant: gone"}))
        self.assertTrue(pr.is_dormant({"status": "Dormant", "description": "fine"}))
        self.assertFalse(pr.is_dormant({"description": "Mentions DORMANT later"}))

    def test_list_hides_dormant(self):
        rc, out = self.run_cli("list")
        self.assertEqual(rc, 0)
        self.assertIn("flight-search", out)
        self.assertNotIn("accommodation-search", out)
        self.assertNotIn("chef", out)

    def test_list_all_marks_dormant(self):
        _, out = self.run_cli("list", "--all", "--json")
        rows = {r["name"]: r for r in json.loads(out)}
        self.assertTrue(rows["accommodation-search"]["dormant"])
        self.assertFalse(rows["flight-search"]["dormant"])

    def test_list_without_registry_fails_loudly(self):
        (self.root / "registry.json").unlink()
        rc, _ = self.run_cli("list")
        self.assertEqual(rc, 1)


class LinkSkillsTest(Base):
    def test_links_are_relative_and_resolve(self):
        self.run_cli("link-skills")
        link = self.skills / "flight-search"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../../vendored-plugins/flight-search/skills/flight-search")
        self.assertTrue((link / "SKILL.md").is_file())

    def test_dormant_and_flat_skills_are_not_linked(self):
        self.run_cli("link-skills")
        names = sorted(p.name for p in self.skills.iterdir())
        self.assertEqual(names, ["flight-search", "parse-youtube"])

    def test_idempotent(self):
        self.run_cli("link-skills")
        _, out = self.run_cli("link-skills")
        self.assertIn("0 linked, 2 unchanged, 0 removed, 0 skipped", out)

    def test_module_switched_off_is_skipped_and_pruned(self):
        self.run_cli("link-skills")
        (self.home / "chassis.config.yaml").write_text(
            "modules:\n  parse_youtube:\n    enabled: false\n  flight-search: true\n"
        )
        _, out = self.run_cli("link-skills")
        self.assertFalse((self.skills / "parse-youtube").exists())
        self.assertTrue((self.skills / "flight-search").is_symlink())
        self.assertIn("remove parse-youtube", out)

    def test_never_replaces_customer_owned_entries(self):
        (self.skills / "flight-search").mkdir(parents=True)
        (self.skills / "flight-search" / "SKILL.md").write_text("mine")
        os.symlink("/somewhere/else", self.skills / "parse-youtube")
        _, out = self.run_cli("link-skills")
        self.assertEqual((self.skills / "flight-search" / "SKILL.md").read_text(), "mine")
        self.assertEqual(os.readlink(self.skills / "parse-youtube"), "/somewhere/else")
        self.assertIn("2 skipped", out)

    def test_dangling_own_link_is_pruned(self):
        self.run_cli("link-skills")
        os.symlink("../../vendored-plugins/gone/skills/gone", self.skills / "gone")
        self.run_cli("link-skills")
        self.assertFalse(os.path.lexists(self.skills / "gone"))

    def test_missing_registry_prunes_nothing(self):
        self.run_cli("link-skills")
        (self.root / "registry.json").unlink()
        rc, _ = self.run_cli("link-skills")
        self.assertEqual(rc, 0)
        self.assertTrue((self.skills / "flight-search").is_symlink())

    def test_dry_run_writes_nothing(self):
        self.run_cli("link-skills", "--dry-run")
        self.assertFalse(self.skills.exists())


if __name__ == "__main__":
    unittest.main()
