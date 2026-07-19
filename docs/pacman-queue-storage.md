# Pacman queue storage - design proposal

**Status:** proposal, not ratified. Nothing in this document is implemented.
**Author:** Jax, 2026-07-19
**Decision owner:** Sean
**Context:** Stage 2 of the second-brain adapter work. PR #58 listed "Pacman
queue to Postgres" as a Stage 2 item; it is the largest piece and it is an
architecture change rather than a wiring change, so it is written up here
instead of being built.

The other four Stage 2 items shipped in the same PR as this document. This one
needs a decision first.

---

## 1. The problem in one paragraph

Pacman stores its work queue as SiYuan blocks, and uses SiYuan block IDs as
queue-entry handles. SiYuan is one of three supported second brains. On a
Notion or Obsidian install - and on ANY install running
`second_brain.mode: adapter`, where the native `mcp__siyuan__*` tools are
deliberately not registered - the queue cannot be read, written, or drained.
Pacman is a chassis-core capability, not a plugin, so "it only works on one
backend" is a defect rather than a limitation.

---

## 2. What the queue actually stores, and what it needs

Reconstructed from the four implementations, not from the docs:

| Script | Operation |
|---|---|
| `pacman-queue-add.py` | enqueue: POST an `h2` block containing the URL under `PACMAN_SIYUAN_QUEUE_BLOCK_ID` |
| `gather-pacman-queue.sh` | depth: `SELECT COUNT(*)` of child blocks whose content matches `%http%` |
| `pacman-drain-prompt.md` | dequeue: oldest-first (`created ASC`), capped at `PACMAN_MAX_BATCH_URLS` |
| `pacman.sh` / `skills/pacman.md` | delete after processing; write proposal doc; write drop record |
| `pacman-process-reactions.py` | enqueue from a Telegram reaction, via `pacman-queue-add.py` |

The functional requirements are modest, and that matters for the choice below:

1. **Append** a URL with a source tag (`telegram-react`, `discord`, `manual`, `heartbeat-drain`).
2. **Count** pending entries cheaply. This runs every 15 minutes on the
   dispatcher tick and must stay free - it is the gate that decides whether to
   spend Claude tokens at all.
3. **Read oldest N**, FIFO, capped.
4. **Delete** an entry after processing, exactly once. The skill calls this
   non-negotiable: an entry that survives processing gets re-processed forever.
5. Tolerate **several URLs per entry** (one pasted message can carry many); the
   entry is deleted only once every URL in it is done.
6. Survive a container restart. The queue is the only record that a URL was
   ever submitted.

What it explicitly does **not** need: full-text search, hierarchy, rich text,
human browsability, or concurrent writers. One producer, one consumer.

That last point is the crux. **The queue is not notes.** It was only ever
stored in the second brain because the second brain was the thing that was
there. Making it backend-neutral by pushing it through the notes adapter would
carry the coupling forward into a new abstraction.

---

## 3. Why block IDs do not port

A SiYuan block ID (`20260718120000-abc1234`) is doing four jobs at once:

1. **Primary key** for the entry.
2. **Creation timestamp**, encoded in the first 14 characters. This is what
   makes `ORDER BY created ASC` free, and it is why FIFO ordering needs no
   extra column.
3. **Deletion handle** - `delete_block(id)` is the dequeue.
4. **User-facing approval token.** `chassis/skills/pacman.md` line 161 matches
   installer replies against `^(approve|reject|defer)\s+(\d{14}-\w{7})...$`.

Neither of the other backends provides anything with those properties:

- **Notion** page ids are 32-char UUIDv4. No embedded timestamp, so ordering
  needs a real property. There is no sub-page-level addressable unit at all -
  a queue of URLs would have to become a database of rows, at which point it is
  a database and not notes.
- **Obsidian** ids are vault-relative file paths. Ordering is filesystem mtime,
  which `docs/second-brain-adapters.md` already documents as unreliable: a git
  pull or an iCloud resync rewrites mtime and would silently reorder the queue.
  Deleting an entry means deleting a file, and a sync conflict resurrects it.

Item 4 is the one that is easy to miss and expensive to get wrong. Even if the
queue moved to Postgres tomorrow, **the approval regex is independently
SiYuan-shaped** and would still reject a Notion or Obsidian proposal id. Any
queue migration must change that regex in the same pass, or approvals break on
exactly the installs the migration was meant to serve. See "Out of scope but
coupled" below.

---

## 4. Options

### Option A - Postgres (`chassis_pacman_queue` table)

Add a `chassis/db/migrations/` table alongside the existing plugin migrations.

**For:**
- Already present and already required. `docker-compose.yml` boots Postgres
  before chassis with `condition: service_healthy`, so the queue cannot be read
  before the DB is up. `psycopg[binary]==3.2.3` is already in
  `requirements.txt` and baked into the image.
