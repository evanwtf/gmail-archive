-- 0004_message_headers: the headers that settle bulk-versus-human (#34).
--
-- The parser keeps a dozen headers as columns and discards the rest. They
-- survive in the blob, so nothing is lost — but nothing is queryable either,
-- and answering "which of these 277,000 messages came from a machine" by
-- reading 277,000 files is not answering it.
--
-- Today that question is guessed at from address shape (`no-reply@`) and Gmail
-- categories. Both have holes, and they are the same hole: Gmail categories
-- only exist for mail Gmail classified, thinning out across the early years of
-- the archive, which is exactly the period where the human correspondence
-- lives. `List-Unsubscribe` has no such gap. It is also how #44 got the wrong
-- answer — the classifier had nothing better to lean on.
--
-- A table rather than columns, for two reasons. The allowlist will grow, and
-- growing it should be a parser change rather than a migration. And `messages`
-- is already the largest table in the database (#32 is about shrinking it);
-- widening it by seven mostly-null text columns is the wrong direction.

create table if not exists message_headers (
    raw_sha256 char(64) not null references messages (raw_sha256) on delete cascade,
    name       text     not null,
    -- Ordinal within this message for this header name. A header may
    -- legitimately repeat — `Received` chains are the obvious case, and
    -- `List-Unsubscribe` does it too — so the key cannot be (message, name)
    -- alone without silently dropping occurrences.
    seq        smallint not null default 0,
    value      text     not null,
    primary key (raw_sha256, name, seq)
);

-- "Every message with a List-Id" is the shape every analytics query here
-- takes: pick a header name, get the messages. Leading with `name` makes that
-- one index scan. The primary key above already covers the other direction
-- (given a message, get its headers).
create index if not exists message_headers_name_idx
    on message_headers (name, raw_sha256);
