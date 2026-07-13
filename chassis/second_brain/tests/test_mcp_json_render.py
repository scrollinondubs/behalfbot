#!/usr/bin/env python3
"""test_mcp_json_render.py - .mcp.json renders correctly for every (backend, mode) combination.

Drives the REAL template (chassis/.mcp.json.template) through the REAL
hydrator (chassis/scripts/hydrate-mcp-json.py) with fixture configs, and
asserts which second-brain servers land in the output:

    (siyuan,   direct)  -> siyuan only
    (siyuan,   adapter) -> secondbrain only
    (notion,   direct)  -> notion only
    (notion,   adapter) -> secondbrain only
    (obsidian, direct)  -> NO second-brain server at all. This combination is
                           effectively invalid: there is no Obsidian MCP server
                           in the template because none exists that fits the
                           chassis (the community options need the Obsidian
                           desktop app + Local REST API plugin, which a
                           headless container install does not run). Obsidian
                           installs are adapter-mode-only.
    (obsidian, adapter) -> secondbrain only

Plus the compatibility case that protects existing installs: a config with NO
mode key renders exactly like mode: direct.

Run:
    python3 -m pytest chassis/second_brain/tests/test_mcp_json_render.py -v
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

TEMPLATE_PATH = REPO_ROOT / "chassis" / ".mcp.json.template"
HYDRATOR_PATH = REPO_ROOT / "chassis" / "scripts" / "hydrate-mcp-json.py"

_spec = importlib.util.spec_from_file_location("hydrate_mcp_json", HYDRATOR_PATH)
hydrator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hydrator)

SECOND_BRAIN_SERVERS = {"siyuan", "notion", "secondbrain"}


def _render(backend: str, mode: str | None) -> dict:
    sb: dict = {"backend": backend}
    if mode is not None:
        sb["mode"] = mode
    config = {"second_brain": sb}
    template = hydrator.load_json(str(TEMPLATE_PATH))
    hydrated, _unresolved = hydrator.hydrate(config, template, env={})
    return hydrated["mcpServers"]


class McpJsonRenderMatrixTest(unittest.TestCase):
    def _assert_second_brain_servers(self, servers: dict, expected: set[str]) -> None:
        present = SECOND_BRAIN_SERVERS & set(servers)
        self.assertEqual(present, expected)

    def test_siyuan_direct(self) -> None:
        self._assert_second_brain_servers(_render("siyuan", "direct"), {"siyuan"})

    def test_siyuan_adapter(self) -> None:
        self._assert_second_brain_servers(_render("siyuan", "adapter"), {"secondbrain"})

    def test_notion_direct(self) -> None:
        self._assert_second_brain_servers(_render("notion", "direct"), {"notion"})

    def test_notion_adapter(self) -> None:
        self._assert_second_brain_servers(_render("notion", "adapter"), {"secondbrain"})

    def test_obsidian_direct_has_no_second_brain_server(self) -> None:
        # Invalid-but-tolerated combination: no native Obsidian MCP server
        # exists, so direct mode leaves the install with no second-brain MCP.
        self._assert_second_brain_servers(_render("obsidian", "direct"), set())

    def test_obsidian_adapter(self) -> None:
        self._assert_second_brain_servers(
            _render("obsidian", "adapter"), {"secondbrain"}
        )

    def test_missing_mode_means_direct(self) -> None:
        # Installs that predate the mode key must see zero change.
        self._assert_second_brain_servers(_render("siyuan", None), {"siyuan"})
        self._assert_second_brain_servers(_render("notion", None), {"notion"})
        self._assert_second_brain_servers(_render("obsidian", None), set())

    def test_default_on_servers_survive_in_both_modes(self) -> None:
        for mode in ("direct", "adapter"):
            servers = _render("siyuan", mode)
            for name in ("memory", "playwright", "context7", "github"):
                self.assertIn(name, servers, f"{name} missing in mode={mode}")

    def test_adapter_entry_shape(self) -> None:
        servers = _render("notion", "adapter")
        entry = servers["secondbrain"]
        self.assertEqual(entry["command"], "python3")
        self.assertIn("second_brain/mcp_server.py", entry["args"][0])
        # Meta keys must be stripped from the hydrated output.
        self.assertFalse([k for k in entry if k.startswith("_")])


class EnableWhenEvaluatorTest(unittest.TestCase):
    CONFIG = {"second_brain": {"backend": "siyuan", "mode": "adapter"}}

    def _eval(self, predicate: str, config=None) -> bool:
        return hydrator.evaluate_enable_when(
            predicate, self.CONFIG if config is None else config
        )

    def test_equality(self) -> None:
        self.assertTrue(self._eval("chassis.config.yaml.second_brain.backend == 'siyuan'"))
        self.assertFalse(self._eval("chassis.config.yaml.second_brain.backend == 'notion'"))

    def test_inequality(self) -> None:
        self.assertFalse(self._eval("chassis.config.yaml.second_brain.mode != 'adapter'"))
        self.assertTrue(self._eval("chassis.config.yaml.second_brain.mode != 'direct'"))

    def test_inequality_on_missing_path_is_true(self) -> None:
        config = {"second_brain": {"backend": "siyuan"}}
        self.assertTrue(
            self._eval("chassis.config.yaml.second_brain.mode != 'adapter'", config)
        )

    def test_equality_on_missing_path_is_false(self) -> None:
        config = {"second_brain": {"backend": "siyuan"}}
        self.assertFalse(
            self._eval("chassis.config.yaml.second_brain.mode == 'adapter'", config)
        )

    def test_conjunction(self) -> None:
        self.assertTrue(
            self._eval(
                "chassis.config.yaml.second_brain.backend == 'siyuan' "
                "&& chassis.config.yaml.second_brain.mode == 'adapter'"
            )
        )
        self.assertFalse(
            self._eval(
                "chassis.config.yaml.second_brain.backend == 'siyuan' "
                "&& chassis.config.yaml.second_brain.mode != 'adapter'"
            )
        )

    def test_unparsable_predicates_are_false(self) -> None:
        self.assertFalse(self._eval("garbage"))
        self.assertFalse(self._eval(""))
        self.assertFalse(self._eval("a == 'x' && "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
