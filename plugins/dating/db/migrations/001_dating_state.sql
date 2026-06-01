-- 001_dating_state.sql -- Dating V2 state schema for the chassis.
--
-- Ported from <v1-reference-install> PR #522 (V1->V2 migration: file-backed state to Postgres).
-- Chassis adaptation: no installer-specific data baked in.
--
-- Prerequisites:
--   - A running Postgres instance reachable via BEHALFBOT_PG_DSN (or CHASSIS_PG_DSN)
--   - Install bootstrap script has already applied the chassis baseline schema
--
-- Run:
--   psql "$BEHALFBOT_PG_DSN" -f 001_dating_state.sql
--
-- Idempotent via IF NOT EXISTS on all CREATE statements.
--
-- Tables:
--   dating_directives  -- per-match action queue (GO DARK, HALT, RESCHEDULE, etc.)
--   dating_clearances  -- preauth pierces regional video-screen per match

BEGIN;

-- dating_directives
-- Per-match action queue replacing pending-instructions.md / pending-instructions.template.md.
-- The orchestrator INSERTs rows when the installer posts in the social channel;
-- the dating subagent reads open rows at session start and marks each acted_at
-- when it executes the action.

CREATE TABLE IF NOT EXISTS dating_directives (
    id              BIGSERIAL PRIMARY KEY,
    match_name      TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT 'Hinge',
    match_id        TEXT,
    directive       TEXT NOT NULL,
    source_message  TEXT,
    source_channel  TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acted_at        TIMESTAMPTZ,
    acted_outcome   TEXT,
    expires_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_dating_directives_match
    ON dating_directives (match_name, platform, acted_at);

-- dating_clearances
-- Preauth clearances replacing cleared-matches.json / cleared-matches.template.json.
-- The orchestrator INSERTs a row on "Cleared: <Name>" / "Preauth <Name>";
-- "Revoke clearance: <Name>" soft-deletes via revoked_at.
-- The dating subagent reads rows WHERE revoked_at IS NULL at session start.

CREATE TABLE IF NOT EXISTS dating_clearances (
    id                      BIGSERIAL PRIMARY KEY,
    match_name              TEXT NOT NULL,
    platform                TEXT NOT NULL DEFAULT 'Hinge',
    cleared_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cleared_via_message     TEXT,
    channel                 TEXT,
    vetted_basis            TEXT,
    scope_pierced           TEXT NOT NULL DEFAULT 'screening_ladder_only',
    exchange_at_clearance   INTEGER,
    notes                   TEXT,
    revoked_at              TIMESTAMPTZ,
    revoked_via_message     TEXT,
    revoked_reason          TEXT
);

CREATE INDEX IF NOT EXISTS ix_dating_clearances_match
    ON dating_clearances (match_name, platform, revoked_at);

COMMIT;
