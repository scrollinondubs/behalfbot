#!/usr/bin/env python3
"""bootstrap-claude-project.py - wire an install's Claude Code project.

The CLAUDE.md template promises skills, a sub-agent, hooks and a notes store
that a fresh install never got: the chassis shipped the files but nothing put
them where Claude Code looks (scrollinondubs/behalfbot#264). This does, and is
safe to re-run: it creates what is missing and never overwrites what the
installer has written.

Into $CUSTOMER_HOME:
  .claude/hooks/chassis-hook.sh   the shim every hook command goes through
                                  (chassis-owned, rewritten on every run)
  .claude/settings.json           rendered from chassis/claude-settings.json.template
                                  when absent; when present, only the
                                  SessionStart map hook and the html-plan plugin
                                  are added
  .claude/skills/software-engineering/SKILL.md   copied when absent
  .claude/skills/build-plans/SKILL.md            copied when absent
  .claude/agents/software-engineering.md         copied when absent

Into the auto-memory notes store (chassis/scripts/_memory_store.py explains the
path), only when it has no MEMORY.md yet:
  MEMORY.md, feedback_never_deceive.md, and the domain indexes
  (memory-index-build.py --init)

Then, unless BOOTSTRAP_SKIP_PLUGIN_INSTALL=1 or --skip-plugin-install, installs
the html-plan plugin at project scope when the claude CLI is on PATH.
Non-fatal: the settings entry already names it, and the build-plans skill says
how to install it by hand.

Usage:
  bootstrap-claude-project.py [--customer-home DIR] [--config FILE]
                              [--memory-dir DIR] [--dry-run] [--skip-plugin-install]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CHASSIS_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = CHASSIS_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _memory_store import mangle, resolve_store  # noqa: E402

HTML_PLAN_PLUGIN = "html-plan@claude-community"
MAP_HOOK_MARKER = "load-second-brain-map"
CORE_FILES = [
    (CHASSIS_DIR / "skills" / "software-engineering.md", Path(".claude/skills/software-engineering/SKILL.md")),
    (CHASSIS_DIR / "skills" / "build-plans.md", Path(".claude/skills/build-plans/SKILL.md")),
    (CHASSIS_DIR / "agents" / "software-engineering.md", Path(".claude/agents/software-engineering.md")),
]
PLACEHOLDER = re.compile(r"^<[^>]*>$|^\$\{[^}]*\}$")


class Run:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

    def say(self, msg: str) -> None:
        print(f"[claude-project] {msg}")

    def done(self, msg: str) -> None:
        """Report a write; in a dry run write() already said what it would do."""
        if not self.dry_run:
            self.say(msg)

    def write(self, path: Path, text: str, mode: int | None = None) -> None:
        if self.dry_run:
            self.say(f"[dry-run] would write {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if mode is not None:
            path.chmod(mode)


def load_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    text = path.read_text()
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(text) or {}
    except ImportError:
        sys.path.insert(0, str(CHASSIS_DIR.parent))
        from chassis.second_brain.factory import _parse_yaml_minimal

        data = _parse_yaml_minimal(text)
    return data if isinstance(data, dict) else {}


def _real(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return "" if not value or PLACEHOLDER.match(value) else value


def identity_names(config: dict) -> tuple[str, str]:
    ident = config.get("identity") or {}
    assistant = (ident.get("assistant") or {}) if isinstance(ident, dict) else {}
    principal = (ident.get("principal") or {}) if isinstance(ident, dict) else {}
    instance = (
        _real(assistant.get("name"))
        or _real(os.environ.get("INSTANCE_NAME"))
        or "Assistant"
    )
    installer = (
        _real(principal.get("full_name"))
        or _real(ident.get("installer_name") if isinstance(ident, dict) else None)
        or _real(os.environ.get("INSTALLER_NAME"))
        or "the principal"
    )
    return instance, installer


def render(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def install_hook_shim(run: Run, home: Path) -> None:
    template = (SCRIPTS / "templates" / "chassis-hook.sh.template").read_text()
    text = template.replace("@HOST_CHASSIS_DIR@", str(CHASSIS_DIR))
    dest = home / ".claude" / "hooks" / "chassis-hook.sh"
    if dest.is_file() and dest.read_text() == text:
        run.say(f"hook shim current: {dest}")
        return
    run.write(dest, text, 0o755)
    run.done(f"hook shim written: {dest}")


def _commands(settings: dict, event: str) -> list[str]:
    out = []
    for group in (settings.get("hooks") or {}).get(event) or []:
        for hook in group.get("hooks") or []:
            out.append(str(hook.get("command", "")))
    return out


def merge_settings(existing: dict, template: dict) -> list[str]:
    """Add the map hook and the html-plan plugin to an existing settings dict.

    Returns what changed. Never removes or rewrites an entry the installer
    already has, including an explicit `false` for the plugin.
    """
    changes = []
    if not any(MAP_HOOK_MARKER in c for c in _commands(existing, "SessionStart")):
        hooks = existing.setdefault("hooks", {})
        hooks.setdefault("SessionStart", []).extend(template["hooks"]["SessionStart"])
        changes.append("added SessionStart second-brain map hook")
    markets = existing.setdefault("extraKnownMarketplaces", {})
    if "claude-community" not in markets:
        markets["claude-community"] = template["extraKnownMarketplaces"]["claude-community"]
        changes.append("added claude-community marketplace")
    plugins = existing.setdefault("enabledPlugins", {})
    if HTML_PLAN_PLUGIN not in plugins:
        plugins[HTML_PLAN_PLUGIN] = True
        changes.append(f"enabled {HTML_PLAN_PLUGIN}")
    return changes


def install_settings(run: Run, home: Path) -> None:
    template_text = (CHASSIS_DIR / "claude-settings.json.template").read_text()
    template = json.loads(template_text)
    dest = home / ".claude" / "settings.json"
    if not dest.exists():
        run.write(dest, template_text)
        run.done(f"settings rendered: {dest}")
        return
    try:
        existing = json.loads(dest.read_text())
    except json.JSONDecodeError as exc:
        run.say(f"WARN: {dest} is not valid JSON ({exc}) - left untouched; add the hooks by hand")
        return
    changes = merge_settings(existing, template)
    if not any("guardrails" in c for c in _commands(existing, "PreToolUse")):
        run.say(f"WARN: {dest} registers no guardrails hook. The chassis safety layer is off "
                "in this project; see docs/security.md 'Registering the hooks'.")
    if not changes:
        run.say(f"settings current: {dest}")
        return
    run.write(dest, json.dumps(existing, indent=2) + "\n")
    for change in changes:
        run.say(f"settings: {change}{' (dry run)' if run.dry_run else ''}")


def install_core_files(run: Run, home: Path) -> None:
    for src, rel in CORE_FILES:
        dest = home / rel
        if dest.exists():
            note = "" if dest.read_text() == src.read_text() else " (differs from the chassis copy; kept)"
            run.say(f"present: {rel}{note}")
            continue
        run.write(dest, src.read_text())
        run.done(f"installed: {rel}")


def memory_dir(home: Path, explicit: str | None) -> tuple[Path, bool]:
    """(store to seed, whether it already holds a notes store).

    Goes through _memory_store.resolve_store first, the same resolution every
    memory script uses. Deriving a path here instead would seed a second,
    near-empty store beside an existing one (a legacy $CUSTOMER_HOME/memory, or
    one named by CLAUDE_AUTO_MEMORY_DIR), and resolve_store would then prefer
    the new empty one for every script after.
    """
    if explicit:
        store = Path(explicit).expanduser()
        return store, (store / "MEMORY.md").is_file()
    os.environ["CUSTOMER_HOME"] = str(home)
    found, _, _ = resolve_store(None)
    if found is not None and (found / "MEMORY.md").is_file():
        return found, True
    env = os.environ.get("CLAUDE_AUTO_MEMORY_DIR")
    if env:
        return Path(env).expanduser(), False
    return Path.home() / ".claude" / "projects" / mangle(str(home)) / "memory", False


def seed_memory(run: Run, store: Path, exists: bool, instance: str, installer: str) -> None:
    if exists:
        run.say(f"notes store already has MEMORY.md: {store}")
        return
    run.say(f"notes store: {store} (Claude Code reads it only for sessions opened at "
            "this install path; set CLAUDE_AUTO_MEMORY_DIR when sessions run elsewhere)")
    values = {"INSTANCE_NAME": instance, "INSTALLER_NAME": installer}
    memory_src = CHASSIS_DIR / "memory"
    run.write(store / "MEMORY.md", render((memory_src / "MEMORY.md.template").read_text(), values))
    note = store / "feedback_never_deceive.md"
    if not note.exists():
        run.write(note, render((memory_src / "feedback_never_deceive.md.template").read_text(), values))
    run.done(f"notes store seeded: {store}")
    if run.dry_run:
        return
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "memory-index-build.py"), "--store", str(store), "--init"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        run.say(f"WARN: memory-index-build.py --init exited {result.returncode}: {result.stdout}{result.stderr}")
    else:
        run.say("domain indexes created (memory-index-build.py --init)")


def install_html_plan_plugin(run: Run, home: Path) -> None:
    claude = shutil.which("claude")
    if not claude:
        run.say("claude CLI not on PATH - html-plan plugin left to the settings entry "
                "(install by hand: see .claude/skills/build-plans/SKILL.md)")
        return
    if run.dry_run:
        run.say("[dry-run] would install the html-plan plugin at project scope")
        return
    # Two steps, both at project scope. `plugin install --marketplace` is one
    # step on current CLIs but unknown to older ones, and an image can lag;
    # project scope keeps the marketplace out of the user settings, which on
    # a container install is the host's bind-mounted ~/.claude.
    for step in (
        ["plugin", "marketplace", "add", "anthropics/claude-plugins-community", "--scope", "project"],
        ["plugin", "install", HTML_PLAN_PLUGIN, "--scope", "project"],
    ):
        try:
            result = subprocess.run([claude, *step], cwd=home, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            run.say(f"WARN: `claude {' '.join(step[:2])}` did not finish ({exc}); not fatal")
            return
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
            run.say(f"WARN: `claude {' '.join(step[:3])}` exited {result.returncode}: {tail[0]} - not fatal")
            return
    run.say(f"{HTML_PLAN_PLUGIN} installed (project scope)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer-home", default=os.environ.get("CUSTOMER_HOME"))
    ap.add_argument("--config", help="chassis.config.yaml (default: <customer home>/chassis.config.yaml)")
    ap.add_argument("--memory-dir", help="notes store (default: $CLAUDE_AUTO_MEMORY_DIR, then derived)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-plugin-install", action="store_true")
    args = ap.parse_args(argv)

    if not args.customer_home:
        print("[claude-project] CUSTOMER_HOME is not set and --customer-home was not given", file=sys.stderr)
        return 2
    home = Path(os.path.abspath(os.path.expanduser(args.customer_home)))
    if not home.is_dir():
        print(f"[claude-project] {home} does not exist", file=sys.stderr)
        return 2
    run = Run(args.dry_run)
    config = load_config(Path(args.config) if args.config else home / "chassis.config.yaml")
    instance, installer = identity_names(config)

    install_hook_shim(run, home)
    install_settings(run, home)
    install_core_files(run, home)
    store, exists = memory_dir(home, args.memory_dir)
    seed_memory(run, store, exists, instance, installer)
    if not (args.skip_plugin_install or os.environ.get("BOOTSTRAP_SKIP_PLUGIN_INSTALL") == "1"):
        install_html_plan_plugin(run, home)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
