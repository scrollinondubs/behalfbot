#!/usr/bin/env python3
"""Read-only pointer integrity report for an install's knowledge stores.

Checks the pointer-format rule in chassis/memory/MEMORY_MODEL.md across the
auto-memory notes store and the install repo's CLAUDE.md, skills/**/*.md,
docs/*.md and .claude/agents/*.md. Never writes, moves or deletes anything.
Exit 0 always: it is a report, and the dream gather reads it as one.

What it checks, all from text alone:
  - repo-relative paths that do not exist
  - absolute or ~/ paths INTO the install repo (the repo sits at different
    roots on the host and in the container, so they are wrong in one of the
    two); paths outside the repo are not repo pointers and are left alone
  - bare #N GitHub refs with no owner/repo
  - notes-store links ([Title](file.md), [[note]]) that do not resolve
  - orphan notes no MEMORY.md or index-*.md line reaches
  - second-brain refs (siyuan:, notion:, obsidian:) whose id is badly formed

Whether a well-formed second-brain id still resolves is a question only the
backend can answer, so it is the adapter's job, not this script's:
--probe-second-brain asks the configured adapter to read_doc() each ref
(chassis/second_brain, any backend). Off by default because it is a network
call per ref on SiYuan and Notion. The adapter reads its credentials from the
environment and does not load .env itself, so a hand run needs
`set -a; . "$CUSTOMER_HOME/.env"; set +a` first; heartbeat runs already have it.

Usage:
  python3 chassis/scripts/memory-integrity.py [--json] [--repo PATH] [--store PATH]
                                              [--probe-second-brain] [--all]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from _memory_store import resolve_store  # noqa: E402

PROBE_CAP = 500

ABS_PATH = re.compile(r"(?<![\w./:~-])((?:/|~/)[^\s`'\")\]>,;]+)")
# Install roots a pointer must not be written against. The same repo is
# /home/<user>/.behalfbot (or /Users/<user>/...) on the host and /app/customer
# in the container, so an absolute path into it is wrong in one of the two.
GENERIC_INSTALL_ROOT = re.compile(r"^(?:/(?:Users|home)/[^/]+/\.behalfbot|~/\.behalfbot|/app/customer)/[^/]")
REPO_TOKEN = re.compile(r"(?<![\w./~:@-])((?:\.?[A-Za-z0-9_-]+/)+[A-Za-z0-9_.*-]*[A-Za-z0-9_*])(?![\w/])")
BARE_ISSUE = re.compile(r"(?<![\w/#&])#(\d+)\b")
OWNER_REPO_ISSUE = re.compile(r"(?<![\w/.-])[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+\b")
SHORT_ISSUE = re.compile(r"(?<![\w/.-])([A-Za-z][A-Za-z0-9_.-]*[A-Za-z0-9])#(\d+)\b")
NOT_A_REPO = {"pr", "pull", "issue", "issues", "gh"}
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")
WIKI_LINK = re.compile(r"\[\[([A-Za-z0-9_.-]+)\]\]")
SB_REF = re.compile(r"(?<![\w/-])(siyuan|notion|obsidian):(?!//)([^\s`'\")\]>]+)")
PLACEHOLDER = re.compile(r"[*<>{}]|YYYY|MM-DD|XXX|\.\.\.")
FENCE = re.compile(r"^\s*(```|~~~)")

# Shape of a doc id per backend, as chassis/second_brain hands them out.
SB_SHAPES = {
    "siyuan": re.compile(r"^\d{14}-[a-z0-9]{7}$"),
    "notion": re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"),
}
SB_HINT = {
    "siyuan": "expected a block id like 20240101120000-abc1234, never an hpath or a title",
    "notion": "expected a 32-hex page id, dashes optional",
    "obsidian": "expected a vault-relative path, no leading / and no ..",
}


def sb_shape_ok(backend: str, ident: str) -> bool:
    ident = ident.rstrip(".,;:")
    if backend == "obsidian":
        return bool(ident) and not ident.startswith("/") and ".." not in ident.split("/")
    return bool(SB_SHAPES[backend].match(ident))


def into_install(tok: str, roots: set[str]) -> bool:
    if GENERIC_INSTALL_ROOT.match(tok):
        return True
    # The bare root is a statement about where the repo lives, not a pointer.
    return any(tok.startswith(r.rstrip("/") + "/") and len(tok) > len(r) + 1 for r in roots)


def strip_code_fences(text):
    """Yield (lineno, line) outside fenced code blocks."""
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield n, line


def collect_files(repo: Path, store: Path | None):
    files = []
    if store:
        files += [("notes", p) for p in sorted(store.glob("*.md"))]
    if (repo / "CLAUDE.md").exists():
        files.append(("repo", repo / "CLAUDE.md"))
    files += [("repo", p) for p in sorted((repo / "skills").rglob("*.md"))]
    files += [("repo", p) for p in sorted((repo / "docs").glob("*.md"))]
    files += [("repo", p) for p in sorted((repo / ".claude/agents").glob("*.md"))]
    return files


def chassis_roots(explicit: str | None) -> list[Path]:
    """Trees a `chassis/...` path may resolve against.

    The install repo may hold a vendored chassis/, an empty placeholder, or
    nothing. The chassis tree this script ships in is always a valid answer:
    in the chassis repo it is <repo>/chassis, on a baked container /app/chassis
    (which IS the chassis tree, no nested chassis/).
    """
    roots = []
    for c in (explicit, os.environ.get("CHASSIS_ROOT")):
        if c:
            roots.append(Path(c))
    roots += [SCRIPTS.parent, SCRIPTS.parent / "chassis"]
    return roots


def repo_path_exists(repo: Path, roots: list[Path], tok: str) -> bool:
    if (repo / tok).exists():
        return True
    if tok.startswith("chassis/"):
        rest = tok.split("/", 1)[1]
        return any((r / rest).exists() or (r / "chassis" / rest).exists() for r in roots)
    return False


def probe_second_brain(repo: Path, refs: dict, findings: dict, meta: dict) -> None:
    try:
        sys.path.insert(0, str(SCRIPTS.parent.parent))
        from chassis.second_brain import get_adapter

        sb = get_adapter(repo / "chassis.config.yaml")
    except Exception as e:  # noqa: BLE001 - report, never fail the caller
        meta["second_brain_probe"] = "skipped - adapter unavailable (%s: %s)" % (e.__class__.__name__, e)
        return
    checked = 0
    for (backend, ident), where in sorted(refs.items()):
        if backend != sb.backend:
            for f, n in where:
                findings["foreign_backend_ref"].append(
                    {"file": f, "line": n, "pointer": "%s:%s" % (backend, ident),
                     "detail": "install backend is %s" % sb.backend})
            continue
        if checked >= PROBE_CAP:
            meta["second_brain_probe_truncated"] = True
            break
        checked += 1
        try:
            sb.notes.read_doc(ident)
        except Exception as e:  # noqa: BLE001
            for f, n in where:
                findings["dead_second_brain_ref"].append(
                    {"file": f, "line": n, "pointer": "%s:%s" % (backend, ident),
                     "detail": e.__class__.__name__})
    meta["second_brain_probe"] = "ok (%s, %d refs read)" % (sb.backend, checked)


def run(args):
    repo = Path(args.repo).resolve()
    store, how, searched = resolve_store(args.store)
    roots = chassis_roots(args.chassis_root)
    try:
        top_level = set(os.listdir(repo))
    except OSError:
        top_level = set()

    # Both spellings: a symlinked root (/var -> /private/var on macOS) is
    # written the short way in prose and resolves the long way.
    install_roots = set()
    for r in {repo, Path(os.path.abspath(args.repo))}:
        install_roots.add(str(r))
        try:
            install_roots.add("~/" + str(r.relative_to(Path.home())))
        except ValueError:
            pass
    files = collect_files(repo, store)
    findings = defaultdict(list)
    meta = {"repo": str(repo),
            "store": ("%s (%s)" % (store, how)) if store else None,
            "store_searched": searched,
            "second_brain_probe": "off (pass --probe-second-brain)"}
    short_forms = defaultdict(int)
    sb_refs = defaultdict(list)
    linked_notes = set()

    for kind, path in files:
        where = ("notes:" + path.name) if kind == "notes" else str(path.relative_to(repo))
        try:
            text = path.read_text(errors="replace")
        except OSError as e:
            findings["unreadable"].append({"file": where, "detail": str(e)})
            continue
        is_index = kind == "notes" and (path.name == "MEMORY.md" or path.name.startswith("index-"))

        for n, line in strip_code_fences(text):
            for m in SB_REF.finditer(line):
                backend, raw = m.group(1), m.group(2)
                ident = raw.rstrip(".,;:")
                if not ident or PLACEHOLDER.search(raw):
                    continue
                if sb_shape_ok(backend, ident):
                    sb_refs[(backend, ident)].append((where, n))
                else:
                    findings["bad_second_brain_ref"].append(
                        {"file": where, "line": n, "pointer": "%s:%s" % (backend, ident),
                         "detail": SB_HINT[backend]})

            for m in ABS_PATH.finditer(line):
                tok = m.group(1).rstrip(".,;:")
                if PLACEHOLDER.search(tok) or not into_install(tok, install_roots):
                    continue
                findings["absolute_path"].append({"file": where, "line": n, "pointer": tok,
                                                  "detail": "write it repo-relative"})

            for m in REPO_TOKEN.finditer(line):
                tok = m.group(1).rstrip(".,;:")
                if tok.split("/", 1)[0] not in top_level or PLACEHOLDER.search(tok):
                    continue
                if not repo_path_exists(repo, roots, tok):
                    findings["missing_repo_path"].append({"file": where, "line": n, "pointer": tok})

            masked = OWNER_REPO_ISSUE.sub(lambda mm: " " * len(mm.group(0)), line)
            for m in SHORT_ISSUE.finditer(masked):
                if m.group(1).lower() in NOT_A_REPO:
                    findings["bare_issue_ref"].append({"file": where, "line": n, "pointer": m.group(0),
                                                       "context": line.strip()[:120]})
                else:
                    short_forms[m.group(1)] += 1
            for m in BARE_ISSUE.finditer(masked):
                findings["bare_issue_ref"].append({"file": where, "line": n, "pointer": "#" + m.group(1),
                                                   "context": line.strip()[:120]})

            if kind != "notes":
                continue
            for m in MD_LINK.finditer(line):
                target = m.group(1)
                if "/" in target or target.startswith("http"):
                    continue
                if is_index:
                    linked_notes.add(target)
                if not (store / target).exists():
                    findings["dead_note_link"].append({"file": where, "line": n, "pointer": target})
            for m in WIKI_LINK.finditer(line):
                target = m.group(1)
                fname = target if target.endswith(".md") else target + ".md"
                if is_index:
                    linked_notes.add(fname)
                if not (store / fname).exists():
                    item = {"file": where, "line": n, "pointer": "[[%s]]" % target}
                    if (store / fname.replace("-", "_")).exists():
                        item["detail"] = "kebab-case; the snake_case file exists"
                    findings["dead_note_link"].append(item)

    if store:
        mem = store / "MEMORY.md"
        mem_text = mem.read_text(errors="replace") if mem.exists() else ""
        for p in sorted(store.glob("*.md")):
            if p.name == "MEMORY.md" or p.name.startswith("index-"):
                continue
            if p.name not in linked_notes:
                findings["orphan_note"].append({"file": "notes:" + p.name})
        for idx in sorted(store.glob("index-*.md")):
            if idx.name not in mem_text:
                findings["orphan_note"].append({"file": "notes:" + idx.name,
                                                "detail": "index not linked from MEMORY.md"})

    if args.probe_second_brain and sb_refs:
        probe_second_brain(repo, sb_refs, findings, meta)

    meta["second_brain_refs"] = {b: sum(1 for (bb, _) in sb_refs if bb == b) for b in SB_HINT}
    meta["short_form_issue_refs"] = dict(short_forms)
    meta["files_scanned"] = len(files)
    return findings, meta


ORDER = [
    ("bad_second_brain_ref", "a1. Second-brain refs with a malformed id"),
    ("dead_second_brain_ref", "a2. Second-brain refs the adapter could not read (probe)"),
    ("foreign_backend_ref", "a3. Second-brain refs for a backend this install does not run (probe)"),
    ("missing_repo_path", "b1. Repo paths that do not exist"),
    ("absolute_path", "b2. Absolute paths into the install repo"),
    ("bare_issue_ref", "c. Bare #N GitHub refs (no owner/repo)"),
    ("dead_note_link", "d1. Notes-store links that do not resolve"),
    ("orphan_note", "d2. Orphan notes (not linked from MEMORY.md or index-*.md)"),
    ("unreadable", "Unreadable files"),
]


def render(findings, meta, limit):
    out = ["Memory integrity report (read-only)",
           "repo: %s" % meta["repo"],
           "notes store: %s" % (meta["store"] or "NOT FOUND - notes checks skipped (searched: %s)"
                                % ", ".join(meta["store_searched"])),
           "second-brain probe: %s" % meta["second_brain_probe"],
           "files scanned: %d" % meta["files_scanned"],
           "", "Counts"]
    for key, title in ORDER:
        out.append("  %-72s %d" % (title, len(findings.get(key, []))))
    refs = ", ".join("%s %d" % (b, c) for b, c in meta["second_brain_refs"].items())
    out.append("  %-72s %s" % ("Well-formed second-brain refs", refs))
    sf = meta["short_form_issue_refs"]
    out.append("  %-72s %s" % ("Short-form repo#N refs (acceptable, owner/repo#N preferred)",
                               ", ".join("%s %d" % kv for kv in sorted(sf.items())) or "0"))
    for key, title in ORDER:
        items = findings.get(key, [])
        if not items:
            continue
        out += ["", "== %s (%d) ==" % (title, len(items))]
        for it in items[:limit] if limit else items:
            loc = it["file"] + (":%d" % it["line"] if "line" in it else "")
            extra = it.get("pointer", "")
            if it.get("detail"):
                extra += ("  [%s]" % it["detail"]) if extra else it["detail"]
            out.append("  %s  %s" % (loc, extra))
        if limit and len(items) > limit:
            out.append("  ... %d more (use --all)" % (len(items) - limit))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--repo", default=os.environ.get("CUSTOMER_HOME") or os.getcwd(),
                    help="install repo root (default: $CUSTOMER_HOME, else cwd)")
    ap.add_argument("--store", help="notes store directory override")
    ap.add_argument("--chassis-root", help="chassis tree that chassis/ paths resolve against")
    ap.add_argument("--probe-second-brain", action="store_true",
                    help="read_doc() each second-brain ref through the configured adapter")
    ap.add_argument("--limit", type=int, default=40, help="findings shown per check (text mode)")
    ap.add_argument("--all", action="store_true", help="show every finding (text mode)")
    args = ap.parse_args(argv)
    try:
        findings, meta = run(args)
        if args.json:
            print(json.dumps({"meta": meta,
                              "counts": {k: len(findings.get(k, [])) for k, _ in ORDER},
                              "findings": {k: findings.get(k, []) for k, _ in ORDER}}, indent=2))
        else:
            print(render(findings, meta, 0 if args.all else args.limit))
    except Exception as e:  # noqa: BLE001 - reporting tool, never fail the caller
        msg = "%s: %s" % (e.__class__.__name__, e)
        if args.json:
            print(json.dumps({"error": msg}))
        print("memory-integrity: aborted - " + msg, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
