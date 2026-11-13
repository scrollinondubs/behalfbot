-- 003_crm.sql - a small CRM: contacts, lists, and who is on which list.
--
-- A list is anything you track people against: an event guest list, an
-- outreach batch, a cohort. Status, headcount and follow-up dates live on the
-- membership, not the contact, so the next list reuses the same contacts.
--
-- These tables are separate from chassis_contacts (002). That table is the
-- soft-delete, reference-by-id contact store from behalfbot#110; the CRM tab
-- needs hard delete, a whatsapp field and list membership. Folding the two
-- together is an open question, not something this migration decides.
--
-- Read and written by chassis/scripts/_crm.py and the CRM dashboard tab.

CREATE TABLE IF NOT EXISTS crm_contacts (
    id          BIGSERIAL   PRIMARY KEY,
    name        TEXT        NOT NULL CHECK (btrim(name) <> ''),
    email       TEXT,
    phone       TEXT,
    whatsapp    TEXT,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_crm_contacts_name ON crm_contacts (lower(name));

CREATE TABLE IF NOT EXISTS crm_lists (
    id          BIGSERIAL   PRIMARY KEY,
    name        TEXT        NOT NULL CHECK (btrim(name) <> ''),
    event_date  DATE,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS crm_memberships (
    id                BIGSERIAL   PRIMARY KEY,
    contact_id        BIGINT      NOT NULL REFERENCES crm_contacts (id) ON DELETE CASCADE,
    list_id           BIGINT      NOT NULL REFERENCES crm_lists (id) ON DELETE CASCADE,
    status            TEXT        NOT NULL DEFAULT 'invited'
                                  CHECK (status IN ('invited', 'yes', 'maybe', 'no', 'unknown')),
    headcount         INTEGER     NOT NULL DEFAULT 1 CHECK (headcount >= 0),
    -- Free text (email, whatsapp, in person, ...): installs reach people
    -- through different surfaces and a CHECK would mean a migration per one.
    channel           TEXT,
    invited_on        DATE,
    last_followup_on  DATE,
    notes             TEXT,
    -- The line an importer read this membership from, kept verbatim so a
    -- bad parse can be checked against what was actually there.
    raw_source        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (list_id, contact_id)
);

CREATE INDEX IF NOT EXISTS ix_crm_memberships_contact ON crm_memberships (contact_id);
