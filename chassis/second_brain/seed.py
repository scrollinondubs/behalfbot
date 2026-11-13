"""seed.py - idempotent starter content for a fresh second-brain install.

new-jaxity#257: a fresh Behalf.bot install configures a second-brain backend
(SiYuan, Notion or Obsidian) and gets an empty one. This seeds a small starter
skeleton - PARA top-level docs, a `_MAP` table of contents, a Priorities doc, a
Mental Models stub, and the Introspection pattern of Daily Log / Weekly Review /
Long-Arc Review templates - so day one has somewhere to write instead of a blank slate.

Written entirely against `NotesAdapter` (base.py). No backend-specific calls:
the same content lands correctly whichever of the three backends is configured,
because the adapter is what makes that true, not this module.

Idempotent by construction, which is the actual requirement here, not a nice-to-
have: a re-run (re-install, a second bootstrap pass, a manual invocation) must
never overwrite a user's own notes just because they happen to share a seeded
title. A sentinel doc (SENTINEL_TITLE, containing SENTINEL_MARKER) marks the
install as seeded; its presence is checked via `adapter.search` before writing
anything, and its absence is what triggers the actual seed. If a re-run somehow
races past the sentinel check for one specific doc, `create_doc` per backend
is asked to fail rather than clobber where it can - Obsidian raises outright on
an existing path; that failure is caught per-doc here and reported, not raised,
so one already-there doc can't abort the rest of the batch.
"""

from __future__ import annotations

import os
from pathlib import Path

from chassis.second_brain.base import NotesAdapter

SENTINEL_TITLE = "Second Brain Seed"
SENTINEL_MARKER = "<!-- behalfbot-second-brain-seed-v1 -->"
# The second-brain map (chassis/docs/second-brain-map.md). Its id goes in
# second_brain.map.doc_id; main() prints it so the installer can copy it.
MAP_TITLE = "_MAP"

SEED_DOCS: list[tuple[str, str]] = [
    (
        "Projects",
        "# Projects\n\n"
        "Things with a defined outcome and a deadline - or at least a "
        "finish line you'd recognize if you crossed it. If it never ends, "
        "it belongs in Areas, not here.\n",
    ),
    (
        "Areas",
        "# Areas\n\n"
        "Standards to maintain, not outcomes to reach. Health, finances, "
        "a relationship, an ongoing responsibility. No finish line - the "
        "job is to keep the standard, not complete it.\n",
    ),
    (
        "Resources",
        "# Resources\n\n"
        "Topics of ongoing interest that aren't tied to a current Project "
        "or Area. Reference material, things you're learning, ideas worth "
        "keeping around in case they become one of the other three later.\n",
    ),
    (
        "Archive",
        "# Archive\n\n"
        "Inactive items from the other three. A finished Project, a "
        "dropped Area, a Resource you no longer track. Moved here, not "
        "deleted - PARA's whole point is that nothing has to be thrown "
        "away to stay organized.\n",
    ),
    (
        "Priorities",
        "# Priorities\n\n"
        "## Immediate\n\n"
        "_What actually has to happen this week._\n\n"
        "## Soon\n\n"
        "_On deck - not urgent yet, but the next thing once Immediate "
        "clears._\n\n"
        "## Horizon\n\n"
        "_Worth doing eventually. Not scheduled, not forgotten._\n",
    ),
    (
        MAP_TITLE,
        "# _MAP\n\n"
        "The table of contents for this second brain. One line and one link "
        "per first-level doc in Projects, Areas and Resources - never sub-docs, "
        "never Archive. The assistant keeps it current and loads it at the "
        "start of every session; you own Priorities and your project docs.\n\n"
        "Each line: a link to the doc, what it is, what state it is in, and any "
        "nickname you use for it in conversation, so the assistant can match "
        "the nickname to the doc.\n\n"
        "## Projects\n\n"
        "- _Your first project goes here: one line and one link._\n\n"
        "## Areas\n\n"
        "## Resources\n\n"
        "- Priorities - what is urgent right now. Owned by you, not the "
        "assistant.\n",
    ),
    (
        "Mental Models",
        "# Mental Models\n\n"
        "Frameworks and heuristics worth reaching for on purpose, rather "
        "than reinventing under pressure. One entry per model: the name, "
        "the one-line version, and when it actually applies.\n",
    ),
    (
        "Daily Log Template",
        "# Daily Log Template\n\n"
        "**Date:**\n\n"
        "## What happened\n\n"
        "## What I noticed\n\n"
        "## Open loops\n\n"
        "Copy this structure into a new dated doc each day - this one "
        "stays a template, not a running log.\n",
    ),
    (
        "Weekly Review Template",
        "# Weekly Review Template\n\n"
        "**Week of:**\n\n"
        "## What moved\n\n"
        "## What stalled, and why\n\n"
        "## Adjust for next week\n\n"
        "Skim the week's Daily Logs before filling this in - the point is "
        "synthesis, not a fresh guess.\n",
    ),
    (
        "Long-Arc Review Template",
        "# Long-Arc Review Template\n\n"
        "**Period covered:**\n\n"
        "## Where I actually ended up vs. where I meant to\n\n"
        "## What pattern shows up across multiple Weekly Reviews\n\n"
        "## What to change about the arc itself, not just the next step\n\n"
        "Quarterly or slower - this is for patterns a single Weekly Review "
        "is too close to see.\n",
    ),
]


