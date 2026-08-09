-- 0006_accounts: the multi-account dimension (#39).
--
-- Schema only. There is no account switcher, no `account:` operator and no
-- per-account IMAP yet; this exists now because it is the one part of #39 that
-- cannot be added later without another full rebuild, and #52 is doing a
-- rebuild anyway.
--
-- The urgency is that ingesting a second export today is a **one-way**
-- operation: `raw_sha256` is the primary key, so a message in two accounts is
-- one row, and nothing records that it arrived twice. The merge would be
-- silent and unpickable-apart afterwards.

create table if not exists accounts (
    id           bigint generated always as identity primary key,
    address      text   not null unique,
    display_name text,
    -- For the switcher, so two accounts are distinguishable at a glance the
    -- way Gmail's avatars do it. Null until someone picks one.
    colour       text,
    added_at     timestamptz not null default now()
);

-- Not a column on `messages`. The content-addressed primary key means one row
-- can legitimately belong to several accounts — which is exactly what happens
-- to anything addressed to two of your own addresses — and a column would
-- force a lie in that case.
--
-- Beside `message_sightings` rather than folded into it: a sighting is "this
-- message was at this offset in this file", keyed by source path. An account
-- is not a file. Two exports of the same account are two sightings and one
-- account; one export containing mail for two accounts is the reverse.
create table if not exists message_accounts (
    raw_sha256 char(64) not null references messages (raw_sha256) on delete cascade,
    account_id bigint   not null references accounts (id) on delete cascade,
    primary key (account_id, raw_sha256)
);

create index if not exists message_accounts_message_idx
    on message_accounts (raw_sha256);

-- ── labels gain the account dimension ───────────────────────────────────────
-- The awkward part, and the reason this migration cannot wait. `labels` was
-- keyed `(raw_sha256, label)`, so a message present in two accounts with
-- different labels in each — starred in one, not the other; in the inbox of
-- one and archived in the other — could not be represented at all. That is not
-- an exotic case, it is what happens to every message you send to yourself.
--
-- Adding it after the rebuild would mean a second rebuild, because the label
-- rows are written during ingest from the per-account export they came from.
-- There is no way to recover, later, which account a label belonged to.

-- A default account for everything already here. These rows came from
-- somewhere, and leaving them unattributed would make `account_id` nullable
-- forever — which is the same as not having the dimension.
insert into accounts (address, display_name)
    select 'default', 'Default account'
    where not exists (select 1 from accounts);

alter table labels add column if not exists account_id bigint
    references accounts (id) on delete cascade;

update labels set account_id = (select min(id) from accounts)
    where account_id is null;

alter table labels alter column account_id set not null;

-- Rekey. The old primary key would still collapse two accounts' labels.
alter table labels drop constraint if exists labels_pkey;
alter table labels add primary key (account_id, raw_sha256, label);

-- Existing rows get attributed the same way.
insert into message_accounts (raw_sha256, account_id)
    select m.raw_sha256, (select min(id) from accounts)
    from messages m
    on conflict do nothing;

-- ── which account a run was for ─────────────────────────────────────────────
alter table ingest_runs add column if not exists account_id bigint
    references accounts (id) on delete set null;

update ingest_runs set account_id = (select min(id) from accounts)
    where account_id is null;
