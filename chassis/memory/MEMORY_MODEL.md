# Memory model

One fact, one home. Everywhere else, a pointer to it.

A fact kept in two places is two things to keep in sync, and one of them goes stale first. The model below gives every kind of fact exactly one home, says how to point at it from anywhere else, and ships the tools that check the pointers still resolve.

This replaces the old `WRITING_RULES.md` (scrollinondubs/behalfbot#219). The taxonomy and the "what not to save" rules carried over; what is new is the layer table, the pointer formats, generated indexes, and an optional memory MCP.

## Where facts live

| Layer | Home | Holds |
|---|---|---|
| Long-term facts | The second brain, through the adapter (SiYuan, Obsidian or Notion; `docs/second-brain-adapters.md`) | The installer's world: people, projects, decisions, plans, priorities |
| Infrastructure facts | `docs/` in the install repo | How this install is built and run: hosts, mounts, quirks |
| Procedures | `skills/` in the install repo | How to do a recurring task |
| Constitution | `CLAUDE.md` | Identity, hard limits, where to look. Kept small |
| Working memory | The auto-memory notes store | Corrections only, plus pointer lines, with generated indexes |
| Engineering decisions | GitHub issues | Already the convention |
| Raw history | Transcripts, chat | Never edited |

If a fact fits a layer above the notes store, it goes there, and the note becomes a one-line pointer or goes away.

The `memory` MCP knowledge graph is optional (see the last section). It is not a layer of its own: a graph entity holding a fact that also lives in the second brain or the notes store is the second copy this model exists to remove.

## The notes store

The notes store is Claude Code's auto-memory directory for the install, `~/.claude/projects/<project dir>/memory/`. `<project dir>` is the install root's absolute path with every non-alphanumeric character turned into `-` (`/home/ana/.behalfbot` becomes `-home-ana--behalfbot`), fixed by the path the install was first opened at. Inside the chassis container that path differs from the host's, so a containerized install sets `CLAUDE_AUTO_MEMORY_DIR` in `.env`. Every chassis memory script resolves the store the same way (`chassis/scripts/_memory_store.py`).

### Two levels

- **`MEMORY.md`** is the root, loaded into every session. It holds only the rules that must never be one hop away (never deceive the installer, privacy boundaries, anything that has already gone wrong once), plus one pointer per domain index. Keep it short; lines past about 200 are truncated on load.
- **`index-<domain>.md`**, one per domain, lists every note in that domain, one line each. These are generated from note frontmatter by `chassis/scripts/memory-index-build.py`. Never edit the list by hand; hand-kept indexes are where stale narrative piles up.
- **Subfolders** belong to a subagent with its own index. The generator and the dream heartbeat leave them alone.

Templates: `MEMORY.md.template`, `index-domain.md.template`.

### What a note is for

Corrections: something the assistant got wrong and must not repeat, or a non-obvious approach the installer confirmed. A note about the installer's world belongs in the second brain; a note about how the install runs belongs in `docs/`. When one of those turns up in the store, the note becomes a pointer to the fact's real home.

| Type | When to save | Body structure |
|---|---|---|
| **feedback** | Guidance about how to approach work: corrections AND confirmations | Lead with the rule, then a `**Why:**` line and a `**How to apply:**` line |
| **user** | The installer's role, goals, responsibilities, knowledge, as it shapes how to work with them | Plain prose. Bio facts belong in the second brain |
| **project** | A project decision that changes how to act, with its reason | Lead with the decision, then `**Why:**` and `**How to apply:**`. Project state itself belongs in the second brain |
| **reference** | Where information lives in an external system | Plain prose with the URL, id or path, in the pointer formats below |

Plugins may add namespaced types (`lead:`, `topic:`, `student:`) when they are active. The same rule applies: a lead's history is the installer's world and lives in the second brain or the plugin's own store; a note keeps only what the assistant must not get wrong about it.

### Frontmatter

The index generator reads it, so every note carries it:

```yaml
---
name: Never bare COUNT(*) on tickets
description: Filter status='paid' or refunded rows overstate sales.
type: feedback
domain: workflow
supersedes: feedback_old_ticket_rule
---
```

- `name` is the link title in the index; `description` is the text after it. Both required.
- `type` is one of the types above.
- `domain` is the index name without `index-` and `.md`, so a note for `index-tools.md` gets `domain: tools`. A note in two indexes takes a list: `domain: [tools, workflow]`.
- `supersedes` (optional) names the note this one replaces: filename with or without `.md`, or `[[name]]`. The generator deletes nothing; it flags the superseded note so it can be removed.

The fields may sit at the top level or under `metadata:`; top level wins. Until every note has `domain:`, a note without one keeps whichever indexes link it today, and the generator counts it as "inferred".

### Saving a note

1. Write the note to its own file (`feedback_testing.md`), with the frontmatter above.
2. Regenerate the indexes: `python3 chassis/scripts/memory-index-build.py --write`.

Touch `MEMORY.md` only for a root-level rule or a new domain. A new domain needs a link in `MEMORY.md`; `memory-index-build.py --init` then creates its index.

If the installer asks you to remember something, save it at once in the layer it belongs to. If they ask you to forget something, find and remove it.

### What NOT to save

- Code patterns, conventions, architecture, file paths, project structure: derive them from the repo.
- Git history, who changed what: `git log` and `git blame` are authoritative.
- Debugging solutions or fix recipes: the fix is in the code, the context in the commit message.
- Anything already in `CLAUDE.md`, `docs/` or `skills/`. Point at it instead.
- Ephemeral task details: in-progress work, temporary state, this conversation's context.

These exclusions hold even when the installer explicitly asks to save. If they ask you to save a PR list or an activity summary, ask what was surprising or non-obvious about it; that is the part worth keeping.

## Pointer formats

A pointer only works if it survives the thing it points at being renamed or moved, so pointers have a fixed shape.

**Second brain: `<backend>:<doc id>`, never a title or a path of names.**
- `siyuan:20240101120000-abc1234` (block id), `notion:0123456789abcdef0123456789abcdef` (page id), `obsidian:Projects/Plan.md` (vault-relative path).
- The id is what `mcp__secondbrain__create_doc` returns and what `read_doc` accepts. Treat it as opaque.
- Never "the Priorities doc" or `/Projects/Plan`. Titles and hpaths change; ids do not (Obsidian's id is its path, so a rename there is a real move, and the checker will see the dead ref).
- The backend prefix tells a reader which store resolves it, and lets an install that switches backend find every stale pointer with one grep.

**Repo files: path from the install repo root.**
- `docs/hosts.md`, `skills/outreach.md`, `chassis/scripts/memory-integrity.py`.
- Never an absolute path into the install (`/home/<user>/.behalfbot/...`, `/app/customer/...`, `~/.behalfbot/...`). The same repo sits at different roots on the host and in the container, so an absolute path is wrong in one of the two.

**GitHub: `owner/repo#N`, or the full github.com URL.**
- `acme/widgets#42`, `https://github.com/acme/widgets/pull/43`.
- Never a bare `#42`. Repos get renamed and transferred, and a bare number then resolves to the wrong issue. A short `repo#N` is tolerated; prefer the full form in new text.

**Note to note: `[[note_name]]` or `[Title](file.md)`, resolving to a file in the store.**
- The name matches the filename exactly, snake_case included. `[[feedback-never-deceive]]` does not resolve to `feedback_never_deceive.md`.
- Every note is linked from `MEMORY.md` or an `index-*.md`, or it is an orphan nobody will read.

## Checking

```
python3 chassis/scripts/memory-integrity.py                       # text report
python3 chassis/scripts/memory-integrity.py --json                # machine output
python3 chassis/scripts/memory-integrity.py --probe-second-brain  # also read_doc() every second-brain ref
```

Read-only, always exits 0. It scans the notes store and the install repo's `CLAUDE.md`, `skills/`, `docs/` and `.claude/agents/`, and reports: repo paths that do not exist, absolute paths into the install, bare `#N` refs, dead note links (with a hint when only kebab vs snake case differs), orphan notes and unlinked indexes, and second-brain refs whose id is badly formed for its backend.

Whether a well-formed second-brain id still resolves is the backend's answer, not a regex's, so that check is adapter-provided: `--probe-second-brain` asks the configured adapter (`chassis/second_brain`) to `read_doc` each ref, on any backend. It is off by default because it is a network call per ref on SiYuan and Notion. The adapter reads credentials from the environment, so a hand run loads `.env` first (`set -a; . .env; set +a`).

```
python3 chassis/scripts/memory-index-build.py            # --check: report only
python3 chassis/scripts/memory-index-build.py --verbose  # per-note changes, inferred domains
python3 chassis/scripts/memory-index-build.py --write    # rewrite index-*.md files that changed
python3 chassis/scripts/memory-index-build.py --init     # create indexes MEMORY.md links, then write
```

A generated index keeps everything above its first list item verbatim (title, intro), then one line per note sorted by filename, `- [<name>](<file>) - <description>`, then a `## Pointers (homes outside the notes store)` section holding every hand-written line that linked no note in the store. The report lists notes no index reaches, domains with no index, duplicate titles, superseded notes, notes missing `name` or `description`, and pointer lines that name a missing `.md`.

## Reading and writing

**When to read.** When a memory seems relevant, when the installer refers to earlier work, and always when they ask you to check, recall or remember. If they say to ignore memory, do not apply, cite or mention it.

**Search order before saying "I have no context on this".** An empty result from one store is evidence of one empty store, not of no context. Work down the tiers, and when you do report a blind spot, name the tiers you actually checked:

1. **The second brain** (`mcp__secondbrain__search`, or the backend's own MCP in direct mode). The installer writes there daily; it is almost always the hit. Start here, and use the map (below) to find the right doc.
2. **The notes store.** Your own corrections on the same subject.
3. **Chat history.** Discord's search API is not open to bots, so this means pulling and reading history: good for recent, expensive for old.
4. **Email,** when an outside party is involved.

The failure this order exists for: a session searched one store for a name, found nothing, and reported starting blind, while a long prep doc for a call the next day sat in the second brain, written the evening before. The instruction had named the wrong store.

**Docs are claims; the system is the truth.** A README, an issue body, a comment or a note records what was true when it was written. The system records what is true now. Before asserting that something is or is not deployed, running, configured or broken, ask what the authoritative surface is - the account, the database, the live endpoint, the process list - and query it. A doc is a hint about where to look, never the answer. A note naming a file, function or flag says it existed when the note was written; check that it still does before recommending it. If verifying is genuinely expensive, say so and ask whether it is worth it rather than quietly falling back on the document. And if one doc turned out stale, assume others are too.

The reference install learned this when a session concluded a component had never been deployed by reading its README, which described a deploy still awaiting approval. It had been live for two weeks; the account was one query away. The wrong conclusion then sat in the installer's task list for over a week.

**Write as well as read.** When a session produces real work on a person, company or project, file it before the session ends, in its layer: a fact about the installer's world appended to its second-brain doc under a heading that marks it as yours (`## <assistant> - YYYY-MM-DD`), never rewriting the installer's own text; a correction as a note; an infrastructure fact in `docs/` on a branch and PR.

**Link what you write.** Every second-brain write by the assistant carries real links to:

- the project or area doc it belongs to, and
- every person, company, place or concept doc it names.

Use the backend's own link form (`((<block id> 'Title'))` on SiYuan, `[[Note]]` on Obsidian, a page mention on Notion), so the link resolves and backlinks work. If a named doc does not exist, create it in the same session with real content, read its id back from the backend, then link it. Never invent an id: a dead link looks exactly like a live one. Before reporting the write as done, check the links landed (on SiYuan, `SELECT count(*) FROM refs WHERE root_id = '<doc id>'`; 0 means the write is unlinked).

Why: keyword search misses nicknames and synonyms, which is how the search failure above happened; a link is exact, and backlinks for any doc are one query away. On the reference install fewer than one doc in five linked to anything before this rule, because installers do not hand-link and nothing else did. Do not link for density: a few links in the right places beat many, because a session follows them one hop at a time.

## The map: the second brain's table of contents (opt-in)

`chassis/docs/second-brain-map.md`. One doc in the second brain, conventionally `_MAP`, with one line and one link per first-level doc in Projects, Areas and Resources - Archive excluded, sub-docs never listed. A SessionStart hook loads it into every session, uncached and loud when unreachable, and a daily gather-first heartbeat flags drift between the map and the real docs.

Ownership is the point. The assistant owns the map ("what exists and where"); the installer owns their priorities doc ("what is urgent") and their project docs. Never keep a copy of priorities in the map or in `CLAUDE.md`: a second copy of a fact is a second thing to forget to update, and a priorities list in `CLAUDE.md` goes stale within weeks.

## Dream: nightly consolidation (opt-in)

`chassis/docs/heartbeats/dream.md`. A free gather runs the two scripts above every night and fires a paid run only when the rotation is due or something new broke. The run edits the notes store and commits it by path; facts that belong elsewhere are proposed, never moved: a `dream/<date>` PR against the install repo, and one block in an assistant-owned inbox doc through the second-brain adapter. It never edits the installer's own docs unattended.

## The memory MCP is optional

The `memory` MCP knowledge graph ships enabled, and an install that never touches the setting behaves exactly as before. `memory.mcp_enabled: false` in `chassis.config.yaml` retires it:

- the next `.mcp.json` hydrate no longer registers the `memory` server;
- `bootstrap-audit` Gap 3 reports "skipped" instead of failing every week;
- the `memory-canary` heartbeat reports `stage: skipped` and stops paging.

Retiring it is the right call once its contents live in the layers above: the graph tends to become a third copy of facts the second brain and the notes store already hold, read by nobody on purpose. Leave the graph file on disk, read-only, as an archive. The steps for an existing install: `docs/memory-architecture-migration.md`.

While the memory MCP is enabled, entities named `health:*` are reserved for chassis monitors. `health:memory-canary` is rewritten every day by the `memory-canary` heartbeat and holds only a note and a nonce. Never write a `health:` entity, never return one from recall, and drop it if it turns up in a search.
