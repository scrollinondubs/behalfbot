"""Unit tests for chassis/scripts/memory-integrity.py.

Run from repo root:
  python3 -m pytest chassis/scripts/tests/test_memory_integrity.py -v

Each test builds a throwaway install repo and notes store in a temp dir and
plants exactly the pointer it expects to be reported, plus a correctly formed
neighbour that must NOT be reported. A checker that flags everything is as
useless as one that flags nothing, so every finding class has both halves.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("memory_integrity", SCRIPTS / "memory-integrity.py")
mi = importlib.util.module_from_spec(_spec)
sys.modules["memory_integrity"] = mi
_spec.loader.exec_module(mi)


class IntegrityCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="memory-integrity-test-")
        self.repo = Path(self.tmp) / "repo"
        self.store = Path(self.tmp) / "store"
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "skills").mkdir()
        self.store.mkdir()
        (self.repo / "docs" / "infra.md").write_text("# Infra\n")
        self.write_store("MEMORY.md", "# Root\n\n- [Tools](index-tools.md) - tools domain\n")
        self.write_store("index-tools.md", "# Tools\n\n- [Linked](feedback_linked.md) - linked note\n")
        self.write_store("feedback_linked.md", "---\nname: Linked\ndomain: tools\n---\nbody\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def write_store(self, name: str, text: str) -> None:
        (self.store / name).write_text(text)

    def write_doc(self, text: str) -> None:
        (self.repo / "docs" / "notes.md").write_text(text)

    def run_json(self, *extra: str) -> dict:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mi.main(["--json", "--repo", str(self.repo), "--store", str(self.store), *extra])
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def pointers(self, data: dict, key: str) -> list[str]:
        return [f.get("pointer", f["file"]) for f in data["findings"][key]]


class RepoPathTest(IntegrityCase):
    def test_missing_repo_path_reported_existing_one_not(self) -> None:
        self.write_doc("See docs/infra.md and docs/gone.md for details.\n")
        d = self.run_json()
        self.assertEqual(self.pointers(d, "missing_repo_path"), ["docs/gone.md"])

    def test_code_fences_are_skipped(self) -> None:
        self.write_doc("```\ncat docs/gone.md\n```\n")
        self.assertEqual(self.run_json()["counts"]["missing_repo_path"], 0)

    def test_absolute_path_into_the_repo_reported_outside_paths_not(self) -> None:
        self.write_doc(
            f"Bad: {self.repo}/docs/infra.md and /app/customer/docs/infra.md.\n"
            "Fine: ~/.claude/settings.json is outside the repo, /app/customer is a root.\n"
        )
        got = self.pointers(self.run_json(), "absolute_path")
        self.assertEqual(got, [f"{self.repo}/docs/infra.md", "/app/customer/docs/infra.md"])


class IssueRefTest(IntegrityCase):
    def test_bare_issue_reported_owner_repo_and_short_form_not(self) -> None:
        self.write_doc("Fixed in #12. See acme/widgets#7 and widgets#9, also PR#4.\n")
        d = self.run_json()
        self.assertEqual(self.pointers(d, "bare_issue_ref"), ["PR#4", "#12"])
        self.assertEqual(d["meta"]["short_form_issue_refs"], {"widgets": 1})


class NoteLinkTest(IntegrityCase):
    def test_dead_md_link_and_kebab_wiki_link(self) -> None:
        self.write_store("feedback_other.md",
                         "See [x](feedback_linked.md), [y](feedback_gone.md) and [[feedback-linked]].\n")
        self.write_store("index-tools.md",
                         "# Tools\n\n- [Linked](feedback_linked.md)\n- [[feedback_other]]\n")
        d = self.run_json()
        dead = d["findings"]["dead_note_link"]
        self.assertEqual([f["pointer"] for f in dead], ["feedback_gone.md", "[[feedback-linked]]"])
        self.assertEqual(dead[1]["detail"], "kebab-case; the snake_case file exists")

    def test_orphan_note_and_unlinked_index(self) -> None:
        self.write_store("feedback_orphan.md", "---\nname: Orphan\n---\n")
        self.write_store("index-stray.md", "# Stray\n")
        files = [f["file"] for f in self.run_json()["findings"]["orphan_note"]]
        self.assertEqual(files, ["notes:feedback_orphan.md", "notes:index-stray.md"])


class SecondBrainRefTest(IntegrityCase):
    def test_shapes_per_backend(self) -> None:
        self.write_doc(
            "Good: siyuan:20240101120000-abc1234, notion:0123456789abcdef0123456789abcdef, "
            "obsidian:Projects/Plan.md.\n"
            "Bad: siyuan:/Projects/Plan, notion:Plan, obsidian:/abs/path.md, obsidian:../up.md.\n"
            "Not refs: siyuan://blocks/x, `siyuan:<id>`.\n"
        )
        d = self.run_json()
        self.assertEqual(self.pointers(d, "bad_second_brain_ref"),
                         ["siyuan:/Projects/Plan", "notion:Plan", "obsidian:/abs/path.md", "obsidian:../up.md"])
        self.assertEqual(d["meta"]["second_brain_refs"], {"siyuan": 1, "notion": 1, "obsidian": 1})

    def test_probe_is_off_by_default(self) -> None:
        self.write_doc("siyuan:20240101120000-abc1234\n")
        d = self.run_json()
        self.assertTrue(d["meta"]["second_brain_probe"].startswith("off"))

    def test_probe_reads_through_the_adapter(self) -> None:
        self.write_doc("Live obsidian:Live.md, dead obsidian:Gone.md, other siyuan:20240101120000-abc1234.\n")

        class Notes:
            def read_doc(self, doc_id: str) -> str:
                if doc_id != "Live.md":
                    raise FileNotFoundError(doc_id)
                return "body"

        class Adapter:
            backend = "obsidian"
            notes = Notes()

        fake = type(sys)("chassis.second_brain")
        fake.get_adapter = lambda path: Adapter()
        with mock.patch.dict(sys.modules, {"chassis.second_brain": fake}):
            d = self.run_json("--probe-second-brain")
        self.assertEqual(self.pointers(d, "dead_second_brain_ref"), ["obsidian:Gone.md"])
        self.assertEqual(self.pointers(d, "foreign_backend_ref"), ["siyuan:20240101120000-abc1234"])
        self.assertIn("ok (obsidian, 2 refs read)", d["meta"]["second_brain_probe"])

    def test_probe_without_an_adapter_is_reported_not_raised(self) -> None:
        self.write_doc("obsidian:Live.md\n")
        fake = type(sys)("chassis.second_brain")

        def boom(path):
            raise ValueError("no backend")

        fake.get_adapter = boom
        with mock.patch.dict(sys.modules, {"chassis.second_brain": fake}):
            d = self.run_json("--probe-second-brain")
        self.assertIn("skipped - adapter unavailable", d["meta"]["second_brain_probe"])


class ReadOnlyTest(IntegrityCase):
    def test_never_writes_and_text_mode_runs(self) -> None:
        self.write_doc("docs/gone.md #3 siyuan:bad\n")

        def hashes() -> dict:
            out = {}
            for root in (self.repo, self.store):
                for dirpath, _, files in os.walk(root):
                    for f in files:
                        p = os.path.join(dirpath, f)
                        out[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
            return out

        before = hashes()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(mi.main(["--repo", str(self.repo), "--store", str(self.store), "--all"]), 0)
        self.assertIn("Memory integrity report (read-only)", buf.getvalue())
        self.assertEqual(hashes(), before)

    def test_missing_store_still_reports_repo_side(self) -> None:
        self.write_doc("docs/gone.md\n")
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CUSTOMER_HOME": str(self.repo)}, clear=False), \
                contextlib.redirect_stdout(buf):
            os.environ.pop("CLAUDE_AUTO_MEMORY_DIR", None)
            mi.main(["--json", "--repo", str(self.repo), "--store", str(Path(self.tmp) / "nope")])
        d = json.loads(buf.getvalue())
        self.assertIsNone(d["meta"]["store"])
        self.assertEqual(d["counts"]["missing_repo_path"], 1)


if __name__ == "__main__":
    unittest.main()
