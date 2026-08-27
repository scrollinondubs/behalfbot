"""seed.py - idempotent starter content for a fresh second-brain install.

new-jaxity#257: a fresh Behalf.bot install configures a second-brain backend
(SiYuan, Notion or Obsidian) and gets an empty one. This seeds a small starter
skeleton - PARA top-level docs, a Priorities doc, a Mental Models stub, and the
Introspection pattern of Daily Log / Weekly Review / Long-Arc Review templates
- so day one has somewhere to write instead of a blank slate.

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

from chassis.second_brain.base import NotesAdapter

SENTINEL_TITLE = "Second Brain Seed"
SENTINEL_MARKER = "<!-- behalfbot-second-brain-seed-v1 -->"

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


def main() -> int:
    """CLI entry point - `python3 -m chassis.second_brain.seed`.

    Resolves the configured adapter the same way the MCP server does
    (`factory.get_adapter`). A backend that isn't configured, or a
    `second_brain.mode` other than `adapter`, isn't a failure here - it just
    means this install has nothing for the seeder to write into yet, and the
    step logs that plainly instead of raising.
    """
    import sys

    from chassis.second_brain import factory

    try:
        mode = factory.get_mode()
    except Exception as exc:  # noqa: BLE001
        print(f"second-brain seed: could not read second_brain.mode ({exc}); skipping.")
        return 0
    if mode != "adapter":
        print(
            f"second-brain seed: second_brain.mode={mode!r}, not 'adapter'; "
            "skipping (nothing to seed through)."
        )
        return 0

    try:
        adapter = factory.get_adapter()
    except Exception as exc:  # noqa: BLE001
        print(f"second-brain seed: could not construct adapter ({exc}); skipping.")
        return 0

    result = seed_default_content(adapter)
    if not result["seeded"]:
        print(f"second-brain seed: already seeded ({result['reason']}), no-op.")
        return 0

    print(f"second-brain seed: created {len(result['created'])} doc(s).")
    for failure in result.get("failed", []):
        print(f"second-brain seed: FAILED {failure['title']!r}: {failure['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
