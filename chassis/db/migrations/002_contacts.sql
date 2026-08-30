-- 002_contacts.sql - Contacts as a first-class Postgres entity in chassis.
--
-- Full design discussion: scrollinondubs/behalfbot#110. Contact data today has
-- no home - it lands wherever the work happened: an outreach list inside a
-- project note, free-form markdown in the second brain, a name in a Discord
-- message. This table is the fix: one authoritative store in Postgres, and
-- every other surface references a contact by ID instead of copying its
-- fields.
--
-- Slice 1 scope, per the issue thread on 2026-07-22:
--   - Soft delete only, no hard-delete path yet. A deleted contact keeps its
--     row and gets `deleted_at` set, so a reference from a note degrades to
--     "this contact was deleted" instead of erroring or silently resolving to
--     nothing. GDPR erasure is a real requirement but a separate issue.
--   - Email is indexed, not a unique key. Contacts routinely have no email,
--     and a household or company can share one.
--   - Field set is deliberately minimal - name, email, phone, notes. Expand
--     when a real caller needs more, not speculatively; field set was
--     explicitly called out as "not yet scoped" in the issue.

CREATE TABLE IF NOT EXISTS chassis_contacts (
    id          BIGSERIAL   PRIMARY KEY,
    name        TEXT        NOT NULL,
    email       TEXT,
    phone       TEXT,
    notes       TEXT,

    -- Free text, same reasoning as chassis_pacman_queue.source: installs add
    -- their own intake surfaces over time and a CHECK constraint here would
    -- mean a migration per surface. e.g. 'manual', 'discord', 'outreach-import'.
    source      TEXT        NOT NULL DEFAULT 'manual',

    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Soft-delete tombstone. NULL means live. A resolver returns this row
    -- either way, live or tombstoned - the caller sees deleted_at set and
    -- knows the reference is dead, rather than getting an error or a silent
    -- empty result. That is the relational-integrity answer from the issue:
    -- dangling references degrade to a visible tombstone, not a hole.
    deleted_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_contacts_email
    ON chassis_contacts (email)
    WHERE email IS NOT NULL;

-- Search excludes tombstoned contacts by default (the documented default from
-- the issue thread), so the common-path query only ever scans live rows.
CREATE INDEX IF NOT EXISTS ix_contacts_live
    ON chassis_contacts (name)
    WHERE deleted_at IS NULL;
