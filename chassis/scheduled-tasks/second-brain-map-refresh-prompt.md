# Second-brain map refresh

This prompt runs when `chassis/scripts/gather-second-brain-map-drift.sh` finds that the map and the second brain disagree. The gather's JSON array is appended below; read it first. Setup and the opt-in registration: `chassis/docs/second-brain-map.md`.

---

You are this install's assistant. Read `CLAUDE.md` in the install root for your operating manual.

The map (`second_brain.map.doc_id` in `chassis.config.yaml`) is the table of contents for the installer's second brain. Every session loads it at startup through `chassis/.claude/hooks/load-second-brain-map.sh`, so a wrong map means every session that day starts with a wrong picture of what exists. You own the map. The installer owns their priorities doc and their project docs.

## What the gather output means

Each element is one drifted doc:

- `missing_from_map` - a first-level doc exists under one of the configured roots (`root` names which) and the map does not link it. Add an entry.
- `stale_ref_in_map` - the map links a doc that is no longer a first-level doc under any root: moved under another doc, archived, or deleted. Remove the entry. Sub-docs are deliberately not listed and Archive is deliberately excluded, so all three cases mean removal.
- `probe_failed` - the check could not run. Do **not** conclude the map is fine. Post the `detail` to the alerts channel and stop.

## What to do

1. Read the map with its links intact. On SiYuan use the kramdown read (`get_block_kramdown` on the native MCP), not a markdown export: export inlines every referenced doc and turns a few KB into tens of KB. Through the adapter, `read_doc` on a SiYuan map has the same problem; prefer the native tool when it is registered.
2. For each `missing_from_map` doc, read enough of it to write **one honest line**: what it is, what state it is in, and any nickname the installer uses for it. Do not write the line from the title alone. A title is often an abbreviation or an in-joke, and a line guessed from it is a wrong fact loaded into every session.
3. Put it under the section matching its `root` (Projects for work with an end state, Areas for ongoing responsibilities, Resources for reference).
4. Apply the `stale_ref_in_map` removals.
5. Update the generated-on date in the footer, if the map has one.

Each entry links the doc in the backend's own link form, so the link resolves and the drift check can see it: `((<block id> 'Title'))` on SiYuan, `[[Note name]]` on Obsidian, a page mention on Notion. Never write an id you did not read back from the backend.

## Constraints

- **Keep it short.** The map is loaded into every session. One line per entry; detail belongs in the doc it points at.
- **Never list sub-docs.** A doc's children are one search away; listing them defeats the point of an index.
- **Never touch the installer's priorities doc** or any of their own docs. You edit the map and nothing else.
- **Nothing from Archive.**
- **Edit the map in place.** Do not recreate it: its id is in `chassis.config.yaml` and the session-start hook reads it from there. Use the backend's native edit tools (SiYuan `update_block` / `append_block`, the Notion MCP, a file edit on an Obsidian vault). If this install runs `second_brain.mode: adapter` and has no native edit tool, do not append the changes to the end of the map: post the exact lines to add and remove to the alerts channel for the installer to apply, and stop.

## Reporting

Silent on success is wrong here: the installer should know the map changed under them. Post one short line to the alerts channel (display label `discord_channels.alerts_label` in `chassis.config.yaml`, runtime id `DISCORD_ALERTS_CHANNEL_ID` in `.env`) naming what was added and removed. If the only finding was a doc you decided not to add, post nothing and say why in your stdout report.

No em dashes in anything you write. Use " - ".
