#!/usr/bin/env python3
"""plugin-registry.py - read the fetched plugin registry for installs and sessions.

Two jobs, both reading $CUSTOMER_HOME/vendored-plugins (the tree
fetch-plugins.sh installs) and its registry.json:

  list         The plugins an install interview may offer. DORMANT entries are
               left out: a plugin whose supplier is gone stays in the registry
               so old pins still resolve, but offering it to a new installer
               sets up a feature that cannot work. --all shows them, marked.

  link-skills  Expose fetched plugin skills to Claude Code. A plugin skill lands
               at vendored-plugins/<p>/skills/<s>/SKILL.md, where no session
               looks. This links .claude/skills/<s> -> ../../vendored-plugins/
               <p>/skills/<s> (relative, so the link resolves on the host and in
               the container alike). Idempotent; run after every fetch.
               Skipped: DORMANT plugins, plugins switched off in
               chassis.config.yaml modules, and any name where .claude/skills
               already holds something that is not our link. Our links that no
               longer resolve, or now point at a skipped plugin, are removed.

A plugin is DORMANT when its registry entry has "status": "dormant" or its
description starts with "DORMANT".

Usage:
  plugin-registry.py list [--all] [--json]
  plugin-registry.py link-skills [--dry-run]

Env: CUSTOMER_HOME (default /app/customer when it exists, else ~/.behalfbot),
PLUGINS_FETCH_ROOT (default $CUSTOMER_HOME/vendored-plugins),
CHASSIS_CONFIG (default $CUSTOMER_HOME/chassis.config.yaml).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

VENDORED_PREFIX = "../../vendored-plugins/"


def customer_home() -> Path:
    env = os.environ.get("CUSTOMER_HOME")
    if env:
        return Path(env)
    if Path("/app/customer").is_dir():
        return Path("/app/customer")
    return Path.home() / ".behalfbot"


def fetch_root(home: Path) -> Path:
    return Path(os.environ.get("PLUGINS_FETCH_ROOT") or home / "vendored-plugins")


def is_dormant(entry: dict) -> bool:
    if str(entry.get("status", "")).strip().lower() == "dormant":
        return True
    return str(entry.get("description", "")).lstrip().upper().startswith("DORMANT")


def load_registry(root: Path) -> list[dict]:
    path = root / "registry.json"
    if not path.is_file():
        return []
    return list(json.loads(path.read_text()).get("plugins", []))


def load_modules(home: Path) -> dict:
    path = Path(os.environ.get("CHASSIS_CONFIG") or home / "chassis.config.yaml")
    if not path.is_file():
        return {}
    text = path.read_text()
    try:
        import yaml  # type: ignore[import-untyped]

        config = yaml.safe_load(text) or {}
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from chassis.second_brain.factory import _parse_yaml_minimal

        config = _parse_yaml_minimal(text)
    modules = config.get("modules") if isinstance(config, dict) else None
    return modules if isinstance(modules, dict) else {}


def switched_off(name: str, modules: dict) -> bool:
    """True only when chassis.config.yaml explicitly disables the plugin.

    Plugins are fetched by pin, not by module flag, so most have no modules
    entry at all. Absent means on.
    """
    for key in (name, name.replace("-", "_")):
        if key not in modules:
            continue
        value = modules[key]
        if isinstance(value, dict):
            return value.get("enabled", True) is False
        return value is False
    return False


def cmd_list(args: argparse.Namespace) -> int:
    root = fetch_root(customer_home())
    registry = load_registry(root)
    if not registry:
        print(f"no registry.json under {root} - run fetch-plugins.sh first", file=sys.stderr)
        return 1
    rows = []
    for entry in registry:
        dormant = is_dormant(entry)
        if dormant and not args.all:
            continue
        rows.append({
            "name": entry["name"],
            "description": entry.get("description", ""),
            "min_chassis_version": entry.get("min_chassis_version"),
            "dormant": dormant,
        })
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        mark = " [DORMANT - do not offer]" if row["dormant"] else ""
        print(f"{row['name']}{mark}: {row['description']}")
    return 0


def _ours(link: Path) -> bool:
    return link.is_symlink() and os.readlink(link).startswith(VENDORED_PREFIX)


def cmd_link_skills(args: argparse.Namespace) -> int:
    home = customer_home()
    root = fetch_root(home)
    skills_dir = home / ".claude" / "skills"
    registry = load_registry(root)
    if not registry:
        print(f"[link-skills] no registry.json under {root} - nothing to link", file=sys.stderr)
        return 0
    modules = load_modules(home)

    wanted: dict[str, str] = {}
    for entry in registry:
        name = entry["name"]
        if is_dormant(entry) or switched_off(name, modules):
            continue
        plugin_dir = root / entry.get("path", name)
        for skill_md in sorted(plugin_dir.glob("skills/*/SKILL.md")):
            skill = skill_md.parent.name
            rel = os.path.relpath(skill_md.parent, root)
            wanted.setdefault(skill, VENDORED_PREFIX + rel)

    act = not args.dry_run
    if act:
        skills_dir.mkdir(parents=True, exist_ok=True)

    linked = kept = skipped = removed = 0
    for skill, target in sorted(wanted.items()):
        link = skills_dir / skill
        if link.is_symlink() or link.exists():
            if _ours(link) and os.readlink(link) == target:
                kept += 1
                continue
            if not _ours(link):
                print(f"[link-skills] skip {skill}: .claude/skills/{skill} exists and is not a vendored-plugin link")
                skipped += 1
                continue
            if act:
                link.unlink()
        if act:
            link.symlink_to(target)
        print(f"[link-skills] link {skill} -> {target}")
        linked += 1

    if skills_dir.is_dir():
        for link in sorted(skills_dir.iterdir()):
            if not _ours(link) or link.name in wanted:
                continue
            if act:
                link.unlink()
            print(f"[link-skills] remove {link.name} (plugin gone, dormant or switched off)")
            removed += 1

    print(f"[link-skills] {linked} linked, {kept} unchanged, {removed} removed, {skipped} skipped")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="plugins an install interview may offer")
    p_list.add_argument("--all", action="store_true", help="include DORMANT plugins, marked")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)
    p_link = sub.add_parser("link-skills", help="link fetched plugin skills into .claude/skills")
    p_link.add_argument("--dry-run", action="store_true")
    p_link.set_defaults(func=cmd_link_skills)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
