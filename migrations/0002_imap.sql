-- 0002_imap: IMAP folder/UID model and deferred envelope/bodystructure columns.
--
-- Gmail labels are many-to-many; IMAP folders are not. One message appears in
-- several folders with a different, permanent UID in each. UIDs are assigned
-- once, ascend strictly within a folder, and are never reused — clients cache
-- hard enough that violating this looks like data loss.

-- ── IMAP folders ─────────────────────────────────────────────────────────────
-- One row per Gmail label exposed as an IMAP folder. The name is the label as
-- stored in the `labels` table, normalised to IMAP conventions (e.g. INBOX for
-- the special label, hierarchy separator for nested labels).
create table if not exists imap_folders (
    id           bigint      generated always as identity primary key,
    name         text        not null unique,
    uid_validity bigint      not null,
    created_at   timestamptz not null default now()
);

-- ── IMAP UID mapping ──────────────────────────────────────────────────────────
-- Each message gets one UID per folder it appears in. UIDs are assigned
-- monotonically within a folder and never reused, even after a message is
-- removed. This matches IMAP's UID semantics: clients cache UIDs and treat a
-- missing UID as "message deleted", not "new message slot available".
create table if not exists imap_uids (
    folder_id   bigint   not null references imap_folders (id) on delete cascade,
    raw_sha256  char(64) not null references messages (raw_sha256) on delete cascade,
    uid         bigint   not null,
    primary key (folder_id, uid),
    unique (folder_id, raw_sha256)
);

-- Speed up listing all UIDs for a given message (used when a message appears
-- in multiple folders).
create index if not exists imap_uids_sha_idx on imap_uids (raw_sha256);

-- ── Deferred envelope/bodystructure ──────────────────────────────────────────
-- These are computed from the raw RFC822 bytes by pymap's MIME parser and
-- cached here so a live IMAP session does not have to re-parse every message
-- from the blob store on every FETCH. Backfilled by `gmail-archive imap-backfill`.
--
-- Stored as jsonb because the IMAP BODYSTRUCTURE response is a nested data
-- structure that maps naturally to JSON, and Postgres jsonb lets us inspect
-- it for debugging without a custom decoder.
alter table messages
    add column if not exists envelope      jsonb,
    add column if not exists bodystructure jsonb;
