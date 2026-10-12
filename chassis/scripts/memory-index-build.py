#!/usr/bin/env python3
"""Generate the notes store's index-*.md domain indexes from note frontmatter.

Each note lands in index-<domain>.md for every value of its `domain:` field.
A note with no `domain:` keeps the indexes that link it today (inferred), and
the report counts those so a later pass can write `domain:` into frontmatter.

Default mode is --check: nothing is written. --write rewrites only top-level
index-*.md files whose content changed. --init creates an empty
index-<domain>.md for every index MEMORY.md links that does not exist yet (a
fresh install). MEMORY.md is never written, and subfolders (a subagent's own
store with its own index) are never read or written.

The store is found the way chassis/scripts/_memory_store.py documents:
--store, then $CLAUDE_AUTO_MEMORY_DIR, then the path derived from
$CUSTOMER_HOME. Model and frontmatter fields: chassis/memory/MEMORY_MODEL.md.

Usage:
  python3 chassis/scripts/memory-index-build.py [--store PATH] [--check | --write | --init] [--verbose]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _memory_store import resolve_store  # noqa: E402

POINTERS_HEADING = "## Pointers (homes outside the notes store)"

LIST_ITEM = re.compile(r"^\s*[-*] ")
MD_LINK = re.compile(r"\]\(([^)\s]+?\.md)\)")
WIKI_LINK = re.compile(r"\[\[([^\]|#]+)")
INDEX_FILE = re.compile(r"^index-[A-Za-z0-9_-]+\.md$")


@dataclass
class Note:
    file: str
    name: str = ""
    description: str = ""
    type: str = ""
    domains: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    inferred: bool = False


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].replace('\\"', '"')
    return v


def _as_list(v: str) -> list[str]:
    v = v.strip()
    if not v:
        return []
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [x for x in (_unquote(p) for p in v.split(",")) if x]


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter, body). Frontmatter is None when absent."""
    if not text.startswith("---"):
        return None, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def parse_frontmatter(fm: str) -> dict[str, str]:
    """Flat key/value reader for the two shapes in the store.

    Top-level `key: value`, plus one nested level under `metadata:` exposed as
    `metadata.key`. Pending YAML list items (`- x`) under a key are joined into
    a flow list so `supersedes:` can be written either way.
    """
    out: dict[str, str] = {}
    parent = None
    last_key = None
    for raw in fm.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if line.startswith("- ") and last_key:
            prev = out.get(last_key, "")
            item = line[2:].strip()
            out[last_key] = f"{prev}, {item}" if prev else item
            continue
        m = re.match(r"^([\w.-]+):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if indent == 0:
            parent = key if val == "" else None
            out[key] = val
            last_key = key
        elif parent:
            full = f"{parent}.{key}"
            out[full] = val
            last_key = full
    return out


def load_note(path: str) -> Note:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    fm, _ = split_frontmatter(text)
    n = Note(file=os.path.basename(path))
    if fm is None:
        return n
    d = parse_frontmatter(fm)

    def get(k: str) -> str:
        return d.get(k) or d.get(f"metadata.{k}") or ""

    n.name = _unquote(get("name"))
    n.description = _unquote(get("description"))
    n.type = _unquote(get("type"))
    n.domains = _as_list(get("domain"))
    n.supersedes = [_norm_target(x) for x in _as_list(get("supersedes"))]
    return n


def _norm_target(t: str) -> str:
    t = t.strip()
    if t.startswith("[[") and t.endswith("]]"):
        t = t[2:-2]
    return t if t.endswith(".md") else t + ".md"


def link_targets(line: str) -> list[str]:
    return MD_LINK.findall(line) + [w.strip() + ".md" for w in WIKI_LINK.findall(line)]


@dataclass
class Index:
    domain: str
    file: str
    text: str
    header: str
    pointers: list[str]
    pointers_heading: str
    linked: list[str]
    dead_pointer_links: list[tuple[str, str]]


def parse_index(path: str, notes: set[str]) -> Index:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    fname = os.path.basename(path)
    domain = fname[len("index-"):-len(".md")]
    lines = text.split("\n")

    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break

    first_item = len(lines)
    for i in range(body_start, len(lines)):
        if LIST_ITEM.match(lines[i]):
            first_item = i
            break
    header = "\n".join(lines[:first_item]).rstrip("\n")

    pointers: list[str] = []
    linked: list[str] = []
    dead: list[tuple[str, str]] = []
    heading = POINTERS_HEADING
    for line in lines[first_item:]:
        if line.startswith("## Pointers"):
            heading = line.rstrip()
        if not LIST_ITEM.match(line):
            continue
        targets = link_targets(line)
        hits = [t for t in targets if t in notes]
        if hits:
            for t in hits:
                if t not in linked:
                    linked.append(t)
        else:
            pointers.append(line.rstrip())
            for t in targets:
                if "/" not in t:
                    dead.append((t, line.strip()))
    return Index(domain, fname, text, header, pointers, heading, linked, dead)


def scrub(s: str) -> str:
    s = re.sub(r"\s*\u2014\s*", " - ", s)
    return re.sub(r"\s+", " ", s).strip()


def render_line(n: Note) -> str:
    title = scrub(n.name) or n.file[:-3]
    title = title.replace("[", "(").replace("]", ")")
    desc = scrub(n.description)
    return f"- [{title}]({n.file}) - {desc}" if desc else f"- [{title}]({n.file})"


def render_index(ix: Index, members: list[Note]) -> str:
    out = [ix.header, ""] if ix.header else []
    out += [render_line(n) for n in sorted(members, key=lambda n: n.file)]
    if ix.pointers:
        out += ["", ix.pointers_heading, ""] + ix.pointers
    return "\n".join(out).rstrip("\n") + "\n"


def build(store: str) -> dict:
    entries = sorted(os.listdir(store))
    note_files = [
        f for f in entries
        if f.endswith(".md") and f != "MEMORY.md" and not f.startswith("index-")
        and os.path.isfile(os.path.join(store, f))
    ]
    index_files = [
        f for f in entries
        if f.startswith("index-") and f.endswith(".md") and os.path.isfile(os.path.join(store, f))
    ]
    note_set = set(note_files)
    notes = {f: load_note(os.path.join(store, f)) for f in note_files}
    indexes = {ix.domain: ix for ix in (parse_index(os.path.join(store, f), note_set) for f in index_files)}

    current: dict[str, list[str]] = defaultdict(list)
    for ix in indexes.values():
        for f in ix.linked:
            current[f].append(ix.domain)

    for n in notes.values():
        if not n.domains and current.get(n.file):
            n.domains = list(current[n.file])
            n.inferred = True

    memory_links: set[str] = set()
    missing_indexes: list[str] = []
    mem_path = os.path.join(store, "MEMORY.md")
    if os.path.isfile(mem_path):
        with open(mem_path, encoding="utf-8") as f:
            targets = link_targets(f.read())
        memory_links = {t for t in targets if t in note_set}
        missing_indexes = sorted({
            t for t in targets
            if INDEX_FILE.match(t) and t not in index_files
        })

    members: dict[str, list[Note]] = defaultdict(list)
    unknown_domain: list[tuple[str, str]] = []
    for n in notes.values():
        for d in n.domains:
            if d in indexes:
                members[d].append(n)
            else:
                unknown_domain.append((n.file, d))

    listed = {n.file for ms in members.values() for n in ms}
    unlisted = sorted(set(note_files) - listed)

    titles = Counter(scrub(n.name).lower() for n in notes.values() if n.name)
    dup_titles = {t: sorted(f for f, n in notes.items() if scrub(n.name).lower() == t)
                  for t, c in titles.items() if c > 1}

    superseded = []
    for n in notes.values():
        for t in n.supersedes:
            superseded.append((t, n.file, t in note_set))

    rendered = {d: render_index(ix, members.get(d, [])) for d, ix in indexes.items()}

    return {
        "notes": notes,
        "indexes": indexes,
        "rendered": rendered,
        "current": current,
        "members": members,
        "inferred": sorted(f for f, n in notes.items() if n.inferred),
        "explicit": sorted(f for f, n in notes.items() if n.domains and not n.inferred),
        "multi": sorted(f for f, n in notes.items() if len(n.domains) > 1),
        "unlisted_memory_only": [f for f in unlisted if f in memory_links],
        "unlisted_orphans": [f for f in unlisted if f not in memory_links],
        "unknown_domain": sorted(unknown_domain),
        "dup_titles": dup_titles,
        "superseded": sorted(superseded),
        "missing_fields": sorted(f for f, n in notes.items() if not n.name or not n.description),
        "missing_indexes": missing_indexes,
    }


def _nonblank(text: str) -> Counter:
    return Counter(l.rstrip() for l in text.split("\n") if l.strip())


def report(r: dict, verbose: bool) -> list[str]:
    out = []
    notes, indexes = r["notes"], r["indexes"]
    out.append(f"Notes: {len(notes)}   Indexes: {len(indexes)}")
    if r["missing_indexes"]:
        out.append(f"Indexes MEMORY.md links that do not exist (--init creates them): "
                   f"{', '.join(r['missing_indexes'])}")
    out.append(f"Domain explicit in frontmatter: {len(r['explicit'])}")
    out.append(f"Domain inferred from current index links: {len(r['inferred'])}")
    out.append(f"Notes in more than one domain: {len(r['multi'])}")
    out.append("")
    out.append(f"{'index':<28}{'+lines':>8}{'-lines':>8}{'=lines':>8}{'+notes':>8}{'-notes':>8}{'ptrs':>6}")
    for d in sorted(indexes):
        ix = indexes[d]
        old, new = _nonblank(ix.text), _nonblank(r["rendered"][d])
        same = sum((old & new).values())
        added = sum((new - old).values())
        removed = sum((old - new).values())
        now = set(ix.linked)
        after = {n.file for n in r["members"].get(d, [])}
        changed = "" if r["rendered"][d] == ix.text else "  *"
        out.append(f"{ix.file:<28}{added:>8}{removed:>8}{same:>8}"
                   f"{len(after - now):>8}{len(now - after):>8}{len(ix.pointers):>6}{changed}")
        if verbose:
            for f in sorted(after - now):
                out.append(f"    + {f}")
            for f in sorted(now - after):
                out.append(f"    - {f}")
    out.append("  (* = file would change; +/-notes = note membership gained/lost vs today)")

    def section(title: str, items: list[str]) -> None:
        out.append("")
        out.append(f"{title}: {len(items)}")
        for i in items:
            out.append(f"  {i}")

    section("Not listed by any index, linked from MEMORY.md", r["unlisted_memory_only"])
    section("Not listed by any index, orphaned (no MEMORY.md link either)", r["unlisted_orphans"])
    section("Domain with no index-<domain>.md (skipped)", [f"{f}: {d}" for f, d in r["unknown_domain"]])
    section("Duplicate titles", [f"{t}: {', '.join(fs)}" for t, fs in sorted(r["dup_titles"].items())])
    section("Superseded notes (flag for removal)",
            [f"{t} superseded by {by}" + ("" if exists else " (target not in store)")
             for t, by, exists in r["superseded"]])
    section("Notes missing name or description", r["missing_fields"])
    dead = [f"{ix.file}: {t}" for ix in indexes.values() for t, _ in ix.dead_pointer_links]
    section("Pointer lines naming a .md that is not in the store", sorted(dead))
    if verbose:
        section("Inferred domains (write these into frontmatter)",
                [f"{f}: {', '.join(notes[f].domains)}" for f in r["inferred"]])
    return out


def init_indexes(store: str, missing: list[str]) -> list[str]:
    created = []
    for fname in missing:
        domain = fname[len("index-"):-len(".md")]
        path = os.path.join(store, fname)
        if os.path.exists(path):
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                f"# {domain.replace('-', ' ').replace('_', ' ').capitalize()}\n\n"
                f"Generated by chassis/scripts/memory-index-build.py from the `domain: {domain}` "
                "frontmatter of each note. Do not edit the list by hand.\n"
            )
        created.append(fname)
    return created


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", help="notes store directory (default: $CLAUDE_AUTO_MEMORY_DIR, then derived)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report only, write nothing (default)")
    mode.add_argument("--write", action="store_true", help="rewrite index-*.md files that changed")
    mode.add_argument("--init", action="store_true",
                      help="create missing index-*.md files that MEMORY.md links, then write")
    ap.add_argument("--verbose", "-v", action="store_true", help="list per-note membership changes and inferred domains")
    args = ap.parse_args(argv)

    found, how, searched = resolve_store(args.store)
    if found is None:
        print("store not found; searched: " + ", ".join(searched), file=sys.stderr)
        print("set CLAUDE_AUTO_MEMORY_DIR or pass --store", file=sys.stderr)
        return 2
    store = os.path.abspath(str(found))

    if args.init:
        created = init_indexes(store, build(store)["missing_indexes"])
        print(f"Created {len(created)} index file(s): {', '.join(created) or 'none'}")
        args.write = True

    r = build(store)
    print(f"Store: {store} ({how})")
    print(f"Mode: {'write' if args.write else 'check'}")
    print("\n".join(report(r, args.verbose)))

    if args.write:
        written = []
        for d, ix in r["indexes"].items():
            if r["rendered"][d] != ix.text:
                with open(os.path.join(store, ix.file), "w", encoding="utf-8") as f:
                    f.write(r["rendered"][d])
                written.append(ix.file)
        print("")
        print(f"Wrote {len(written)} index file(s): {', '.join(sorted(written)) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
