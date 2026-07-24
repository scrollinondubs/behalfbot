#!/usr/bin/env python3
"""test_reconcile_mcp_config.py - drift detection + config reconciliation.

Drives chassis/scripts/reconcile-mcp-config.py, which reuses the REAL hydrator
(chassis/scripts/hydrate-mcp-json.py) for all gating. Fixtures here are fully
SYNTHETIC (svc_* server names, made-up flag paths) so nothing depends on the
shipped template or any real install's server set.

Coverage:
  - the three sets: PRESENT_BUT_WOULD_DROP, WOULD_EMIT_BUT_MISSING, CONSISTENT
  - the exact preserving flag resolved for each would-drop server
  - the placeholder check (would-emit server whose .env lacks a token)
  - the host-vs-container path-model flag
  - the real-case shape with synthetic names: 15 live, 6 enabled, 9 gated ->
    exactly those 9 in PRESENT_BUT_WOULD_DROP with the correct flags
  - --fix: surgical YAML edit (modify existing leaf, insert new leaf, create a
    missing intermediate mapping), comment preservation, backup, idempotency
  - !=-gated servers are surfaced but never auto-fixed

Run:
    python3 -m pytest chassis/scripts/tests/test_reconcile_mcp_config.py -v
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILER_PATH = REPO_ROOT / "chassis" / "scripts" / "reconcile-mcp-config.py"

_spec = importlib.util.spec_from_file_location("reconcile_mcp_config", RECONCILER_PATH)
reconcile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reconcile)

HYDRATOR = reconcile.load_hydrator()


def _core(role="core"):
    return {"_role": role, "command": "npx", "args": ["-y", "pkg"]}


def _gated(predicate, env=None):
    entry = {"_enable_when": predicate, "command": "npx", "args": ["-y", "pkg"]}
    if env is not None:
        entry["env"] = env
    return entry


def _report(template, config, live, env=None):
    return reconcile.compute_report(HYDRATOR, live, config, template, env or {})


def _live(*names):
    return {"mcpServers": {n: {"command": "npx", "args": ["-y", "pkg"]} for n in names}}


class ThreeSetsTest(unittest.TestCase):
    TEMPLATE = {
        "mcpServers": {
            "svc_core": _core(),
            "svc_on": _gated("chassis.config.yaml.modules.on == true"),
            "svc_off": _gated("chassis.config.yaml.modules.off == true"),
            "svc_wants": _gated("chassis.config.yaml.modules.wants == true"),
        }
    }
    CONFIG = {"modules": {"on": True, "off": False, "wants": True}}

    def test_present_but_would_drop(self):
        # svc_off is live but config never enables it -> the dangerous set.
        live = _live("svc_core", "svc_on", "svc_off")
        report = _report(self.TEMPLATE, self.CONFIG, live)
        names = [i["server"] for i in report["present_but_would_drop"]]
        self.assertEqual(names, ["svc_off"])
        item = report["present_but_would_drop"][0]
        self.assertEqual(item["enable_when"], "chassis.config.yaml.modules.off == true")
        self.assertTrue(item["auto_fixable"])
        self.assertEqual(item["assignments"][0]["path"], "modules.off")
        self.assertEqual(item["assignments"][0]["value_literal"], "true")

    def test_would_emit_but_missing(self):
        # svc_wants is enabled in config but not present live -> info set.
        live = _live("svc_core", "svc_on")
        report = _report(self.TEMPLATE, self.CONFIG, live)
        self.assertIn("svc_wants", report["would_emit_but_missing"])
        self.assertEqual(report["present_but_would_drop"], [])

    def test_consistent(self):
        live = _live("svc_core", "svc_on", "svc_wants")
        report = _report(self.TEMPLATE, self.CONFIG, live)
        self.assertEqual(report["consistent"], ["svc_core", "svc_on", "svc_wants"])
        self.assertFalse(report["drift"])

    def test_underscore_keys_in_live_are_ignored(self):
        live = _live("svc_core", "svc_on", "svc_wants")
        live["mcpServers"]["_divider"] = {"_role": "doc"}
        report = _report(self.TEMPLATE, self.CONFIG, live)
        self.assertNotIn("_divider", report["consistent"])
        self.assertEqual(report["present_but_would_drop"], [])


class PlaceholderCheckTest(unittest.TestCase):
    TEMPLATE = {
        "mcpServers": {
            "svc_keyed": _gated(
                "chassis.config.yaml.modules.keyed == true",
                env={"API_KEY": "<SVC_TOKEN>"},
            ),
        }
    }
    CONFIG = {"modules": {"keyed": True}}

    def setUp(self):
        # substitute_placeholders falls back to os.environ, so isolate it or a
        # real SVC_TOKEN on the dev box would mask the missing-token case.
        patcher = mock.patch.dict("os.environ", {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_token_is_flagged(self):
        live = _live("svc_keyed")
        report = _report(self.TEMPLATE, self.CONFIG, live, env={})
        self.assertEqual(report["broken_placeholders"], {"svc_keyed": ["SVC_TOKEN"]})
        self.assertTrue(report["drift"])

    def test_present_token_is_not_flagged(self):
        live = _live("svc_keyed")
        report = _report(self.TEMPLATE, self.CONFIG, live, env={"SVC_TOKEN": "x"})
        self.assertEqual(report["broken_placeholders"], {})


class PathModelTest(unittest.TestCase):
    TEMPLATE = {
        "mcpServers": {
            "svc_adapter": {
                "_enable_when": "chassis.config.yaml.modules.adapter == true",
                "command": "python3",
                "args": ["${CHASSIS_ROOT:-/app/chassis}/second_brain/mcp_server.py"],
            }
        }
    }
    CONFIG = {"modules": {"adapter": True}}

    def test_host_path_where_template_renders_container_path(self):
        live = {
            "mcpServers": {
                "svc_adapter": {
                    "command": "python3",
                    "args": ["/Users/someone/.behalfbot/chassis/chassis/second_brain/mcp_server.py"],
                }
            }
        }
        report = _report(self.TEMPLATE, self.CONFIG, live)
        self.assertEqual(len(report["path_model_mismatches"]), 1)
        m = report["path_model_mismatches"][0]
        self.assertEqual(m["server"], "svc_adapter")
        self.assertEqual(m["field"], "args[0]")
        self.assertIn("/app/chassis", m["template"])
        self.assertTrue(m["live"].startswith("/Users/"))

    def test_matching_container_path_is_not_flagged(self):
        live = {
            "mcpServers": {
                "svc_adapter": {
                    "command": "python3",
                    "args": ["${CHASSIS_ROOT:-/app/chassis}/second_brain/mcp_server.py"],
                }
            }
        }
        report = _report(self.TEMPLATE, self.CONFIG, live)
        self.assertEqual(report["path_model_mismatches"], [])


class NotEqualGateIsNotAutoFixedTest(unittest.TestCase):
    # second-brain shape: a server gated on mode != 'adapter'. When config is in
    # adapter mode the server drops, but flipping mode would drop the adapter
    # server instead - so it is surfaced with a note and never auto-fixed.
    TEMPLATE = {
        "mcpServers": {
            "svc_native": _gated(
                "chassis.config.yaml.second_brain.backend == 'siyuan' "
                "&& chassis.config.yaml.second_brain.mode != 'adapter'"
            ),
        }
    }
    CONFIG = {"second_brain": {"backend": "siyuan", "mode": "adapter"}}

    def test_not_equal_clause_blocks_auto_fix(self):
        live = _live("svc_native")
        report = _report(self.TEMPLATE, self.CONFIG, live)
        item = report["present_but_would_drop"][0]
        self.assertEqual(item["server"], "svc_native")
        self.assertFalse(item["auto_fixable"])
        self.assertTrue(any("!=" in n for n in item["notes"]))


# ---------------------------------------------------------------------------
# Real-case shape, synthetic names: 15 live, 6 enabled, 9 gated-and-dropped.
# ---------------------------------------------------------------------------

REAL_SHAPE_TEMPLATE = {
    "mcpServers": {
        # 4 ungated core servers - always emit.
        "svc_alpha": _core(),
        "svc_bravo": _core(),
        "svc_charlie": _core(),
        "svc_delta": _core(),
        # 2 gated servers the config DOES enable -> also emit (6 total).
        "svc_echo": _gated("chassis.config.yaml.modules.echo == true"),
        "svc_foxtrot": _gated("chassis.config.yaml.second_brain.backend == 'siyuan'"),
        # 9 gated servers the config never enables -> the drop set.
        "svc_turso": _gated("chassis.config.yaml.modules.turso == true"),
        "svc_amp": _gated("chassis.config.yaml.modules.amplitude == true"),
        "svc_tavily": _gated("chassis.config.yaml.modules.research.tavily == true"),
        "svc_brave": _gated("chassis.config.yaml.modules.research.brave == true"),
        "svc_frame0": _gated("chassis.config.yaml.modules.frame0 == true"),
        "svc_loom": _gated("chassis.config.yaml.modules.loom_mcp == true"),
        "svc_n8n": _gated("chassis.config.yaml.modules.n8n == true"),
        "svc_remarkable": _gated("chassis.config.yaml.modules.remarkable == true"),
        "svc_oura": _gated("chassis.config.yaml.modules.bfl.strava_oura_reconcile == true"),
    }
}

REAL_SHAPE_LIVE = _live(
    "svc_alpha", "svc_bravo", "svc_charlie", "svc_delta", "svc_echo", "svc_foxtrot",
    "svc_turso", "svc_amp", "svc_tavily", "svc_brave", "svc_frame0", "svc_loom",
    "svc_n8n", "svc_remarkable", "svc_oura",
)

# A synthetic chassis.config.yaml with comments and nesting, enabling only 6.
# modules.research is absent entirely (svc_tavily/svc_brave need a new mapping);
# modules.bfl exists with strava_oura_reconcile: false (svc_oura is a modify).
REAL_SHAPE_CONFIG_YAML = """\
# Synthetic chassis.config.yaml for the reconcile test.
version: 1

