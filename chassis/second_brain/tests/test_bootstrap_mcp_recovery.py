#!/usr/bin/env python3
"""test_bootstrap_mcp_recovery.py - bootstrap-mcp-config.sh recovery path.

bootstrap-mcp-config.sh used to carry a second renderer: a sed+jq pass that ran
whenever chassis.config.yaml or PyYAML was missing. It stripped `_enable_when`
without evaluating it, so it registered every feature-gated server at once -
siyuan AND notion AND secondbrain on one install, plus Google entries on
installs with no OAuth token on disk. PR #58 flagged it; this is the fix.

Rather than teach the sed path the predicate grammar, the two conditions that
selected it now resolve inside hydrate-mcp-json.py, and the shell script
delegates unconditionally. These tests cover exactly those two conditions,
since they are the ones that used to route around the hydrator:

  no PyYAML -> minimal fallback parser, same servers as PyYAML would give.
  no config -> empty config, which drops `==`-gated servers and keeps the
               ungated core.

The second is the one worth stating precisely: "fail closed" here means a
recovering install gets a MINIMAL .mcp.json, not a maximal one. Registering
three second-brain servers on a broken install is how you get a Claude session
that reaches for whichever tool it saw first.

Run:
    python3 -m pytest chassis/second_brain/tests/test_bootstrap_mcp_recovery.py -v
"""
from __future__ import annotations

import builtins
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

TEMPLATE_PATH = REPO_ROOT / "chassis" / ".mcp.json.template"
HYDRATOR_PATH = REPO_ROOT / "chassis" / "scripts" / "hydrate-mcp-json.py"
BOOTSTRAP_PATH = REPO_ROOT / "chassis" / "scripts" / "bootstrap-mcp-config.sh"

_spec = importlib.util.spec_from_file_location("hydrate_mcp_json", HYDRATOR_PATH)
hydrator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hydrator)

SECOND_BRAIN_SERVERS = {"siyuan", "notion", "secondbrain"}

SAMPLE_CONFIG = """\
second_brain:
  backend: siyuan
  mode: direct
modules:
  google:
    gmail: true
    calendar: false
  research:
    brave: true
    tavily: false
"""