def seed_default_content(adapter: NotesAdapter, root_parent: str = "") -> dict:
    """Write SEED_DOCS under `root_parent` unless the sentinel already exists.

    Returns a dict with `seeded` (bool) and, when seeded, `created` (list of
    `{"title", "id"}` for every doc that actually got written - a doc that
    raised on create is reported under `failed` instead of aborting the rest).
    """
    for hit in adapter.search(SENTINEL_TITLE, limit=10):
        if hit.title == SENTINEL_TITLE:
            return {"seeded": False, "reason": "sentinel_exists", "created": []}

    created: list[dict] = []
    failed: list[dict] = []
    for title, body in SEED_DOCS:
        try:
            doc_id = adapter.create_doc(root_parent, title, body)
        except Exception as exc:  # noqa: BLE001 - report, don't abort the batch
            failed.append({"title": title, "error": str(exc)})
            continue
        created.append({"title": title, "id": doc_id})

    sentinel_body = (
        f"{SENTINEL_MARKER}\n\n"
        "This doc marks the default second-brain starter content as seeded. "
        "Delete it if you want the seeder to run again on the next install - "
        "it will re-create only docs that are missing; it never overwrites "
        "an existing one.\n"
    )
    try:
        sentinel_id = adapter.create_doc(root_parent, SENTINEL_TITLE, sentinel_body)
        created.append({"title": SENTINEL_TITLE, "id": sentinel_id})
    except Exception as exc:  # noqa: BLE001
        failed.append({"title": SENTINEL_TITLE, "error": str(exc)})

    result = {"seeded": True, "created": created}
    if failed:
        result["failed"] = failed
    return result


PARA_TITLES = ("Projects", "Areas", "Resources")


def _title_matches(title: str, word: str) -> bool:
    """`Projects`, `1 Projects` and `1. Projects` all count as the Projects doc."""
    t = title.strip().lower()
    return t == word.lower() or t.endswith(" " + word.lower())


def has_existing_para(adapter: NotesAdapter) -> bool:
    """True when the second brain already has a PARA top level or a map.

    Direct mode is how installs that predate the seeder run, and their second
    brains are not empty. Seeding into one would add ten docs beside the
    installer's own Projects / Areas / Resources, so any sign of an existing
    layout means hands off.
    """
    for word in PARA_TITLES + (MAP_TITLE,):
        for hit in adapter.search(word, limit=20):
            if _title_matches(hit.title, word):
                return True
    return False


def find_map_id(adapter: NotesAdapter) -> str:
    for hit in adapter.search(MAP_TITLE, limit=20):
        if hit.title.strip() == MAP_TITLE and hit.id:
            return hit.id
    return ""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_key(line: str, key: str) -> bool:
    body = line.strip()
    return not body.startswith("#") and body.split(":", 1)[0].strip() == key and ":" in body


def _block(lines: list[str], header: int) -> range:
    """Line indexes inside the mapping whose `key:` header is at `header`."""
    base = _indent(lines[header])
    end = header + 1
    while end < len(lines):
        body = lines[end].strip()
        if body and not body.startswith("#") and _indent(lines[end]) <= base:
            break
        end += 1
    return range(header + 1, end)


