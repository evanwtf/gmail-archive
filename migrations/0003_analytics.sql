-- 0003_analytics: per-sender profiles, so the archive can tell correspondence
-- from notifications.
--
-- Two thirds of this archive is not mail from people: 69% of messages carry a
-- Gmail bulk category and 33% come from a no-reply-style address. Every
-- interesting question about a mail archive — who do I talk to, how has that
-- changed — is drowned by marketing unless the two can be separated.
--
-- The verdict is stored per *sender* rather than per message. A sender's
-- nature does not vary message to message: amazon.com has never sent a
-- human-written word, and deciding that once is both cheaper and more stable
-- than deciding it 16,000 times. It also gives somewhere to record a manual
-- correction, which per-message classification has no room for.
--
-- Rebuilt by `gmail-archive analyze`, not maintained incrementally during
-- ingest: the signals are corpus-wide (has this address ever been replied to?)
-- and cannot be evaluated one message at a time.

create table if not exists sender_profiles (
    address         text        primary key,
    domain          text        not null,

    -- 'human' | 'bulk'. Uncertain cases resolve to 'human' on purpose: a false
    -- 'bulk' hides a real letter from a real person, which is the harmful
    -- direction of error.
    kind            text        not null,

    -- Why the classifier decided what it did, so a wrong answer can be
    -- understood rather than just overruled.
    evidence        text[]      not null default '{}',

    -- Set by hand and never overwritten by a rebuild.
    override        text,

    received_count  bigint      not null default 0,
    sent_to_count   bigint      not null default 0,
    first_seen      timestamptz,
    last_seen       timestamptz,
    computed_at     timestamptz not null default now()
);

-- The listing queries are "top N of a kind, by volume".
create index if not exists sender_profiles_kind_idx
    on sender_profiles (kind, received_count desc);

-- Domain rollups, and the "everything from this company" view.
create index if not exists sender_profiles_domain_idx
    on sender_profiles (domain, received_count desc);

-- "Who have I lost touch with" sorts humans by how long ago they last wrote.
create index if not exists sender_profiles_last_seen_idx
    on sender_profiles (last_seen desc nulls last);