class NoPyYamlFallbackTest(unittest.TestCase):
    """`import yaml` failing must not change which servers render."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Path(self._tmp.name) / "chassis.config.yaml"
        self.config.write_text(SAMPLE_CONFIG, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    @staticmethod
    def _without_pyyaml():
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("No module named 'yaml'")
            return real_import(name, *args, **kwargs)

        return mock.patch.object(builtins, "__import__", fake_import)

    def test_fallback_parser_produces_the_same_config(self) -> None:
        with_pyyaml = hydrator.load_yaml(str(self.config))
        with self._without_pyyaml():
            without_pyyaml = hydrator.load_yaml(str(self.config))
        self.assertEqual(with_pyyaml, without_pyyaml)

    def test_fallback_parser_produces_the_same_servers(self) -> None:
        template = hydrator.load_json(str(TEMPLATE_PATH))
        rendered_with, _ = hydrator.hydrate(
            hydrator.load_yaml(str(self.config)), template, env={}
        )
        with self._without_pyyaml():
            rendered_without, _ = hydrator.hydrate(
                hydrator.load_yaml(str(self.config)), template, env={}
            )
        self.assertEqual(
            set(rendered_with["mcpServers"]), set(rendered_without["mcpServers"])
        )
        # And the gate actually did something - siyuan in, notion + secondbrain out.
        present = SECOND_BRAIN_SERVERS & set(rendered_without["mcpServers"])
        self.assertEqual(present, {"siyuan"})

    def test_fallback_still_evaluates_module_gates(self) -> None:
        with self._without_pyyaml():
            config = hydrator.load_yaml(str(self.config))
        template = hydrator.load_json(str(TEMPLATE_PATH))
        servers = hydrator.hydrate(config, template, env={})[0]["mcpServers"]
        self.assertIn("gmail", servers)                 # gmail: true
        self.assertNotIn("google-calendar", servers)    # calendar: false
        self.assertIn("brave-search", servers)          # brave: true
        self.assertNotIn("tavily", servers)             # tavily: false


class MissingConfigTest(unittest.TestCase):
    """No chassis.config.yaml renders core-only, never everything."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.output = Path(self._tmp.name) / ".mcp.json"
        self.addCleanup(self._tmp.cleanup)

    def _render_with_empty_config(self) -> dict:
        template = hydrator.load_json(str(TEMPLATE_PATH))
        return hydrator.hydrate({}, template, env={})[0]["mcpServers"]

    def test_no_second_brain_server_is_registered(self) -> None:
        """The exact bug: three second-brain servers on one install."""
        servers = self._render_with_empty_config()
        self.assertEqual(SECOND_BRAIN_SERVERS & set(servers), set())

    def test_no_google_server_is_registered(self) -> None:
        # These are the entries #57 called out: registered on installs that
        # never enabled Google and have no OAuth token on disk.
        servers = self._render_with_empty_config()
        for name in ("gmail", "google-calendar", "google-sheets"):
            self.assertNotIn(name, servers)

    def test_ungated_core_servers_survive(self) -> None:
        # Fail-closed must not mean fail-empty - a recovered install still needs
        # the servers that were never gated in the first place.
        servers = self._render_with_empty_config()
        self.assertIn("memory", servers)

    def test_hydrator_cli_writes_a_file_when_config_is_absent(self) -> None:
        """End to end through the real CLI, since that is what the shell calls."""
        result = subprocess.run(
            [sys.executable, str(HYDRATOR_PATH),
             "--config", str(Path(self._tmp.name) / "does-not-exist.yaml"),
             "--template", str(TEMPLATE_PATH),
             "--output", str(self.output)],
            capture_output=True, text=True,
        )
        self.assertIn(result.returncode, (0, 2))  # 2 = unresolved placeholders
        self.assertIn("config not found", result.stderr)
        self.assertTrue(self.output.exists())
        import json
        servers = json.loads(self.output.read_text())["mcpServers"]
        self.assertEqual(SECOND_BRAIN_SERVERS & set(servers), set())

    def test_hydrator_cli_still_errors_on_a_missing_template(self) -> None:
        # A missing config is recoverable; a missing template is not - there is
        # nothing to render from.
        result = subprocess.run(
            [sys.executable, str(HYDRATOR_PATH),
             "--config", str(Path(self._tmp.name) / "nope.yaml"),
             "--template", str(Path(self._tmp.name) / "nope.json"),
             "--output", str(self.output)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("template not found", result.stderr)


class BootstrapDelegationTest(unittest.TestCase):
    """The shell script must have no second renderer left to drift."""

    def setUp(self) -> None:
        self.script = BOOTSTRAP_PATH.read_text(encoding="utf-8")

    def test_script_parses(self) -> None:
        result = subprocess.run(["bash", "-n", str(BOOTSTRAP_PATH)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_sed_or_jq_rendering_remains(self) -> None:
        for token in ("PLACEHOLDERS=(", "jq empty", "del(._README)", "unfilled"):
            self.assertNotIn(token, self.script,
                             f"sed+jq renderer fragment {token!r} still present")

    def test_delegation_is_not_conditional_on_pyyaml_or_config(self) -> None:
        # The old gate was `[[ -f "$CONFIG" ]] && python3 -c 'import yaml'`.
        # Both conditions moved into the hydrator; neither may gate here again.
        self.assertNotIn("import yaml", self.script)
        self.assertNotIn('-f "$CONFIG"', self.script)

    def test_hydrator_is_invoked(self) -> None:
        self.assertIn('python3 "$HYDRATOR"', self.script)


if __name__ == "__main__":
    unittest.main()
