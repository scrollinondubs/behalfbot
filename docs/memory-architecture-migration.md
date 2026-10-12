# Memory architecture migration (existing installs)

For installs set up before scrollinondubs/behalfbot#219: moving to the "one fact, one home" memory model in `chassis/memory/MEMORY_MODEL.md`. New installs get the templates from day one and only need steps 3 to 6 once there are notes to index.

Nothing in the chassis update does any of this for you. An install that skips every step behaves exactly as it did before the update: the memory MCP stays on, the canary and the audit check it as before, and dream stays off.

Read `chassis/memory/MEMORY_MODEL.md` first. The steps below are the mechanics; the model says why.

## 0. Before you start

- Find your notes store. It is Claude Code's auto-memory directory for the install, `~/.claude/projects/<mangled install path>/memory/` on the host (the install path with every non-alphanumeric character replaced by `-`). Inside the container it is wherever `~/.claude` is bind-mounted. Set `CLAUDE_AUTO_MEMORY_DIR` to that path in `.env` on a containerized install; every chassis memory script reads it.
- Make the store a git repo if it is not one yet, so every step below is reviewable and revertable:

  ```bash
  STORE="$CLAUDE_AUTO_MEMORY_DIR"
  git -C "$STORE" init && git -C "$STORE" add -A && git -C "$STORE" commit -m "baseline before memory migration"
  ```

  (This is the store's own private repo, not the install repo. `add -A` is fine here, once, on a repo you just created.)
- Take a backup of `memory/memory.jsonl` if the memory MCP is on.

## 1. Decide about the memory MCP

Keep it if something on the install still reads the graph on purpose. Retire it if its facts now live in the second brain and the notes store and nothing reads it deliberately (the common case: it became a third copy).

To retire it:

1. Move anything still worth keeping out of the graph into its layer: people, projects and leads into the second brain; corrections into notes; infrastructure facts into `docs/`; decisions into GitHub issues. Do not migrate entities wholesale.
2. Set the flag in `chassis.config.yaml`:

   ```yaml
   memory:
     mcp_enabled: false
   ```

3. Re-hydrate `.mcp.json` so the `memory` server is no longer registered (or remove the `memory` block from `.mcp.json` by hand). `python3 chassis/scripts/reconcile-mcp-config.py` shows the diff first.
4. Remove `mcp__memory__*` calls from your own prompts, skills and `CLAUDE.md`. `grep -rn mcp__memory__ CLAUDE.md skills scheduled-tasks .claude` should come back empty. The chassis's own prompts do not call it; one vendored plugin prompt still does (`plugins/dating/CLAUDE.md`, owned by behalfbot-plugins and not changed here), so leave the flag on if you run that plugin until it is updated.
5. Leave `memory/memory.jsonl` on disk, read-only, as an archive.

With the flag false, `bootstrap-audit` Gap 3 reports "skipped" instead of failing every Sunday, and the `memory-canary` heartbeat reports `stage: skipped` and stays silent. Neither needs a change in `HEARTBEATS.md`.

## 2. Make `MEMORY.md` the two-level root

Compare your `MEMORY.md` with `chassis/memory/MEMORY.md.template`. The root keeps only the rules that must never be one hop away (never deceive, privacy boundaries, anything that has already caused real damage) and one line per domain index:

```markdown
- **[Tools](index-tools.md)** - which tool to reach for, auth quirks, fallbacks.
```

Pick domains that match how your notes cluster (the template's six are a starting point). Move every other note line out of `MEMORY.md`; step 3 puts each note in its index.

## 3. Add frontmatter

Every note needs `name`, `description`, `type` and `domain`:

```yaml
---
name: Never bare COUNT(*) on tickets
description: Filter status='paid' or refunded rows overstate sales.
type: feedback
domain: workflow
---
```

`domain` is the index name without `index-` and `.md`. Most notes already have the first three. For `domain`, the index builder will tell you what it can infer from where each note is linked today:

```bash
python3 chassis/scripts/memory-index-build.py --verbose
```

The "Inferred domains" section at the end is the list to write into frontmatter. While you are in each note, apply the model: a note holding a fact about the installer's world becomes a one-line pointer to its second-brain doc (`<backend>:<doc id>`), and an infrastructure fact moves to `docs/`.

## 4. Build the indexes

```bash
python3 chassis/scripts/memory-index-build.py --init     # creates index-*.md that MEMORY.md links, then writes
python3 chassis/scripts/memory-index-build.py            # check: should report no changes
```

`--init` creates a missing index for every domain `MEMORY.md` links, then rewrites every index from frontmatter. Read the report: notes no index lists, domains with no index, duplicate titles, superseded notes and notes missing a `name` or `description` are all things to fix in frontmatter, then run `--write` again. From now on, never edit an `index-*.md` by hand.

Commit the store: `git -C "$STORE" commit -m "two-level indexes" -- <paths>`.

## 5. Run the integrity checker

```bash
python3 chassis/scripts/memory-integrity.py --repo "$CUSTOMER_HOME"
```

It reports dead and badly formed pointers across the notes store and the install repo's `CLAUDE.md`, `skills/`, `docs/` and `.claude/agents/`: repo paths that no longer exist, absolute paths into the install, bare `#N` issue refs, dead note links, orphan notes, and malformed second-brain refs. It is read-only and always exits 0.

To check that second-brain refs still resolve, add `--probe-second-brain`. It reads each one through the configured adapter, so load `.env` first (`set -a; . "$CUSTOMER_HOME/.env"; set +a`).

Fix what it finds in the notes store directly. Fix repo-side findings on a branch and PR in the install repo.

## 6. Opt into dream (optional)

Dream is the nightly pass that keeps steps 3 to 5 true without you: it fixes unambiguous dead links, audits one domain per night, regenerates the indexes, and proposes (never moves) facts that belong in `docs/`, `skills/` or the second brain. It costs one model run most nights.

Prerequisites, setup and the registration block: `chassis/docs/heartbeats/dream.md`. In short: the store is a git repo (step 0), `CLAUDE_AUTO_MEMORY_DIR` is set on a containerized install, `second_brain.mode: adapter` if you want the inbox block written to the second brain, then uncomment the `dream` block in `${CUSTOMER_HOME}/HEARTBEATS.md` (copy it from `chassis/HEARTBEATS.md.template` if your file predates it). Run the gather once by hand and read the payload before the first scheduled night.

## 7. Update `CLAUDE.md`

Your `CLAUDE.md` was rendered once from an older template and is not rewritten by chassis updates. Replace its "Memory System" section and "Every Session" step 1 with the versions in `chassis/CLAUDE.md.template`, adjusted to your install. If you retired the memory MCP, remove the line telling the assistant to search it first; that instruction is what sends a session to an empty store and makes it report "no context" while the answer sits in the second brain.

## Checklist

- [ ] Store located, `CLAUDE_AUTO_MEMORY_DIR` set (container), store under git
- [ ] Memory MCP kept, or retired: `memory.mcp_enabled: false`, `.mcp.json` re-hydrated, no `mcp__memory__*` callers left
- [ ] `MEMORY.md` is the two-level root
- [ ] Every note has `name`, `description`, `type`, `domain`
- [ ] `memory-index-build.py --init` run; `--check` reports no changes
- [ ] `memory-integrity.py` run; store-side findings fixed
- [ ] Dream registered, or deliberately left off
- [ ] `CLAUDE.md` Memory System section updated