- **Backed up for free.** `chassis/scripts/pg-backup.sh` already runs a nightly
  `pg_dump` with GFS retention and S3 upload. A queue in Postgres inherits a
  tested backup and restore path on day one. Neither alternative below gets
  this without new work, and an unbacked-up queue loses in-flight URLs on any
  disk event.
- Genuinely backend-neutral: identical behavior on all three second brains, and
  on an install with no second brain at all.
- FIFO, atomic dequeue, and cheap counts are what the tool is for.
  `SELECT ... FOR UPDATE SKIP LOCKED` makes the dequeue safe if drains ever
  overlap, which the current design cannot express at all.
- Precedent exists: `plugins/dating/db/migrations/001_dating_state.sql` moved
  file-backed state into Postgres for the same reason.

**Against:**
- **Chassis core has no DB module yet.** This is the real cost and it should
  not be waved past. `plugins/dating/scripts/_chassis_db.py` says so in its own
  docstring: "Chassis V1 has no shared DB abstraction yet - each plugin that
  touches a DB carries its own selector until the chassis grows one." Pacman is
  core, not a plugin. Doing this properly means promoting a `chassis-db` module
  to core, which is a bigger change than the queue itself and drags the
  migration-runner question (who applies `chassis/db/migrations/*.sql`, and
  when?) into scope.
- Adds a hard Postgres dependency to a core capability. Today an install with a
  broken Postgres still drains Pacman.
- The queue becomes invisible to the installer. Today Sean can open
  `/To Investigate` and see what is pending. That is a real feature being
  traded away, and it should be replaced by a Discord `pacman queue` command
  rather than silently dropped.

### Option B - SQLite (`$CUSTOMER_HOME/data/pacman-queue.db`)

**For:**
- Zero new infrastructure; `sqlite3` is in the Python stdlib. No dependency on
  the Postgres container being healthy.
- Same ordering and atomic-delete guarantees as Postgres for a single-writer
  workload, which this is.
- A file in the bind-mounted customer-state directory is trivially portable
  between hosts.

**Against:**
- **Splits the state story.** The chassis already decided Postgres is canonical
  ("Sean's 'Postgres from start' call", `docker-compose.yml` line 9). Adding a
  second durable store for one feature means two backup paths, two restore
  procedures, and a standing question of which store new features use.
- Not covered by `pg-backup.sh`. Needs its own backup wiring, or it is a silent
  data-loss hole on restore.
- SQLite over a bind mount is a known-bad combination if the mount is ever
  network-backed. Fine on the Mac Mini today; a foot-gun on a future install.

### Option C - File-backed JSONL directory

One append-only `queue.jsonl`, or one file per entry under
`$CUSTOMER_HOME/state/pacman-queue/`.

**For:**
- Simplest thing that works. Inspectable with `cat`. No dependency at all.
- Matches the existing gather-script contract - several already read and write
  JSON state files.

**Against:**
- Deletion is the whole problem. Append-only JSONL means dequeue is a
  rewrite-the-file operation, which is not atomic against a concurrent append
  and will eventually lose a URL. One-file-per-entry fixes deletion but
  reintroduces exactly the mtime-ordering fragility that disqualifies Obsidian
  in section 3.
- Requirement 4 (delete exactly once) is the one the skill calls
  non-negotiable, and this is the option least able to guarantee it.

### Option D - Keep it in the second brain, via the notes adapter

Store the queue as notes and reach it through `get_adapter().notes`.

**For:**
- No new storage. Preserves installer visibility of the queue.

**Against:**
- The adapter has no delete operation, and adding one to `NotesAdapter` for the
  sake of a queue is the tail wagging the dog.
- Ordering would rest on Obsidian mtime, which section 3 already rules out.
- It encodes "a work queue is a kind of note", which is the original mistake.
  Rejected.

---

## 5. Recommendation

**Option A, Postgres** - with one explicit condition attached.

The reasoning is not primarily "Postgres is already there", because SQLite is
equally already there. It is:

1. **Backups.** `pg-backup.sh` exists, runs nightly, uploads to S3, and has a
   documented restore path. The queue holds URLs that exist nowhere else once
   the source Discord or Telegram message scrolls away. Option B and Option C
   both need new backup wiring to reach parity, and both will get it later
   rather than sooner.
2. **One durable store.** The chassis already made this call. Two stores is a
   decision that pays interest forever.
3. **The workload is exactly relational** - ordered, transactional, deleted
   once. Requirement 4 is free in Postgres and hard-won in Option C.

The condition: **do not ship a per-feature DB selector.** The honest cost of
Option A is that chassis core needs a `chassis-db` module first - the one the
dating plugin's docstring already says is missing. Building Pacman's queue on a
copy-pasted `_chassis_db.py` would make three copies of that file and guarantee
they drift. Sequence it as:

- **A1.** Promote a `chassis/db/` module to core: connection helper, migration
  runner, `chassis/db/migrations/`. Small, and unblocks more than Pacman.