def set_map_doc_id(config_path: Path, doc_id: str) -> str:
    """Write `second_brain.map.doc_id` in place, only when it is empty.

    A line edit, not a YAML round-trip, so the file's comments survive.
    Returns "set", "already_set" (a value is there; left alone) or
    "no_key" (no `second_brain.map.doc_id` line to fill; nothing written).
    """
    lines = config_path.read_text().split("\n")
    sb = next((i for i, l in enumerate(lines) if _indent(l) == 0 and _is_key(l, "second_brain")), -1)
    if sb < 0:
        return "no_key"
    mp = next((i for i in _block(lines, sb) if _is_key(lines[i], "map")), -1)
    if mp < 0:
        return "no_key"
    di = next((i for i in _block(lines, mp) if _is_key(lines[i], "doc_id")), -1)
    if di < 0:
        return "no_key"
    line = lines[di]
    head, _, rest = line.partition(":")
    value, hashpos = rest, rest.find("#")
    comment = ""
    if hashpos >= 0:
        value, comment = rest[:hashpos], rest[hashpos:]
    if value.strip() not in ("", '""', "''", "~", "null"):
        return "already_set"
    new = f'{head}: "{doc_id}"'
    if comment:
        col = len(head) + 1 + hashpos
        new = new + " " * max(1, col - len(new)) + comment
    lines[di] = new
    config_path.write_text("\n".join(lines))
    return "set"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point - `python3 -m chassis.second_brain.seed [--config FILE]`.

    Resolves the configured adapter the same way the MCP server does
    (`factory.get_adapter`). The adapter is a library, so it works in either
    `second_brain.mode`; the mode only decides which MCP server a session sees.

    - `adapter` mode: seed unless the sentinel says it already ran.
    - `direct` mode: seed only when `second_brain.scaffold_para_if_missing` is
      not false AND the second brain has no PARA top level or map yet, so a
      long-running install is never handed a second set of top-level docs.

    Either way, when `second_brain.map.doc_id` is empty and a `_MAP` doc exists
    (just seeded, or found), its id is written into the config, which is what
    switches on the SessionStart map hook. A backend that is not configured
    is not a failure here: the step says so and exits 0.
    """
    import argparse

    from chassis.second_brain import factory

    ap = argparse.ArgumentParser(prog="python3 -m chassis.second_brain.seed")
    ap.add_argument("--config", help="chassis.config.yaml (default: $CHASSIS_HOME/chassis.config.yaml)")
    args = ap.parse_args(argv)
    config_path = Path(args.config) if args.config else None

    try:
        mode = factory.get_mode(config_path)
        config = factory._load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        print(f"second-brain seed: could not read second_brain config ({exc}); skipping.")
        return 0
    sb_config = config.get("second_brain") or {}

    try:
        # get_adapter returns the two-surface adapter; the seeder writes notes.
        adapter = factory.get_adapter(config_path).notes
    except Exception as exc:  # noqa: BLE001
        print(f"second-brain seed: could not construct adapter ({exc}); skipping.")
        return 0

    map_id = ""
    if mode == "direct" and sb_config.get("scaffold_para_if_missing") is False:
        print("second-brain seed: scaffold_para_if_missing is false; not seeding.")
    elif mode == "direct" and has_existing_para(adapter):
        print("second-brain seed: direct mode and the second brain already has a PARA "
              "layout or a _MAP; not seeding.")
    else:
        result = seed_default_content(adapter)
        if not result["seeded"]:
            print(f"second-brain seed: already seeded ({result['reason']}), no-op.")
        else:
            print(f"second-brain seed: created {len(result['created'])} doc(s).")
            for doc in result["created"]:
                if doc["title"] == MAP_TITLE and doc["id"]:
                    map_id = doc["id"]
            for failure in result.get("failed", []):
                print(f"second-brain seed: FAILED {failure['title']!r}: {failure['error']}")

    current = (sb_config.get("map") or {}).get("doc_id") if isinstance(sb_config.get("map"), dict) else None
    if current:
        return 0
    map_id = map_id or find_map_id(adapter)
    if not map_id:
        print("second-brain seed: no _MAP doc found; second_brain.map.doc_id stays empty "
              "(chassis/docs/second-brain-map.md).")
        return 0
    target = config_path or Path(os.environ.get("CHASSIS_HOME", ".")) / "chassis.config.yaml"
    outcome = set_map_doc_id(target, map_id)
    if outcome == "set":
        print(f"second-brain seed: map doc id {map_id} written to second_brain.map.doc_id in {target}.")
    else:
        print(f"second-brain seed: map doc id {map_id} - set second_brain.map.doc_id to it "
              f"in {target} to load the map into every session ({outcome}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
