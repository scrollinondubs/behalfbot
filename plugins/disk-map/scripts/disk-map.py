#!/usr/bin/env python3
"""disk-map.py - scan the host disk and render a zoomable treemap page.

Runs `du -k -d 6` over the roots that usually hold nearly all used space,
folds anything under 150 MB into an "(other files)" leaf on its parent, and
writes two files:

    $CUSTOMER_HOME/state/disk-map.json      tree + capacity numbers
    $CUSTOMER_HOME/briefings/disk-map.html  self-contained treemap page

The HTML lands in briefings/ so the chassis briefing-server already serves it
(see docs/briefing-pipeline.md): https://<machine>.<tailnet>.ts.net/briefings/disk-map.html

d3.treemap sums leaf values, so only leaves carry a `value`; directories
carry `children` alone. `used_kb - measured_kb` is the space outside the
scanned roots (APFS snapshots, other users, system volumes); the page shows
it so a gap is a number, not a mystery.

Run on the HOST, not inside the container: du in the container only sees
its bind mounts. A full scan takes one to three minutes.

Usage:
    disk-map.py                         # default roots for this OS
    disk-map.py --roots /srv /home      # explicit roots
    DISK_MAP_ROOTS=/srv:/home disk-map.py
    disk-map.py --json-out F --html-out F
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import subprocess
from datetime import datetime, timezone

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = PLUGIN_DIR / "templates" / "disk-map.html"

DEPTH = 6
FOLD_KB = 150 * 1024


def customer_home() -> pathlib.Path:
    """Same resolution order as chassis/scripts/_env.sh."""
    for var in ("CUSTOMER_HOME", "CHASSIS_HOME"):
        if os.environ.get(var):
            return pathlib.Path(os.environ[var])
    for candidate in (pathlib.Path.home() / ".behalfbot", pathlib.Path("/app/customer")):
        if candidate.is_dir():
            return candidate
    return pathlib.Path.home() / ".behalfbot"


def default_roots() -> list[str]:
    if os.environ.get("DISK_MAP_ROOTS"):
        return os.environ["DISK_MAP_ROOTS"].split(":")
    home = str(pathlib.Path.home())
    if platform.system() == "Darwin":
        return [home, "/opt", "/Applications", "/Library", "/private/var"]
    return [home, "/opt", "/var", "/usr", "/srv"]


def du_sizes(roots: list[str]) -> dict[str, int]:
    """Path -> KB for every directory down to DEPTH under each existing root.

    du exits nonzero on permission-denied under system dirs. That is expected
    for a non-root scan: stderr is discarded and whatever du could read is kept.
    """
    existing = [r for r in roots if os.path.isdir(r)]
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


def build_tree(sizes: dict[str, int], roots: list[str]) -> dict:
    children: dict[str, list[str]] = {}
    for path in sizes:
        parent = os.path.dirname(path)
        if parent in sizes and parent != path:
            children.setdefault(parent, []).append(path)

    def node(path: str) -> dict | None:
        kb = sizes[path]
        name = os.path.basename(path) or path
        kept = [n for n in (node(c) for c in children.get(path, [])
                            if sizes[c] >= FOLD_KB) if n]
        if not kept:
            return {"name": name, "path": path, "value": kb}
        kept.sort(key=_total, reverse=True)
        rest = kb - sum(_total(k) for k in kept)
        if rest >= FOLD_KB:
            kept.append({"name": "(other files)", "path": path, "value": rest})
        return {"name": name, "path": path, "children": kept}

    top = [n for n in (node(r) for r in roots if r in sizes) if n]
    top.sort(key=_total, reverse=True)
    return {"name": platform.node().split(".")[0] or "disk", "children": top}


def _total(n: dict) -> int:
    if "value" in n:
        return n["value"]
    return sum(_total(c) for c in n["children"])


def volume_kb(path: str) -> tuple[int, int]:
    """(used_kb, total_kb) for the filesystem holding `path`, from df."""
    out = subprocess.run(["df", "-k", path], capture_output=True, text=True).stdout
    fields = out.splitlines()[-1].split()
    return int(fields[2]), int(fields[1])


def _atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def main() -> None:
    home = customer_home()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roots", nargs="+", default=default_roots())
    ap.add_argument("--json-out", type=pathlib.Path, default=home / "state" / "disk-map.json")
    ap.add_argument("--html-out", type=pathlib.Path, default=home / "briefings" / "disk-map.html")
    args = ap.parse_args()

    sizes = du_sizes(args.roots)
    tree = build_tree(sizes, args.roots)
    # On macOS, $HOME's df reports the APFS data volume, which is what fills up.
    used_kb, total_kb = volume_kb(str(pathlib.Path.home()))
    result = {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "used_kb": used_kb,
        "total_kb": total_kb,
        "measured_kb": _total(tree),
        "roots": args.roots,
        "tree": tree,
    }

    _atomic_write(args.json_out, json.dumps(result))
    # "</" would let a directory name close the <script> tag early.
    payload = json.dumps(result).replace("</", "<\\/")
    _atomic_write(args.html_out, TEMPLATE.read_text().replace("/*DATA*/", f"window.DISK={payload};"))

    gb = lambda kb: kb / 1048576
    print(json.dumps({
        "html": str(args.html_out),
        "json": str(args.json_out),
        "used_gb": round(gb(used_kb), 1),
        "free_gb": round(gb(total_kb - used_kb), 1),
        "unscanned_gb": round(gb(used_kb - result["measured_kb"]), 1),
        "top": [{"path": c["path"], "gb": round(gb(_total(c)), 1)}
                for c in _largest(tree, 5)],
    }))


def _largest(tree: dict, n: int) -> list[dict]:
    """The n biggest directories one level below the roots - the useful hogs."""
    level = [g for r in tree["children"] for g in r.get("children", [r])
             if g["name"] != "(other files)"]
    return sorted(level, key=_total, reverse=True)[:n]


if __name__ == "__main__":
    main()
