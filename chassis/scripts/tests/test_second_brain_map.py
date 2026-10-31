"""Unit tests for chassis/scripts/second-brain-map.py and its two shell wrappers.

Run from repo root:
  python3 -m pytest chassis/scripts/tests/test_second_brain_map.py -v

No network and no live backend: the adapter is a fake, and the end-to-end
wrapper tests use an Obsidian config pointing at a vault that does not exist,
which fails fast and locally.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
CHASSIS = SCRIPTS.parent
_spec = importlib.util.spec_from_file_location("second_brain_map", SCRIPTS / "second-brain-map.py")
sbm = importlib.util.module_from_spec(_spec)
sys.modules["second_brain_map"] = sbm
_spec.loader.exec_module(sbm)

from chassis.second_brain.base import SearchHit  # noqa: E402

MAP_ID = "20240101120000-map0001"
P1 = "20240101120000-proj001"
P2 = "20240101120000-proj002"
A1 = "20240101120000-area001"
GONE = "20240101120000-gone001"


def hit(doc_id: str, title: str = "") -> SearchHit:
    return SearchHit(id=doc_id, title=title or doc_id, snippet="", deeplink="")


class FakeNotes:
    def __init__(self, map_text: str, children: dict, fail_roots=()):
        self.map_text = map_text
        self.children = children
        self.fail_roots = set(fail_roots)
        self.source_reads = 0

    def read_doc(self, doc_id):
        raise AssertionError("read_doc expands links; the map must be read with read_doc_source")

    def read_doc_source(self, doc_id):
        self.source_reads += 1
        return self.map_text

    def list_children(self, parent, limit=200):
        if parent in self.fail_roots:
            raise RuntimeError("no such container")
        return [hit(i) for i in self.children.get(parent, [])]


class FakeAdapter:
    def __init__(self, backend: str, notes: FakeNotes):
        self.backend = backend
        self.notes = notes


SIYUAN_MAP = (
    "# _MAP\n\n## Projects\n"
    f"- (({P1} 'Alpha')) - the alpha launch\n"
    f"- (({GONE} \"Old thing\")) - finished\n"
    f"## Areas\n- (({A1} 'Health'))\n"
)


class LinkedIdsTest(unittest.TestCase):
    def test_siyuan_refs_with_either_quote(self):
        self.assertEqual(sbm.linked_ids("siyuan", SIYUAN_MAP), {P1, GONE, A1})

    def test_siyuan_ignores_bare_ids_in_prose(self):
        self.assertEqual(sbm.linked_ids("siyuan", f"see {P1} later"), set())

    def test_obsidian_wikilinks_and_md_links(self):
        text = "- [[Alpha]]\n- [[Areas/Health|health]]\n- [Beta](Projects/Beta.md)\n"
        self.assertEqual(sbm.linked_ids("obsidian", text), {"Alpha.md", "Areas/Health.md", "Projects/Beta.md"})

    def test_notion_ids_are_normalized(self):
        text = "notion:1234abcd-1234-1234-1234-1234567890ab Alpha"
        self.assertEqual(sbm.linked_ids("notion", text), {"1234abcd1234123412341234567890ab"})


class DriftReportTest(unittest.TestCase):
    roots = {"projects": "/Projects", "areas": "/Areas"}

    def test_missing_and_stale_are_both_reported(self):
        notes = FakeNotes(SIYUAN_MAP, {"/Projects": [P1, P2], "/Areas": [A1]})
        report = sbm.drift_report(FakeAdapter("siyuan", notes), MAP_ID, self.roots)
        kinds = {(r["kind"], r["doc_id"]) for r in report}
        self.assertEqual(kinds, {("missing_from_map", P2), ("stale_ref_in_map", GONE)})
        self.assertEqual(notes.source_reads, 1)

    def test_agreement_is_an_empty_array(self):
        text = f"(({P1} 'Alpha')) (({A1} 'Health'))"
        notes = FakeNotes(text, {"/Projects": [P1], "/Areas": [A1]})
        self.assertEqual(sbm.drift_report(FakeAdapter("siyuan", notes), MAP_ID, self.roots), [])

    def test_the_map_does_not_list_itself(self):
        text = f"(({P1} 'Alpha')) (({A1} 'Health'))"
        notes = FakeNotes(text, {"/Projects": [P1], "/Areas": [A1, MAP_ID]})
        self.assertEqual(sbm.drift_report(FakeAdapter("siyuan", notes), MAP_ID, self.roots), [])

    def test_unlistable_root_reports_probe_failed_not_false_stale_refs(self):
        notes = FakeNotes(SIYUAN_MAP, {"/Projects": [P1]}, fail_roots={"/Areas"})
        report = sbm.drift_report(FakeAdapter("siyuan", notes), MAP_ID, self.roots)
        self.assertTrue(any(r["kind"] == "probe_failed" for r in report))
        self.assertFalse(any(r["kind"] == "stale_ref_in_map" for r in report))

    def test_empty_map_is_a_probe_failure(self):
        notes = FakeNotes("  \n", {"/Projects": [P1]})
        report = sbm.drift_report(FakeAdapter("siyuan", notes), MAP_ID, self.roots)
        self.assertEqual([r["kind"] for r in report], ["probe_failed"])

    def test_obsidian_name_link_counts_for_a_nested_path(self):
        notes = FakeNotes("- [[Alpha]]\n", {"Projects/": ["Projects/Alpha.md", "Projects/Beta.md"]})
        report = sbm.drift_report(FakeAdapter("obsidian", notes), "_MAP.md", {"projects": "Projects/"})
        self.assertEqual([(r["kind"], r["doc_id"]) for r in report], [("missing_from_map", "Projects/Beta.md")])

    def test_notion_matches_dashed_and_undashed_ids(self):
        dashed = "1234abcd-1234-1234-1234-1234567890ab"
        notes = FakeNotes("notion:1234abcd1234123412341234567890ab Alpha", {"root": [dashed]})
        self.assertEqual(sbm.drift_report(FakeAdapter("notion", notes), "map", {"projects": "root"}), [])


class UrlFallbackTest(unittest.TestCase):
    def test_siyuan_connection_failure_tries_the_other_url(self):
        class Notes:
            _base_url = "http://host.docker.internal:6806"

        adapter = FakeAdapter("siyuan", Notes())
        seen = []

        def call():
            seen.append(adapter.notes._base_url)
            if adapter.notes._base_url != "http://127.0.0.1:6806":
                raise RuntimeError("SiYuan /api/x request failed: refused")
            return "ok"

        self.assertEqual(sbm.with_url_fallback(adapter, call), "ok")
        self.assertEqual(seen, ["http://host.docker.internal:6806", "http://127.0.0.1:6806"])

    def test_auth_failure_is_not_retried(self):
        class Notes:
            _base_url = "http://127.0.0.1:6806"

        adapter = FakeAdapter("siyuan", Notes())
        calls = []

        def call():
            calls.append(1)
            raise RuntimeError("returned code=-1: 'Auth failed'")

        with self.assertRaises(RuntimeError):
            sbm.with_url_fallback(adapter, call)
        self.assertEqual(len(calls), 1)


    def test_http_error_means_the_kernel_answered_and_is_not_retried(self):
        class Notes:
            _base_url = "http://host.docker.internal:6806"

        adapter = FakeAdapter("siyuan", Notes())
        calls = []

        def call():
            calls.append(1)
            raise RuntimeError("SiYuan /api/x request failed: HTTP Error 401: Unauthorized")

        with self.assertRaises(RuntimeError) as ctx:
            sbm.with_url_fallback(adapter, call)
        self.assertEqual(len(calls), 1)
        self.assertIn("401", str(ctx.exception))


class _InstallHomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"CUSTOMER_HOME": str(self.home)}, clear=False)
        self.env.start()
        for var in ("CHASSIS_HOME", "CLAUDE_PROJECT_DIR", "SECOND_BRAIN_MAP_DOC"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_config(self, map_block: str = "", backend: str = "siyuan"):
        (self.home / "chassis.config.yaml").write_text(
            f"second_brain:\n  backend: {backend}\n{map_block}", encoding="utf-8")


class LoadCommandTest(_InstallHomeCase):
    def run_load(self) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(sbm.cmd_load(), 0)
        return buf.getvalue()

    def test_no_config_prints_nothing(self):
        self.assertEqual(self.run_load(), "")

    def test_no_map_configured_prints_nothing(self):
        self.write_config()
        self.assertEqual(self.run_load(), "")

    def test_template_placeholder_counts_as_unconfigured(self):
        self.write_config('  map:\n    doc_id: "<map-doc-id>"\n')
        self.assertEqual(self.run_load(), "")

    def test_loads_the_map_as_session_context(self):
        self.write_config(f"  map:\n    doc_id: {MAP_ID}\n")
        adapter = FakeAdapter("siyuan", FakeNotes(SIYUAN_MAP, {}))
        with mock.patch("chassis.second_brain.factory.get_adapter", return_value=adapter):
            payload = json.loads(self.run_load())
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn(f"(({P1} 'Alpha'))", out["additionalContext"])
        self.assertIn(f"siyuan:{MAP_ID}", out["additionalContext"])

    def test_env_var_overrides_config(self):
        self.write_config("  map:\n    doc_id: from-config\n")
        os.environ["SECOND_BRAIN_MAP_DOC"] = "from-env"
        adapter = FakeAdapter("siyuan", FakeNotes("x", {}))
        with mock.patch("chassis.second_brain.factory.get_adapter", return_value=adapter):
            self.assertIn("siyuan:from-env", self.run_load())

    def test_unreachable_backend_emits_banner_with_docker_aware_hint(self):
        self.write_config(f"  map:\n    doc_id: {MAP_ID}\n")
        with mock.patch("chassis.second_brain.factory.get_adapter", side_effect=RuntimeError("SiYuan unreachable")):
            ctx = json.loads(self.run_load())["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("SECOND BRAIN UNREACHABLE"))
        self.assertIn("SiYuan unreachable", ctx)
        self.assertIn("Docker container", ctx)

    def test_empty_map_emits_banner(self):
        self.write_config(f"  map:\n    doc_id: {MAP_ID}\n")
        adapter = FakeAdapter("siyuan", FakeNotes("", {}))
        with mock.patch("chassis.second_brain.factory.get_adapter", return_value=adapter):
            self.assertIn("SECOND BRAIN UNREACHABLE", self.run_load())

    def test_oversized_map_is_truncated(self):
        self.write_config(f"  map:\n    doc_id: {MAP_ID}\n")
        adapter = FakeAdapter("siyuan", FakeNotes("x" * (sbm.MAX_MAP_CHARS + 500), {}))
        with mock.patch("chassis.second_brain.factory.get_adapter", return_value=adapter):
            ctx = json.loads(self.run_load())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("map truncated", ctx)
        self.assertLess(len(ctx), sbm.MAX_MAP_CHARS + 1500)

    def test_dotenv_fills_only_unset_vars(self):
        (self.home / ".env").write_text(
            "# comment\nSBM_TEST_A=from-file\nexport SBM_TEST_B='quoted'\nSBM_TEST_C=file\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"SBM_TEST_C": "from-env"}):
            sbm.fill_env_from_dotenv(self.home)
            self.assertEqual(os.environ["SBM_TEST_A"], "from-file")
            self.assertEqual(os.environ["SBM_TEST_B"], "quoted")
            self.assertEqual(os.environ["SBM_TEST_C"], "from-env")
        for var in ("SBM_TEST_A", "SBM_TEST_B"):
            os.environ.pop(var, None)


    def test_mcp_json_env_fills_only_unset_vars(self):
        (self.home / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "sb": {"env": {"SBM_TEST_D": "from-mcp", "SBM_TEST_E": "<SBM_TEST_E>", "SBM_TEST_F": "mcp"}},
        }}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"SBM_TEST_F": "from-env"}):
            sbm.fill_env(self.home)
            self.assertEqual(os.environ["SBM_TEST_D"], "from-mcp")
            self.assertNotIn("SBM_TEST_E", os.environ)
            self.assertEqual(os.environ["SBM_TEST_F"], "from-env")
        os.environ.pop("SBM_TEST_D", None)


class DriftCommandTest(_InstallHomeCase):
    def run_drift(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(sbm.cmd_drift(), 0)
        return json.loads(buf.getvalue())

    def test_unconfigured_is_an_empty_array(self):
        self.write_config()
        self.assertEqual(self.run_drift(), [])

    def test_map_without_roots_is_a_probe_failure(self):
        self.write_config(f"  map:\n    doc_id: {MAP_ID}\n")
        self.assertEqual([r["kind"] for r in self.run_drift()], ["probe_failed"])

    def test_roots_mapping_drives_the_comparison(self):
        self.write_config(f"  map:\n    doc_id: {MAP_ID}\n    roots:\n      projects: /Projects\n")
        adapter = FakeAdapter("siyuan", FakeNotes(f"(({P1} 'Alpha'))", {"/Projects": [P1, P2]}))
        with mock.patch("chassis.second_brain.factory.get_adapter", return_value=adapter):
            report = self.run_drift()
        self.assertEqual([(r["kind"], r["doc_id"], r["root"]) for r in report], [("missing_from_map", P2, "projects")])


class WrapperTest(unittest.TestCase):
    """The shell wrappers, run for real against configs that need no network."""

    hook = CHASSIS / ".claude" / "hooks" / "load-second-brain-map.sh"
    gather = SCRIPTS / "gather-second-brain-map-drift.sh"

    def run_script(self, script: Path, config: str | None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            if config is not None:
                Path(tmp, "chassis.config.yaml").write_text(config, encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("CHASSIS_HOME", "CLAUDE_PROJECT_DIR", "SECOND_BRAIN_MAP_DOC")}
            env["CUSTOMER_HOME"] = tmp
            return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=60)

    def test_hook_is_silent_without_a_map(self):
        result = self.run_script(self.hook, "second_brain:\n  backend: siyuan\n")
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_hook_emits_banner_json_when_the_backend_is_missing(self):
        cfg = ("second_brain:\n  backend: obsidian\n  obsidian:\n    vault_path: /nonexistent/vault\n"
               "  map:\n    doc_id: _MAP.md\n")
        result = self.run_script(self.hook, cfg)
        self.assertEqual(result.returncode, 0)
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("SECOND BRAIN UNREACHABLE", ctx)
        self.assertIn("obsidian:_MAP.md", ctx)

    def test_gather_is_an_empty_array_without_a_map(self):
        result = self.run_script(self.gather, None)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, []))

    def test_gather_reports_probe_failed_when_the_backend_is_missing(self):
        cfg = ("second_brain:\n  backend: obsidian\n  obsidian:\n    vault_path: /nonexistent/vault\n"
               "  map:\n    doc_id: _MAP.md\n    roots:\n      projects: Projects/\n")
        result = self.run_script(self.gather, cfg)
        self.assertEqual(result.returncode, 0)
        self.assertEqual([r["kind"] for r in json.loads(result.stdout)], ["probe_failed"])


if __name__ == "__main__":
    unittest.main()