- **A2.** Add `001_pacman_queue.sql` and port the four scripts.
- **A3.** Fix the approval regex (section 6) in the same PR as A2, not later.

If Sean wants Pacman portable before committing to A1, **Option B is the
correct interim** - SQLite behind a narrow `pacman_queue.py` interface with the
storage backend as an implementation detail, so A2 becomes a swap of one module
rather than a rewrite of four scripts. That is a deliberate two-step, not
indecision, and it is worth taking only if the portability is needed sooner
than the DB module.

Sketch, for concreteness:

```sql
CREATE TABLE IF NOT EXISTS chassis_pacman_queue (
    id            BIGSERIAL PRIMARY KEY,
    url           TEXT        NOT NULL,
    source        TEXT        NOT NULL DEFAULT 'manual',
    source_ref    TEXT,                    -- discord/telegram message id, for audit
    entry_group   UUID,                    -- URLs pasted in one message share this;
                                           -- requirement 5 (delete once all are done)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at    TIMESTAMPTZ,             -- set at dequeue; a crashed drain is
                                           -- visible as a stale claim rather than
                                           -- a silently vanished URL
    processed_at  TIMESTAMPTZ,
    verdict       TEXT,                    -- drop | proposal | fetch_failed
    gate          SMALLINT,
    proposal_doc_id TEXT                   -- opaque adapter id, NOT a block id
);

CREATE INDEX IF NOT EXISTS ix_pacman_queue_pending
    ON chassis_pacman_queue (created_at) WHERE processed_at IS NULL;
```

Two notes on that shape. `claimed_at` exists because the current design has no
way to distinguish "being processed" from "pending", so a drain that dies
mid-batch either loses URLs or reprocesses them. `proposal_doc_id` is TEXT and
explicitly opaque - it holds whatever `mcp__secondbrain__create_doc` returned,
which is a block id, a UUID, or a path depending on the install.

---

## 6. Out of scope but coupled - the approval regex

`chassis/skills/pacman.md` matches approvals against
`^(approve|reject|defer)\s+(\d{14}-\w{7})(?:\s+(.+))?$`. That pattern is a
SiYuan block ID and nothing else. A Notion UUID and an Obsidian path both fail
it.

This is independent of where the queue lives, and it will not be caught by
testing the queue migration. Whichever option is chosen, the same PR must
either widen the token to `(\S+)` and validate against stored proposal ids, or
issue short opaque approval tokens (`approve a7f3`) mapped to
`proposal_doc_id` - the second is better, because a Notion UUID is unpleasant
to retype on a phone and Sean approves from his phone.

---

## 7. Migration path for the live queue

Nothing in flight may be lost. Sean's install is the only one with a populated
queue, so this runs once.

1. **Freeze.** `touch $CHASSIS_HOME/PACMAN_HARD_PAUSE`. Both
   `gather-pacman-queue.sh` and `pacman.sh` already honor this and exit 0
   without touching the queue - the mechanism exists and needs no new code.
2. **Snapshot.** Dump the queue to a file before touching anything:
   `SELECT id, content, created FROM blocks WHERE root_id = '<queue-block-id>' AND content LIKE '%http%' ORDER BY created ASC`
   Keep the JSON. It is the rollback.
3. **Backfill.** Insert one row per extracted URL, preserving order:
   `created_at` from the block ID's leading 14 digits (a lossless conversion -
   this is the one time the encoded timestamp is useful), `source` =
   `'siyuan-migration'`, `entry_group` shared across URLs from the same block.
4. **Verify before deleting anything.** Row count must equal the URL count from
   step 2, and the oldest and newest three rows must match by URL. The failure
   this guards against is a block containing two URLs being counted as one
   entry.
5. **Drain once, in dry-run**, and diff the batch it selects against what the
   SiYuan queue would have selected. Same URLs, same order, or stop.
6. **Cut over** the four scripts. Leave the SiYuan `/To Investigate` blocks in
   place - do not delete them in the same step. Disk is cheap; an unrecoverable
   queue is not.
7. **Unfreeze.** Remove `PACMAN_HARD_PAUSE`, watch one real drain end to end.
8. **Clean up** the SiYuan blocks only after a full drain cycle has run clean,
   and only once `PACMAN_SIYUAN_QUEUE_BLOCK_ID` is gone from every script.

Rollback at any point before step 8: re-set the pause flag, revert the scripts,
delete the Postgres rows. The SiYuan queue is still intact because step 6 did
not touch it.

---

## 8. Decisions needed from Sean

1. Option A (Postgres) or Option B (SQLite interim)? The recommendation is A,
   sequenced behind a core `chassis/db/` module.
2. Is promoting `chassis-db` to core in scope now, or does Pacman wait for it?
3. Replace the lost installer visibility with a Discord `pacman queue` command,
   or accept that the queue becomes opaque?
4. Approval tokens: widen the regex, or issue short opaque tokens? (Short
   tokens recommended - phone ergonomics.)
