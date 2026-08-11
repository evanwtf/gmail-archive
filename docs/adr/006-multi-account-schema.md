# ADR-006: The account dimension is a join table, not a column

**Status:** Accepted (schema only — migration `0006_accounts.sql`)

**Context:** The archive was built for one Gmail account, and `raw_sha256` is
the primary key of `messages` — a message is identified by its bytes and
nothing else. Adding a second account to that schema is a **one-way**
operation: a message present in both accounts collapses into one row, and
nothing records that it arrived twice. The merge is silent and cannot be
picked apart afterwards.

Worse, `labels` was keyed `(raw_sha256, label)`. A message starred in one
account and not the other, or in the inbox of one and archived in the other,
could not be represented at all. That is not an exotic case — it is what
happens to every message you send to yourself.

Neither problem can be fixed after the fact. The label rows are written during
ingest from the per-account export they came from, and there is no way to
recover later which account a label belonged to. So the schema had to change
before a second export was ever ingested, even though the UI for it
([#39](https://github.com/evanwtf/gmail-archive/issues/39),
[#54](https://github.com/evanwtf/gmail-archive/issues/54)) does not exist yet.
It shipped inside the `#52` rebuild because that rebuild was already paying
the cost of re-ingesting everything.

**Decision:**

- **`accounts`** — one row per Gmail address, created on first use by
  `ingest --account`.
- **`message_accounts`** — a join table, *not* a column on `messages`. The
  content-addressed primary key means one row can legitimately belong to
  several accounts, which is exactly what happens to anything addressed to two
  of your own addresses. A column would force a lie in that case.
- It sits beside `message_sightings` rather than folding into it. A sighting
  is "this message was at this offset in this file", keyed by source path. An
  account is not a file: two exports of one account are two sightings and one
  account; one export containing mail for two accounts is the reverse.
- **`labels` is rekeyed** to `(account_id, raw_sha256, label)`, so per-account
  label state is representable.
- **`ingest_runs.account_id`** records which account a run was for.
- Existing rows are attributed to a `default` account created by the
  migration. Leaving them unattributed would make `account_id` nullable
  forever, which is the same as not having the dimension.

**Consequences:**

- Ingesting a second export is no longer destructive, and the archive can say
  which account a message and each of its labels came from.
- **The UI half does not exist.** There is no account switcher, no `account:`
  search operator, and no per-account IMAP. Everything lands in the default
  account unless `--account` is passed. That is
  [#54](https://github.com/evanwtf/gmail-archive/issues/54).
- Deduplication across exports still happens at the byte level, so two exports
  of the *same* account collapse correctly — but a message whose labels
  changed between exports hashes differently, because `X-Gmail-Labels` is
  inside the hashed bytes. Merging two Takeout exports of one account
  therefore duplicates every message whose labels moved.
- The rekey made `raw_sha256` a non-leading column of the `labels` primary
  key, so lookups by hash alone now depend on the Postgres 18 skip scan.
  That is [#60](https://github.com/evanwtf/gmail-archive/issues/60), and it is
  the reason the compose file pins the major version.