second_brain:
  backend: siyuan                           # enables svc_foxtrot
  mode: direct

modules:
  echo: true                                # enables svc_echo
  admin: true                               # unrelated, must survive untouched
  bfl:
    enabled: false
    strava_oura_reconcile: false            # svc_oura gate - flips to true
    fdc_enrich: false
  google:
    gmail: false

trust_line:
  calendar: read_only
"""

DROPPED_9 = {
    "svc_turso", "svc_amp", "svc_tavily", "svc_brave", "svc_frame0",
    "svc_loom", "svc_n8n", "svc_remarkable", "svc_oura",
}

EXPECTED_FLAGS = {
    "svc_turso": ("modules.turso", "true"),
    "svc_amp": ("modules.amplitude", "true"),
    "svc_tavily": ("modules.research.tavily", "true"),
    "svc_brave": ("modules.research.brave", "true"),
    "svc_frame0": ("modules.frame0", "true"),
    "svc_loom": ("modules.loom_mcp", "true"),
    "svc_n8n": ("modules.n8n", "true"),
    "svc_remarkable": ("modules.remarkable", "true"),
    "svc_oura": ("modules.bfl.strava_oura_reconcile", "true"),
}


class RealCaseShapeTest(unittest.TestCase):
    def _config(self):
        import io
        # Reuse the hydrator's YAML loader against the synthetic text.
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(REAL_SHAPE_CONFIG_YAML)
            path = f.name
        self.addCleanup(os.unlink, path)
        return HYDRATOR.load_yaml(path)

    def test_exactly_nine_would_drop_with_correct_flags(self):
        report = _report(REAL_SHAPE_TEMPLATE, self._config(), REAL_SHAPE_LIVE)
        dropped = {i["server"] for i in report["present_but_would_drop"]}
        self.assertEqual(dropped, DROPPED_9)
        for item in report["present_but_would_drop"]:
            self.assertTrue(item["auto_fixable"], item["server"])
            path, value = EXPECTED_FLAGS[item["server"]]
            self.assertEqual(item["assignments"][0]["path"], path)
            self.assertEqual(item["assignments"][0]["value_literal"], value)

    def test_six_consistent_and_drift_true(self):
        report = _report(REAL_SHAPE_TEMPLATE, self._config(), REAL_SHAPE_LIVE)
        self.assertEqual(len(report["consistent"]), 6)
        self.assertTrue(report["drift"])


class FixEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.dir, "chassis.config.yaml")
        with open(self.config_path, "w") as f:
            f.write(REAL_SHAPE_CONFIG_YAML)

    def _run_fix(self):
        config = HYDRATOR.load_yaml(self.config_path)
        report = _report(REAL_SHAPE_TEMPLATE, config, REAL_SHAPE_LIVE)
        return reconcile.apply_fix(self.config_path, report)

    def test_fix_enables_all_nine_then_render_drops_nothing(self):
        result = self._run_fix()
        self.assertTrue(result["changed"])
        self.assertTrue(os.path.exists(result["backup"]))

        # After the fix, a fresh report must show zero would-drop and every live
        # server consistent.
        config = HYDRATOR.load_yaml(self.config_path)
        report = _report(REAL_SHAPE_TEMPLATE, config, REAL_SHAPE_LIVE)
        self.assertEqual(report["present_but_would_drop"], [])
        self.assertEqual(len(report["consistent"]), 15)

    def test_fix_preserves_unrelated_keys_and_comments(self):
        self._run_fix()
        text = Path(self.config_path).read_text()
        self.assertIn("admin: true", text)
        self.assertIn("# unrelated, must survive untouched", text)
        self.assertIn("backend: siyuan", text)
        # The oura flag was flipped in place, keeping its trailing comment.
        self.assertIn("strava_oura_reconcile: true", text)
        self.assertIn("# svc_oura gate", text)

    def test_fix_creates_missing_research_mapping(self):
        self._run_fix()
        config = HYDRATOR.load_yaml(self.config_path)
        self.assertIs(config["modules"]["research"]["tavily"], True)
        self.assertIs(config["modules"]["research"]["brave"], True)

    def test_fix_is_idempotent(self):
        self._run_fix()
        second = self._run_fix()
        self.assertFalse(second["changed"])
        self.assertIsNone(second["backup"])


class SetYamlPathUnitTest(unittest.TestCase):
    def _apply(self, text, dotted, value):
        lines = text.split("\n")
        action = reconcile.set_yaml_path(lines, dotted.split("."), value)
        return "\n".join(lines), action

    def test_modify_existing_leaf_preserves_comment_column(self):
        text = "modules:\n  flag: false            # keep me\n"
        out, action = self._apply(text, "modules.flag", "true")
        self.assertEqual(action, "modified")
        self.assertIn("flag: true", out)
        self.assertIn("# keep me", out)

    def test_insert_new_leaf_under_existing_parent(self):
        text = "modules:\n  existing: true\n"
        out, action = self._apply(text, "modules.turso", "true")
        self.assertEqual(action, "inserted")
        self.assertIn("  turso: true", out)
        self.assertIn("added by reconcile-mcp-config", out)

    def test_insert_creates_missing_intermediate_mapping(self):
        text = "modules:\n  existing: true\n"
        out, action = self._apply(text, "modules.research.brave", "true")
        self.assertEqual(action, "inserted")
        self.assertIn("  research:", out)
        self.assertIn("    brave: true", out)

    def test_noop_when_value_already_set(self):
        text = "modules:\n  flag: true\n"
        _out, action = self._apply(text, "modules.flag", "true")
        self.assertEqual(action, "noop")

    def test_does_not_confuse_sibling_blocks(self):
        text = "modules:\n  bfl:\n    a: false\n  other:\n    a: false\n"
        out, _action = self._apply(text, "modules.bfl.a", "true")
        # Only the bfl.a leaf flips; other.a stays false.
        self.assertIn("  bfl:\n    a: true", out)
        self.assertIn("  other:\n    a: false", out)


class CliTest(unittest.TestCase):
    """Exercise main() through argv, asserting exit codes and read-only-on-mcp."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.template = os.path.join(self.dir, ".mcp.json.template")
        self.config = os.path.join(self.dir, "chassis.config.yaml")
        self.mcp = os.path.join(self.dir, ".mcp.json")
        self.env = os.path.join(self.dir, ".env")
        import json as _json
        with open(self.template, "w") as f:
            _json.dump(REAL_SHAPE_TEMPLATE, f)
        with open(self.config, "w") as f:
            f.write(REAL_SHAPE_CONFIG_YAML)
        with open(self.mcp, "w") as f:
            _json.dump(REAL_SHAPE_LIVE, f)
        with open(self.env, "w") as f:
            f.write("")

    def _argv(self, *extra):
        return ["--config", self.config, "--template", self.template,
                "--mcp", self.mcp, "--env", self.env, *extra]

    def test_check_exits_1_on_drift(self):
        rc = reconcile.main(self._argv())
        self.assertEqual(rc, 1)

    def test_fix_does_not_touch_mcp_json(self):
        before = Path(self.mcp).read_bytes()
        reconcile.main(self._argv("--fix"))
        self.assertEqual(Path(self.mcp).read_bytes(), before)

    def test_fix_then_check_exits_0(self):
        reconcile.main(self._argv("--fix"))
        rc = reconcile.main(self._argv())
        self.assertEqual(rc, 0)

    def test_missing_live_file_exits_2(self):
        os.unlink(self.mcp)
        rc = reconcile.main(self._argv())
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
