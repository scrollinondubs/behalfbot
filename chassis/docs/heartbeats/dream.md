# dream heartbeat

Nightly consolidation of the assistant's auto-memory notes store, one domain
per night on rotation. Opt-in: the chassis ships it commented out in
`HEARTBEATS.md.template`, and nothing turns it on for an existing install.

Issue: scrollinondubs/behalfbot#219. The memory model it maintains:
`chassis/memory/MEMORY_MODEL.md`.

## What it does

- `chassis/scripts/gather-dream.sh` runs every night at no model cost. It runs
  `memory-integrity.py` and the `memory-index-build.py` check, compares the
  counts with what the last run recorded, and emits `{"count": 0}` when there
  is nothing new and the rotation is not due.
- When there is work, the dispatcher runs
  `chassis/scheduled-tasks/dream-prompt.md` with the gather's payload. The
  prompt fixes what is unambiguous inside the notes store, audits one domain
  (at most 25 notes), regenerates the indexes, and commits the store by path.
- Everything else is **proposed, never moved**:
  - facts that belong in the install repo's `docs/` or `skills/` go in a
    `dream/<date>` PR against the install repo's own `origin` (never the
    chassis upstream, never `CLAUDE.md`, never `chassis/`);
  - facts that belong in the second brain go in one block appended to the
    dream inbox doc through the adapter (`mcp__secondbrain__append_to_doc`).
    The inbox is a doc the assistant owns. Dream never edits one of the
    installer's own docs unattended.

## Fires when

| Reason | Means |
|---|---|
| `rotation_due` | `DREAM_MIN_HOURS` (default 20) since the last rotation run, or no run recorded yet |
| `new_dead_links` | More dead note links than the last run recorded |
| `new_orphans` | More orphan notes than the last run recorded |
| `new_bad_second_brain_refs` | More malformed second-brain refs than the last run recorded |
| `index_drift` | An `index-*.md` is out of date, `MEMORY.md` links an index that does not exist, or more notes are unlisted than last time |
| `probe_failed` | A check could not run (store not found, no `MEMORY.md`, a script crashed) |

"More than the last run recorded" is the property that keeps the cost
bounded: a dead link the last run could not fix is in its baseline and does
not re-fire the paid run every night. Only a new one does.

20 hours rather than 24 because the heartbeat is daily and dispatcher jitter
can land tonight's tick a few minutes before last night's. With `rotation_due`
on most nights, expect one run per night at the configured `budget`.

## Prerequisites

1. **The notes store is findable.** The gather resolves it the way
   `chassis/scripts/_memory_store.py` documents: `$CLAUDE_AUTO_MEMORY_DIR`, then
   `~/.claude/projects/<mangled install path>/memory`. Inside the container the
   mangled name comes from the HOST path the install was first opened at, so
   derivation finds nothing there. **On a containerized install set
   `CLAUDE_AUTO_MEMORY_DIR` in `.env`** to the container-side path of the
   bind-mounted store.
2. **The store has the two-level shape:** a `MEMORY.md` root that links one
   `index-<domain>.md` per domain, and notes with `domain:` frontmatter. Run
   the index builder once with `--init` (see the migration note,
   `docs/memory-architecture-migration.md`).
3. **The store is its own git repo** (`git -C "$STORE" init` and one commit).
   Dream commits by path, so every change is reviewable and revertable. If the
   store is unversioned, or sits inside another repo (an older install that
   keeps notes under `$CUSTOMER_HOME/memory` inside the install repo), dream
   still runs but commits nothing: edits stay uncommitted for you to review,
   and deletes and merges become proposals in the report. The gather tells
   the two apart (`store_is_git`, `store_git_toplevel`); a store nested in the
   install repo is never committed to, because that commit would land on
   whatever branch the install repo has checked out.
4. **For the inbox block:** `second_brain.mode: adapter` in
   `chassis.config.yaml`, so the `secondbrain` MCP server is registered. In
   `direct` mode the block goes into the run's stdout report instead.
   Optionally set `memory.dream_inbox_doc` to an existing doc id; when empty,
   the first run creates "<assistant> dream inbox" under the backend's default
   parent and records its id in the dream state file.
5. **For the PR:** the install repo has an `origin` on GitHub that is not
   `scrollinondubs/behalfbot`, and `gh` is authenticated as the assistant's
   account.

## Files

| Path | What |
|---|---|
| `chassis/scripts/gather-dream.sh` | Free gate; always exits 0, failures travel as `probe_failed` |
| `chassis/scheduled-tasks/dream-prompt.md` | The run itself |
| `chassis/scripts/memory-integrity.py` | Read-only pointer report |
| `chassis/scripts/memory-index-build.py` | Index generator (`--check`, `--write`, `--init`) |
| `$CUSTOMER_HOME/scheduled-tasks/dream-state.json` | `lastDreamTime`, `domainCursor`, `domainResume`, `lastIntegrity`, `inboxDocId`, `lastPrUrl`. Dream's own file: the dispatcher owns `heartbeat-state.json`, and a prompt rewriting it would race the dispatcher |
| `$CUSTOMER_HOME/scheduled-tasks/dream.lock` | Run lock |

## Turning it on

Uncomment the block under `## dream` in `${CUSTOMER_HOME}/HEARTBEATS.md`
(copy it from `chassis/HEARTBEATS.md.template` if your file predates it).
Containerized install:

```yaml
schedule: daily 03:00
gather: state/chassis-root/scripts/gather-dream.sh
condition: threshold count > 0
prompt: state/chassis-root/scheduled-tasks/dream-prompt.md
model: sonnet
budget: 1.50
criticality: background
max_fires_per_day: 1
```

A host-side install that runs the dispatcher from launchd has no
`state/chassis-root`; use `${CHASSIS_HOME}/chassis/scripts/gather-dream.sh` and
`chassis/scheduled-tasks/dream-prompt.md` (see "Path conventions" in the
template).

Before the first scheduled run, run the gather by hand and read its payload:

```bash
CUSTOMER_HOME=/app/customer bash state/chassis-root/scripts/gather-dream.sh | jq .
```

## Tests

`bash chassis/scripts/test-dream-gather.sh` covers the gate: silent on a clean
store, fires on a first run, a stale run, a new dead link, an out-of-date index
and a missing store, and stays silent on a dead link the baseline already
holds. The prompt itself is not unit-tested; do one supervised run before
leaving it on.
