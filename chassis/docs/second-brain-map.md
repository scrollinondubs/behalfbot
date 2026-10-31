# The second-brain map

A session starts knowing what is in `CLAUDE.md` and nothing about what is in the installer's second brain. Without help it re-asks for context that is already written down, or worse, tells the installer it has none. The failure this exists for: a session searched one store, found nothing, and reported starting blind on a meeting the next day, while a long prep doc for exactly that meeting sat in the second brain, written the evening before.

The map fixes the "what exists" half of that. It is opt-in: an install that sets nothing sees no change.

Issue: scrollinondubs/behalfbot#208. Memory model it belongs to: `chassis/memory/MEMORY_MODEL.md`.

## What the map is

One doc in the second brain, conventionally titled `_MAP`, holding one line and one link per **first-level** doc in Projects, Areas and Resources.

- **Archive is excluded on purpose.** Archived means not current.
- **Sub-docs are never listed.** A doc's children are related to it by definition and are one search away. Listing them turns an index into a dump nobody reads.
- **Each line carries aliases.** If the installer calls a project by an abbreviation or a nickname, the line says so, because a session that only sees the full name has nothing to match the nickname against.
- **The assistant owns it; the installer owns their priorities doc.** The map answers "what exists and where", priorities answer "what is urgent". Different clocks, different owners, never merged. A second copy of priorities inside the map, or inside `CLAUDE.md`, goes stale - that has happened on the reference install, twice in six weeks.

A fresh install gets a half-written `_MAP` from the second-brain seed (`python3 -m chassis.second_brain.seed`), which prints its id. An existing install creates the doc by hand in its Resources area.

## Three parts

| Part | File | Cost |
|---|---|---|
| Session-start load | `chassis/.claude/hooks/load-second-brain-map.sh` | Free, every session |
| Drift check | `chassis/scripts/gather-second-brain-map-drift.sh` | Free, daily |
| Refresh | `chassis/scheduled-tasks/second-brain-map-refresh-prompt.md` | A model run, only when the check finds drift |

All three call `chassis/scripts/second-brain-map.py`, which reads through the second-brain adapter, so they work on SiYuan, Notion and Obsidian.

## Configure

In the install's `chassis.config.yaml`:

```yaml
second_brain:
  backend: siyuan
  map:
    doc_id: "<map doc id>"        # env SECOND_BRAIN_MAP_DOC overrides
    roots:                        # only the drift check needs these
      projects: "/Projects"
      areas: "/Areas"
      resources: "/Resources"
```

`roots` maps a label to a container, in the forms `list_children` takes:

| Backend | Container |
|---|---|
| SiYuan | an hpath in the configured notebook (`/Projects`), a doc block id, or `notebook:<id>` when each PARA area is its own notebook |
| Notion | the parent page id |
| Obsidian | a vault directory (`Projects/`) |

Credentials are the ones the adapter already reads (`SIYUAN_URL` / `SIYUAN_TOKEN`, `NOTION_API_TOKEN`, the Obsidian vault path). The hook runs with Claude Code's environment, which usually lacks them, so the script fills any unset var from the install's `.env`. On SiYuan it also retries loopback and the docker host gateway when the configured URL refuses the connection, because `.env` usually holds the container's view of the kernel and the hook runs on the host.

## Register the hook

Hooks ship in this repo but are wired up host-side, in the settings file Claude Code reads (`docs/security.md` covers the same step for the guardrails). Add:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "<CHASSIS_HOME>/chassis/.claude/hooks/load-second-brain-map.sh", "timeout": 20 }
        ]
      }
    ]
  }
}
```

Then add the directive to the install's `CLAUDE.md` ("Every Session" in `chassis/CLAUDE.md.template`). The pair is deliberate: the hook is the deterministic half and cannot be skipped; the directive tells a session what to do when the hook did not fire.

## Not cached, on purpose

When the map is configured and cannot be read, the hook does not fall back to a cached copy and does not stay quiet. It injects a banner beginning `SECOND BRAIN UNREACHABLE` that tells the session to raise it in its first message. The reference install's operator put it this way: if the second brain is down there are bigger problems, and the right response is to fail visibly and get it fixed, not paper over it with yesterday's copy. A cached map would also have hidden a broken second-brain connection for as long as the cache lived.

The drift check follows the same rule: a check that cannot run emits `probe_failed`, which fires the heartbeat, rather than an empty array that reads as all clear.

## Read path

The map's links are its content. `read_doc` on SiYuan exports markdown, which resolves every block ref and inlines the referenced doc - a 3KB map came back as 51KB. So the script reads with `read_doc_source`, which keeps links as links: SiYuan kramdown with refs as `((id 'Title'))`, the raw file on Obsidian, and on Notion the rendered text plus `notion:<page id>` for every page mention and link-to-page block.

## Drift check heartbeat

Shipped commented out in `HEARTBEATS.md.template` as `second-brain-map-refresh`. Daily is enough: the shape of a second brain changes over weeks, not hours. Output elements:

| `kind` | Means |
|---|---|
| `missing_from_map` | A first-level doc under `root` that the map does not link |
| `stale_ref_in_map` | The map links a doc that is not first-level under any root (moved, archived or deleted) |
| `probe_failed` | The check could not run; `detail` says why. A root that cannot be listed suppresses `stale_ref_in_map`, since every link into it would otherwise look stale |

Linking is matched per backend: SiYuan by block id in `((...))` refs, Notion by page id, Obsidian by `[[name]]` or `[[path]]` and markdown links to `.md` files.

**Known limit:** the adapter MCP (`second_brain.mode: adapter`) has no tool to edit a doc in place, so on an adapter-mode install without the backend's native MCP, the refresh run posts the lines to change instead of making the edit.
