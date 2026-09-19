-- 0001_initial: messages, blobs, labels, attachments, and the run bookkeeping.
--
-- The authoritative definition of the schema. docs/plan.md Phase 4 explains the
-- decisions; this file records the ones that are expressible as constraints,
-- because a rule that lives only in prose is invisible.
--
-- Raw message bytes are NOT here. They live in a content-addressed blob store on
-- disk, so a pg_dump stays a few GB of derived metadata instead of carrying the
-- whole archive.

-- ── migration bookkeeping ────────────────────────────────────────────────────
create table if not exists schema_migrations (
    version     integer     primary key,
    name        text        not null,
    applied_at  timestamptz not null default now()
);

-- ── blobs ───────────────────────────────────────────────────────────────────
-- One row per distinct byte sequence on disk. `sha256` is the file's identity
-- and its path is derived from it, so there is no path column to fall out of
-- sync with the filesystem.
create table if not exists blobs (
    sha256      char(64)    primary key,
    size_bytes  bigint      not null check (size_bytes >= 0),
    kind        text        not null check (kind in ('message', 'attachment')),
    created_at  timestamptz not null default now()
);

-- ── messages ────────────────────────────────────────────────────────────────
-- The primary key IS the content hash of the unquoted RFC822 bytes. That makes
-- idempotency a database constraint (`on conflict do nothing`) rather than
-- pipeline bookkeeping, and it does not assume a Gmail identifier that the mbox
-- export does not actually carry.
--
-- Measured against a real export before this was written:
--   X-GM-THRID   100% of messages      -> thread_id is reliable
--   X-GM-MSGID   0%                    -> gmail_id is null for the whole path
--   Message-ID   0.012% missing, 0.036% duplicated
--   Date         2.67% absent or unparseable -> internal_date is nullable, and
--                that is why the keyset index below needs `nulls last`
create table if not exists messages (
    raw_sha256      char(64)    primary key references blobs (sha256),
    size_bytes      bigint      not null check (size_bytes >= 0),

    -- All best-effort. Nullable is not laziness; each of these is measured to
    -- be absent on real messages.
    message_id      text,
    gmail_id        text,
    thread_id       text,
    subject         text,
    from_addr       text,
    to_addrs        text[]      not null default '{}',
    cc_addrs        text[]      not null default '{}',
    bcc_addrs       text[]      not null default '{}',
    reply_to        text,
    in_reply_to     text,
    references_ids  text[]      not null default '{}',
    internal_date   timestamptz,

    body_text       text,
    body_html       text,
    -- Bounded well under the tsvector 1 MB hard limit by the parser. Stored
    -- rather than recomputed so the generated column below has a stable input.
    search_text     text        not null default '',

    parse_warnings  jsonb       not null default '[]'::jsonb,
    ingested_at     timestamptz not null default now(),

    -- The two-argument form. to_tsvector(text) is STABLE, not IMMUTABLE, because
    -- it reads default_text_search_config — and Postgres refuses a STABLE
    -- function in a generated column. left() bounds each input a second time so
    -- a bug upstream cannot abort a COPY batch of thousands.
    search_tsv tsvector generated always as (
        setweight(to_tsvector('english', left(coalesce(subject, ''), 100000)), 'A')
        ||
        setweight(to_tsvector('english', left(coalesce(search_text, ''), 900000)), 'B')
    ) stored
);

-- Keyset pagination over (internal_date, raw_sha256). `nulls last` is load
-- bearing: internal_date is null for ~2.7% of a real export, and a plain `desc`
-- puts those first, so a keyset walk starting from a real date never reaches
-- them and a walk starting from null falls off the end immediately. query.py
-- must match this ordering exactly.
create index if not exists messages_keyset_idx
    on messages (internal_date desc nulls last, raw_sha256 desc);

create index if not exists messages_search_idx on messages using gin (search_tsv);
create index if not exists messages_thread_idx on messages (thread_id)
    where thread_id is not null;
-- Not unique: 0.036% of a real export duplicates it.
create index if not exists messages_message_id_idx on messages (message_id)
    where message_id is not null;

-- ── labels ──────────────────────────────────────────────────────────────────
-- btree, not GIN. GIN on a scalar text column needs btree_gin, buys nothing
-- over btree for equality, and btree serves "all messages with label X"
-- directly. Labels are many-to-many with messages, hence the join table.
create table if not exists labels (
    raw_sha256  char(64) not null references messages (raw_sha256) on delete cascade,
    label       text     not null,
    primary key (raw_sha256, label)
);

create index if not exists labels_label_idx on labels (label);

-- ── attachments ─────────────────────────────────────────────────────────────
-- filename and mime_type are stored AS DECLARED and never trusted as a
-- filesystem path or for serving. Sanitising here would hide what the archive
-- actually contains; the defence belongs at the serving layer.
--
-- blob_sha256 is nullable on purpose: extracting attachment bytes is a knob.
-- Measured, it adds about a quarter to the store rather than the doubling that
-- motivated the knob, so it defaults on — but a metadata-only row stays legal
-- and `verify` reports it as an explicit state rather than as corruption.
create table if not exists attachments (
    id           bigint      generated always as identity primary key,
    raw_sha256   char(64)    not null references messages (raw_sha256) on delete cascade,
    part_index   integer     not null,
    filename     text,
    mime_type    text        not null,
    size_bytes   bigint      not null check (size_bytes >= 0),
    content_sha256 char(64)  not null,
    blob_sha256  char(64)    references blobs (sha256),
    unique (raw_sha256, part_index)
);

create index if not exists attachments_content_idx on attachments (content_sha256);

-- ── sightings ───────────────────────────────────────────────────────────────
-- Byte-identical duplicates collapse into one `messages` row. Each sighting is
-- recorded so nothing is silently lost and `verify` can reconcile against the
-- source file. Inserts need `on conflict do nothing`: a resume replays the
-- batch whose checkpoint had not advanced.
create table if not exists message_sightings (
    raw_sha256   char(64) not null references messages (raw_sha256) on delete cascade,
    source_path  text     not null,
    byte_offset  bigint   not null check (byte_offset >= 0),
    byte_length  bigint   not null check (byte_length > 0),
    primary key (source_path, byte_offset)
);

create index if not exists message_sightings_sha_idx on message_sightings (raw_sha256);

-- ── run bookkeeping ─────────────────────────────────────────────────────────
create table if not exists ingest_runs (
    id             bigint      generated always as identity primary key,
    source_path    text        not null,
    started_at     timestamptz not null default now(),
    finished_at    timestamptz,
    -- The resume point. Checkpointed in the database rather than in a sidecar
    -- file so it survives the container, not merely the process.
    checkpoint_offset bigint   not null default 0 check (checkpoint_offset >= 0),
    messages_seen  bigint      not null default 0,
    messages_new   bigint      not null default 0,
    failures       bigint      not null default 0,
    status         text        not null default 'running'
                   check (status in ('running', 'complete', 'failed', 'interrupted'))
);

create index if not exists ingest_runs_source_idx on ingest_runs (source_path, started_at desc);

-- Raw bytes kept so a parser fix can replay them, capped so that a handful of
-- 25 MB failures does not undo the "keep pg_dump small" rationale that put
-- message bytes on disk in the first place.
create table if not exists failed_messages (
    id           bigint      generated always as identity primary key,
    run_id       bigint      references ingest_runs (id) on delete set null,
    source_path  text        not null,
    byte_offset  bigint      not null,
    byte_length  bigint      not null,
    error        text        not null,
    traceback    text,
    raw_prefix   bytea,
    truncated    boolean     not null default false,
    created_at   timestamptz not null default now()
);
