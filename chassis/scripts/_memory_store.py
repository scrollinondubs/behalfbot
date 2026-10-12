"""Locate an install's auto-memory notes store.

Shared by memory-index-build.py, memory-integrity.py and gather-dream.sh so
the three never disagree about which directory they are looking at.

The notes store is Claude Code's auto-memory directory for the install:
`~/.claude/projects/<project dir>/memory/`, where <project dir> is the install
root's absolute path with every non-alphanumeric character turned into "-"
(`/home/ana/.behalfbot` -> `-home-ana--behalfbot`). That name is fixed by the
path the install was FIRST opened at. Inside the chassis container the
customer root is `/app/customer`, but the bind-mounted `~/.claude` still holds
the host-derived name, so deriving it from the container path finds nothing.
On a containerized install set CLAUDE_AUTO_MEMORY_DIR; the derivation is a
convenience for host-side runs, never a guarantee.

Resolution order, first existing directory wins:
  1. an explicit path (--store)
  2. $CLAUDE_AUTO_MEMORY_DIR
  3. ~/.claude/projects/<mangled $CUSTOMER_HOME>/memory
  4. $CUSTOMER_HOME/memory, only when it holds a MEMORY.md (older installs
     seeded their notes there)
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def mangle(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def candidates(explicit: str | None = None) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    if explicit:
        out.append(("--store", Path(explicit).expanduser()))
    env = os.environ.get("CLAUDE_AUTO_MEMORY_DIR")
    if env:
        out.append(("CLAUDE_AUTO_MEMORY_DIR", Path(env).expanduser()))
    home = os.environ.get("CUSTOMER_HOME") or os.path.expanduser("~/.behalfbot")
    out.append(("derived from CUSTOMER_HOME",
                Path.home() / ".claude" / "projects" / mangle(home) / "memory"))
    out.append(("CUSTOMER_HOME/memory", Path(home) / "memory"))
    return out


def resolve_store(explicit: str | None = None) -> tuple[Path | None, str, list[str]]:
    """Return (store or None, how it was found, every path searched).

    An explicit path is returned as-is even when missing, so the caller can
    report "not found at the path you gave" instead of silently falling back
    to some other store.
    """
    searched: list[str] = []
    for how, p in candidates(explicit):
        searched.append(str(p))
        if how == "--store":
            return (p if p.is_dir() else None), how, searched
        if how == "CUSTOMER_HOME/memory" and not (p / "MEMORY.md").is_file():
            continue
        if p.is_dir():
            return p, how, searched
    return None, "", searched
