#!/usr/bin/env python3
"""test_bootstrap_claude_project.py - the Claude Code project wiring (behalfbot#264).

The CLAUDE.md template promised skills, a sub-agent, a SessionStart map hook
and a seeded notes store, and a fresh install got none of them: some files did
not ship at all, the rest shipped where Claude Code never looks. These tests
drive bootstrap-claude-project.py against temp installs and check what a
session would actually find, plus that every path the template names exists.

Run:
    python3 -m pytest chassis/scripts/tests/test_bootstrap_claude_project.py -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

CHASSIS = Path(__file__).resolve().parents[2]
SCRIPT = CHASSIS / "scripts" / "bootstrap-claude-project.py"
spec = importlib.util.spec_from_file_location("bootstrap_claude_project", SCRIPT)
bcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bcp)

CONFIG = """identity:
  installer_name: Alex Example
  principal:
    full_name: Alex Example
  assistant:
    name: Robo
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "install"
        self.home.mkdir()
        (self.home / "chassis.config.yaml").write_text(CONFIG)
        self.store = base / "store"
        self.env = mock.patch.dict(os.environ, {"BOOTSTRAP_SKIP_PLUGIN_INSTALL": "1"})
        self.env.start()
        os.environ.pop("CLAUDE_AUTO_MEMORY_DIR", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_wirer(self, *extra) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = bcp.main(["--customer-home", str(self.home), "--memory-dir", str(self.store), *extra])
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    @property
    def settings(self) -> dict:
        return json.loads((self.home / ".claude" / "settings.json").read_text())


class FreshInstallTest(Base):
    def test_everything_lands_where_claude_code_looks(self):
        self.run_wirer()
        for rel in (
            ".claude/skills/software-engineering/SKILL.md",
            ".claude/skills/build-plans/SKILL.md",
            ".claude/agents/software-engineering.md",
            ".claude/hooks/chassis-hook.sh",
        ):
            self.assertTrue((self.home / rel).is_file(), rel)
        self.assertTrue(os.access(self.home / ".claude/hooks/chassis-hook.sh", os.X_OK))
        for rel in (".claude/skills/software-engineering/SKILL.md", ".claude/skills/build-plans/SKILL.md",
                    ".claude/agents/software-engineering.md"):
            head = (self.home / rel).read_text().split("---")[1]
            self.assertRegex(head, r"\nname: [a-z-]+\n")
            self.assertIn("description:", head)

    def test_settings_register_map_hook_guardrails_and_html_plan(self):
        self.run_wirer()
        s = self.settings
        session = [h["command"] for g in s["hooks"]["SessionStart"] for h in g["hooks"]]
        pre = [h["command"] for g in s["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertTrue(any("load-second-brain-map.sh" in c for c in session))
        self.assertTrue(any("guardrails.sh" in c for c in pre))
        self.assertTrue(s["enabledPlugins"]["html-plan@claude-community"])
        self.assertEqual(s["extraKnownMarketplaces"]["claude-community"]["source"]["repo"],
                         "anthropics/claude-plugins-community")

    def test_notes_store_seeded_and_integrity_clean(self):
        self.run_wirer()
        memory = (self.store / "MEMORY.md").read_text()
        self.assertTrue(memory.startswith("# Robo Memory"))
        self.assertNotIn("{{", memory)
        self.assertIn("Alex Example", (self.store / "feedback_never_deceive.md").read_text())
        self.assertTrue((self.store / "index-communication.md").is_file())
        result = subprocess.run(
            ["python3", str(CHASSIS / "scripts" / "memory-index-build.py"), "--store", str(self.store), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_placeholder_identity_falls_back(self):
        (self.home / "chassis.config.yaml").write_text("identity:\n  assistant:\n    name: <agent-name>\n")
        with mock.patch.dict(os.environ, {"INSTANCE_NAME": "", "INSTALLER_NAME": ""}):
            self.run_wirer()
        self.assertTrue((self.store / "MEMORY.md").read_text().startswith("# Assistant Memory"))


class RerunTest(Base):
    def test_rerun_changes_nothing_and_keeps_edits(self):
        self.run_wirer()
        skill = self.home / ".claude/skills/software-engineering/SKILL.md"
        skill.write_text(skill.read_text() + "\nInstall-specific rule.\n")
        (self.store / "MEMORY.md").write_text("# mine\n")
        before = (self.home / ".claude" / "settings.json").read_text()
        out = self.run_wirer()
        self.assertIn("differs from the chassis copy; kept", out)
        self.assertIn("Install-specific rule.", skill.read_text())
        self.assertEqual((self.store / "MEMORY.md").read_text(), "# mine\n")
        self.assertEqual((self.home / ".claude" / "settings.json").read_text(), before)

    def test_existing_settings_are_merged_not_replaced(self):
        existing = {
            "hooks": {"PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "my-gate.sh"}]}]},
            "enabledPlugins": {"html-plan@claude-community": False},
            "permissions": {"allow": ["Bash(ls:*)"]},
        }
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps(existing))
        out = self.run_wirer()
        s = self.settings
        self.assertEqual(s["permissions"], existing["permissions"])
        self.assertEqual(s["hooks"]["PreToolUse"], existing["hooks"]["PreToolUse"])
        self.assertFalse(s["enabledPlugins"]["html-plan@claude-community"])
        self.assertTrue(any("load-second-brain-map.sh" in h["command"]
                            for g in s["hooks"]["SessionStart"] for h in g["hooks"]))
        self.assertIn("registers no guardrails hook", out)
        self.run_wirer()
        self.assertEqual(len(self.settings["hooks"]["SessionStart"]), 1)

    def test_invalid_settings_left_untouched(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text("{not json")
        out = self.run_wirer()
        self.assertIn("not valid JSON", out)
        self.assertEqual((self.home / ".claude" / "settings.json").read_text(), "{not json")

    def test_dry_run_writes_nothing(self):
        self.run_wirer("--dry-run")
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse(self.store.exists())


class HookShimTest(Base):
    def run_hook(self, name: str, payload: dict, env: dict | None = None) -> str:
        full_env = {k: v for k, v in os.environ.items() if k not in ("CHASSIS_ROOT", "CHASSIS_HOME", "CUSTOMER_HOME")}
        full_env.update(env or {})
        result = subprocess.run(
            ["bash", str(self.home / ".claude/hooks/chassis-hook.sh"), name],
            input=json.dumps(payload), capture_output=True, text=True, env=full_env,
        )
        return result.stdout

    def test_guardrails_run_through_the_shim(self):
        self.run_wirer()
        push = "git push " + "--force origin feat/x"
        out = self.run_hook("guardrails.sh", {"tool_name": "Bash", "tool_input": {"command": push}})
        self.assertIn('"permissionDecision": "deny"', out)
        out = self.run_hook("guardrails.sh", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertNotIn("deny", out)

    def test_missing_chassis_fails_closed(self):
        self.run_wirer()
        shim = self.home / ".claude/hooks/chassis-hook.sh"
        shim.write_text(shim.read_text().replace(str(CHASSIS), "/nonexistent/chassis"))
        out = self.run_hook("guardrails.sh", {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                            {"HOME": str(self.home)})
        if Path("/app/chassis/.claude/hooks/guardrails.sh").is_file():
            self.skipTest("running inside a chassis container; /app/chassis is a real fallback")
        self.assertIn('"permissionDecision":"deny"', out)
        out = self.run_hook("load-second-brain-map.sh", {}, {"HOME": str(self.home)})
        self.assertIn("SECOND BRAIN UNREACHABLE", out)

    def test_bad_hook_name_rejected(self):
        self.run_wirer()
        result = subprocess.run(["bash", str(self.home / ".claude/hooks/chassis-hook.sh"), "../../evil.sh"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)


class TemplatePathsTest(unittest.TestCase):
    """Every chassis path CLAUDE.md.template names must ship, and every
    .claude/ path it names must be one the wirer installs. That gap is the
    original bug: the template listed three chassis skills, one of which
    existed."""

    def test_template_paths_exist(self):
        text = (CHASSIS / "CLAUDE.md.template").read_text()
        repo = CHASSIS.parent
        installed = {str(rel) for _, rel in bcp.CORE_FILES}
        installed |= {".claude/settings.json", ".claude/agents/", ".claude/skills/"}
        missing = []
        for path in sorted(set(re.findall(r"`((?:chassis|docs)/[A-Za-z0-9_./-]+)`", text))):
            if not (repo / path).exists():
                missing.append(path)
        for path in sorted(set(re.findall(r"`(\.claude/[A-Za-z0-9_./-]+)`", text))):
            if path not in installed and "<" not in path:
                missing.append(path)
        self.assertEqual(missing, [])

    def test_no_container_breaking_script_paths(self):
        text = (CHASSIS / "CLAUDE.md.template").read_text()
        self.assertNotRegex(text, r"bash \$\{CHASSIS_HOME\}/chassis/scripts")
        self.assertNotRegex(text, r"python3 chassis/scripts/")


class PortedFilesAreGenericTest(unittest.TestCase):
    """The core skills were ported from one install. Nothing install-specific
    may ride along."""

    FILES = [src for src, _ in bcp.CORE_FILES]

    def test_no_install_specifics(self):
        banned = re.compile(r"Sean|Jax|jacketyjax|scrollinondubs|Chelsea|heykitwells|VCL|Vibecode|/Users/|\b\d{17,19}\b")
        for path in self.FILES:
            hits = banned.findall(path.read_text())
            self.assertEqual(hits, [], f"{path.name}: {hits}")

    def test_no_em_dashes(self):
        for path in self.FILES + [CHASSIS / "claude-settings.json.template",
                                  CHASSIS / "scripts" / "templates" / "chassis-hook.sh.template",
                                  CHASSIS / "memory" / "feedback_never_deceive.md.template"]:
            self.assertNotIn("\u2014", path.read_text(), path.name)


if __name__ == "__main__":
    unittest.main()
