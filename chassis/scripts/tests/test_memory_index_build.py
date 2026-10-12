"""Unit tests for chassis/scripts/memory-index-build.py.

Run from repo root:
  python3 -m pytest chassis/scripts/tests/test_memory_index_build.py -v

Every test runs against a temp copy of
chassis/scripts/tests/fixtures/memory-index/store, never the fixture itself and
never a live notes store.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
FIXTURE = SCRIPTS / "tests" / "fixtures" / "memory-index" / "store"
_spec = importlib.util.spec_from_file_location("memory_index_build", SCRIPTS / "memory-index-build.py")
mib = importlib.util.module_from_spec(_spec)
sys.modules["memory_index_build"] = mib
_spec.loader.exec_module(mib)


def tree_hashes(root: str) -> dict[str, str]:
    out = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="memory-index-test-")
        self.store = os.path.join(self.tmp, "store")
        shutil.copytree(FIXTURE, self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def run_main(self, *args: str) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mib.main(["--store", self.store, *args])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def read(self, rel: str) -> str:
        with open(os.path.join(self.store, rel), encoding="utf-8") as f:
            return f.read()


class FrontmatterTest(StoreCase):
    def test_flat_frontmatter(self) -> None:
        n = mib.load_note(os.path.join(self.store, "feedback_note_a.md"))
        self.assertEqual(n.type, "feedback")
        self.assertEqual(n.description, 'A flat note with a "quoted" description.')
        self.assertEqual(n.domains, [])

    def test_nested_metadata_type(self) -> None:
        n = mib.load_note(os.path.join(self.store, "project_note_b.md"))
        self.assertEqual(n.name, "project_note_b")
        self.assertEqual(n.type, "project")

    def test_flow_list_domain_and_block_list_supersedes(self) -> None:
        n = mib.load_note(os.path.join(self.store, "feedback_note_e.md"))
        self.assertEqual(n.domains, ["beta"])
        self.assertEqual(n.supersedes, ["project_note_b.md"])

    def test_scalar_domain(self) -> None:
        n = mib.load_note(os.path.join(self.store, "feedback_note_d.md"))
        self.assertEqual(n.domains, ["alpha"])

    def test_no_frontmatter(self) -> None:
        p = os.path.join(self.store, "feedback_bare.md")
        with open(p, "w") as f:
            f.write("just a body\n")
        n = mib.load_note(p)
        self.assertEqual((n.name, n.description, n.domains), ("", "", []))


class BuildTest(StoreCase):
    def test_domain_inference(self) -> None:
        r = mib.build(self.store)
        self.assertEqual(r["inferred"], ["feedback_note_a.md", "project_note_b.md", "reference_note_c.md"])
        self.assertEqual(r["notes"]["reference_note_c.md"].domains, ["alpha", "beta"])
        self.assertEqual(r["explicit"], ["feedback_note_d.md", "feedback_note_e.md", "feedback_note_f.md"])

    def test_explicit_domain_wins_over_current_link(self) -> None:
        r = mib.build(self.store)
        self.assertIn("feedback_note_d.md", r["rendered"]["alpha"])
        self.assertNotIn("feedback_note_d.md", r["rendered"]["beta"])

    def test_subfolder_ignored(self) -> None:
        r = mib.build(self.store)
        self.assertNotIn("note_subagent.md", r["notes"])
        self.assertNotIn("subagent", r["indexes"])

    def test_report_categories(self) -> None:
        r = mib.build(self.store)
        self.assertEqual(r["unlisted_memory_only"], ["feedback_root_only.md"])
        self.assertEqual(r["unlisted_orphans"], ["feedback_note_f.md"])
        self.assertEqual(r["unknown_domain"], [("feedback_note_f.md", "gamma")])
        self.assertEqual(list(r["dup_titles"].values()), [["feedback_note_a.md", "feedback_note_f.md"]])
        self.assertEqual(r["superseded"], [("project_note_b.md", "feedback_note_e.md", True)])

    def test_pointer_lines_preserved_verbatim(self) -> None:
        out = mib.build(self.store)["rendered"]["alpha"]
        for line in (
            '- Repo pointer: chassis/memory/MEMORY_MODEL.md "Checking"',
            "- Second-brain pointer: obsidian:Projects/Example.md",
            "- [Gone note](feedback_gone.md) - a link to a file that no longer exists",
        ):
            self.assertIn(line, out)
        self.assertEqual(out.count("## Pointers (homes outside the notes store)"), 1)
        self.assertLess(out.index("feedback_note_a.md"), out.index("## Pointers"))

    def test_header_preserved_and_prose_dropped(self) -> None:
        out = mib.build(self.store)["rendered"]["alpha"]
        self.assertTrue(out.startswith(self.read("index-alpha.md").split("- [A]")[0].rstrip("\n")))
        self.assertIn("Intro prose that sits above the first list item.", out)
        self.assertNotIn("Stale prose after the first list item", out)

    def test_note_line_format(self) -> None:
        out = mib.build(self.store)["rendered"]["alpha"]
        self.assertIn(
            '- [Note A - flat frontmatter](feedback_note_a.md) - A flat note with a "quoted" description.', out)

    def test_em_dash_scrubbed(self) -> None:
        with open(os.path.join(self.store, "feedback_dash.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: Dash\u2014title\ndescription: a \u2014 b\ndomain: alpha\n---\n")
        out = mib.build(self.store)["rendered"]["alpha"]
        self.assertIn("- [Dash - title](feedback_dash.md) - a - b", out)
        self.assertNotIn("\u2014", out)


class ModesTest(StoreCase):
    def test_check_writes_nothing(self) -> None:
        before = tree_hashes(self.store)
        out = self.run_main()
        self.assertIn("Mode: check", out)
        self.run_main("--check", "--verbose")
        self.assertEqual(tree_hashes(self.store), before)

    def test_write_touches_only_top_level_indexes(self) -> None:
        before = tree_hashes(self.store)
        self.run_main("--write")
        after = tree_hashes(self.store)
        changed = {k for k in before if before[k] != after.get(k)}
        self.assertEqual(changed, {"index-alpha.md", "index-beta.md"})
        self.assertEqual(set(after), set(before))

    def test_write_is_idempotent(self) -> None:
        self.run_main("--write")
        once = tree_hashes(self.store)
        out = self.run_main("--write")
        self.assertIn("Wrote 0 index file(s)", out)
        self.assertEqual(tree_hashes(self.store), once)


class StoreResolutionTest(StoreCase):
    def test_env_var_store_is_used_without_flag(self) -> None:
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_AUTO_MEMORY_DIR": self.store}):
            with contextlib.redirect_stdout(buf):
                rc = mib.main([])
        self.assertEqual(rc, 0)
        self.assertIn("(CLAUDE_AUTO_MEMORY_DIR)", buf.getvalue())

    def test_missing_explicit_store_is_an_error_not_a_fallback(self) -> None:
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_AUTO_MEMORY_DIR": self.store}):
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = mib.main(["--store", os.path.join(self.tmp, "nope")])
        self.assertEqual(rc, 2)
        self.assertIn("store not found", err.getvalue())

    def test_derived_path_mangles_every_non_alphanumeric(self) -> None:
        import _memory_store
        self.assertEqual(_memory_store.mangle("/home/ana/.behalfbot"), "-home-ana--behalfbot")


class InitTest(StoreCase):
    def test_init_creates_missing_indexes_named_in_memory_md(self) -> None:
        with open(os.path.join(self.store, "MEMORY.md"), "a", encoding="utf-8") as f:
            f.write("- **[Gamma](index-gamma.md)** - gamma domain.\n")
        r = mib.build(self.store)
        self.assertEqual(r["missing_indexes"], ["index-gamma.md"])
        out = self.run_main("--init")
        self.assertIn("Created 1 index file(s): index-gamma.md", out)
        # feedback_note_f.md declares domain: gamma, so it is now listed.
        self.assertIn("feedback_note_f.md", self.read("index-gamma.md"))
        self.assertNotIn("index-gamma.md", mib.build(self.store)["missing_indexes"])

    def test_init_with_nothing_missing_creates_nothing(self) -> None:
        out = self.run_main("--init")
        self.assertIn("Created 0 index file(s): none", out)


if __name__ == "__main__":
    unittest.main()
