#!/usr/bin/env python3
"""Scan the host disk and write a treemap-ready JSON for /devops/disk.

Runs `du -k -d 6` over the roots that hold nearly all of the used space, then
folds anything under 150 MB into an "(other files)" leaf on its parent. The
dashboard renders the result with d3.treemap, which sums leaf values, so only
leaves carry a `value`; directories carry `children` alone.

Roots: $HOME plus the usual system dirs for the OS (macOS: /opt,
/Applications, /Library, /private/var; Linux: /opt, /srv, /usr, /var).
DISK_MAP_ROOTS overrides them with a colon-separated list. Capacity comes
from `df -k $HOME`.

Output (atomic write): $CUSTOMER_HOME/state/disk-map.json
    {scanned_at, used_kb, total_kb, measured_kb, roots, tree}

`used_kb - measured_kb` is the space outside the scanned roots (snapshots,
other users, system volumes). The page shows it so a gap is visible, not lost.

Usage:
    python3 chassis/scripts/disk-map-scan.py            # scan + write
    python3 chassis/scripts/disk-map-scan.py --out FILE
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import socket
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _paths import customer_home  # noqa: E402

DEPTH = 6
FOLD_KB = 150 * 1024
OS_ROOTS = {
    "Darwin": ["/opt", "/Applications", "/Library", "/private/var"],
    "Linux": ["/opt", "/srv", "/usr", "/var"],
}


def default_roots() -> list[str]:
    override = os.environ.get("DISK_MAP_ROOTS", "").strip()
    if override:
        return [r for r in override.split(":") if r]
    return [str(pathlib.Path.home())] + OS_ROOTS.get(platform.system(), [])


def du_sizes(roots: list[str]) -> dict[str, int]:
    """Path -> KB for every directory down to DEPTH under each existing root.

    du exits nonzero on permission-denied under system dirs. That is expected
    for a non-root scan: stderr is discarded and whatever du could read is kept.
    """
    existing = [r for r in roots if os.path.isdir(r)]
    if not existing:
        return {}
    proc = subprocess.run(
        ["du", "-k", "-d", str(DEPTH), *existing],
        capture_output=True, text=True, errors="replace",
    )
    sizes: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        kb, _, path = line.partition("\t")
        if path and kb.isdigit():
            sizes[path] = int(kb)
    return sizes


def build_tree(sizes: dict[str, int], roots: list[str], name: str) -> dict:
    children: dict[str, list[str]] = {}
    for path in sizes:
        parent = os.path.dirname(path)
        if parent in sizes and parent != path:
            children.setdefault(parent, []).append(path)

    def node(path: str) -> dict | None:
        kb = sizes[path]
        label = os.path.basename(path) or path
        kept = [n for n in (node(c) for c in children.get(path, [])
                            if sizes[c] >= FOLD_KB) if n]
        if not kept:
            return {"name": label, "path": path, "value": kb}
        kept.sort(key=_total, reverse=True)
        rest = kb - sum(_total(k) for k in kept)
        if rest >= FOLD_KB:
            kept.append({"name": "(other files)", "path": path, "value": rest})
        return {"name": label, "path": path, "children": kept}

    top = [n for n in (node(r) for r in roots if r in sizes) if n]
    top.sort(key=_total, reverse=True)
    return {"name": name, "children": top}


def _total(n: dict) -> int:
    if "value" in n:
        return n["value"]
    return sum(_total(c) for c in n["children"])


def volume_kb(path: str) -> tuple[int, int]:
    """(used_kb, total_kb) for the volume holding `path`, from df."""
    out = subprocess.run(["df", "-k", path], capture_output=True, text=True).stdout
    fields = out.splitlines()[-1].split()
    return int(fields[2]), int(fields[1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, default=customer_home() / "state" / "disk-map.json")
    args = ap.parse_args()

    roots = default_roots()
    sizes = du_sizes(roots)
    tree = build_tree(sizes, roots, f"{socket.gethostname().split('.')[0]} disk")
    used_kb, total_kb = volume_kb(str(pathlib.Path.home()))
    result = {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "used_kb": used_kb,
        "total_kb": total_kb,
        "measured_kb": _total(tree),
        "roots": roots,
        "tree": tree,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result))
    os.replace(tmp, args.out)
    print(f"wrote {args.out}: measured {result['measured_kb'] // 1048576} GB "
          f"of {used_kb // 1048576} GB used")


if __name__ == "__main__":
    main()
