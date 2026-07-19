-- 001_pacman_queue.sql - Pacman's work queue, moved out of SiYuan.
--
-- Why this table exists: Pacman stored its queue as SiYuan blocks and used
-- SiYuan block IDs as queue handles. SiYuan is one of three supported second
-- brains, so Pacman was broken on Notion and Obsidian installs, and broken on
-- ANY install running second_brain.mode: adapter (where mcp__siyuan__* is
-- deliberately not registered). PR #78 made that fail loudly; this is the fix.
-- Full reasoning: docs/pacman-queue-storage.md.
--
-- One row per URL, not per submitted message. A pasted message carrying four
-- URLs becomes four rows sharing an entry_group, because the 4-gate pipeline
-- runs per URL and each URL gets its own verdict.

CREATE TABLE IF NOT EXISTS chassis_pacman_queue (
    id               BIGSERIAL   PRIMARY KEY,

    -- Short opaque approval token. This replaces the SiYuan block ID as the
    -- thing the installer types in Discord ("approve qhtnbz"). Generated at
    -- enqueue so a row has one stable handle for its whole life. Alphabet is
    -- vowel-free lowercase letters with no digits - see chassis/pacman/tokens.py
    -- for why that alphabet and not a UUID.
    token            TEXT        NOT NULL UNIQUE,

    url              TEXT        NOT NULL,

    -- 'manual' | 'discord' | 'telegram-react-<chat>' | 'heartbeat-drain' |
    -- 'siyuan-migration'. Free text rather than an enum: sources are added by
    -- installs, and a CHECK constraint here would mean a migration every time
    -- someone wires a new intake.
    source           TEXT        NOT NULL DEFAULT 'manual',

    -- Discord/Telegram message id the URL came from, for audit. Nullable
    -- because manual CLI submissions have no message behind them.
    source_ref       TEXT,

    -- URLs submitted together share this. Requirement 5 in the design note:
    -- an entry is only fully done once every URL in it is done.
    entry_group      UUID        NOT NULL,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Set at dequeue. A drain that dies mid-batch leaves a stale claim, which
    -- is visible and reclaimable, rather than a URL that silently vanished.
    -- The old design could not express "being processed" at all.
    claimed_at       TIMESTAMPTZ,

    processed_at     TIMESTAMPTZ,
    verdict          TEXT,       -- drop | proposal | fetch_failed
    gate             SMALLINT,   -- gate number that dropped it, NULL on proposals

    -- Whatever mcp__secondbrain__create_doc returned: a SiYuan block id, a
    -- Notion UUID, or an Obsidian vault path depending on the install.
    -- Deliberately opaque TEXT - core code never parses this.
    proposal_doc_id  TEXT,

    -- Set only by the one-time SiYuan backfill. Doubles as the idempotency key
    -- so re-running the migration cannot duplicate rows.
    legacy_block_id  TEXT
);

-- The gather script runs this predicate every dispatcher tick and must stay
-- cheap - it is the gate that decides whether to spend Claude tokens at all.
CREATE INDEX IF NOT EXISTS ix_pacman_queue_pending
    ON chassis_pacman_queue (created_at)
    WHERE processed_at IS NULL;

-- Migration idempotency. A SiYuan block holding two URLs becomes two rows, so
-- the key is (block, url) and not the block alone. Partial index because rows
-- created normally have no legacy block and must not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS ux_pacman_queue_legacy_block_url
    ON chassis_pacman_queue (legacy_block_id, url)
    WHERE legacy_block_id IS NOT NULL;
