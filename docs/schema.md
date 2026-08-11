# Schema and migrations

The schema is defined entirely by the files in `migrations/`, applied in
lexical order by `gmail-archive migrate` and tracked in a
`schema_migrations` table. There is no ORM and no separate model definition:
the SQL is the source of truth, and each file's header comment carries the
reasoning that led to it.

Migrations are **forward-only**. There are no down migrations, because the
recovery story for this project is a re-ingest from the export rather than a
rollback — the raw bytes are always still on disk, and every derived table can
be rebuilt from them.

## The migrations

| | What it does | Why |
|---|---|---|
| `0001_initial` | `messages`, `blobs`, `labels`, `attachments`, `message_sightings`, `ingest_runs`, `failed_messages` | The core model. Raw bytes on disk, derived metadata in Postgres ([ADR-001](adr/001-blob-store.md)); `messages_keyset_idx` matches the pagination ordering exactly ([ADR-003](adr/003-keyset-pagination.md)). |
| `0002_imap` | `imap_folders`, `imap_uids`, and `envelope` / `bodystructure` columns | Gmail labels are many-to-many, IMAP folders are not: one message appears in several folders with a different permanent UID in each ([ADR-004](adr/004-pymap-imap.md)). |
| `0003_analytics` | `sender_profiles` | Two thirds of the archive is not mail from people. Telling correspondence from notifications needs corpus-wide signals, which cannot be computed per message during ingest. |
| `0004_message_headers` | `message_headers` | Classifying by address shape (`no-reply@`) and Gmail categories has a hole, and it is the same hole: categories thin out across the early years, which is where the human correspondence is. `List-Unsubscribe` does not. |
| `0005_drop_body_html` | drops `messages.body_html` | It was ~1.7 GB, over a quarter of the database, and every byte was a decoded copy of parts of a raw message already on disk. The HTML body is now re-derived from the blob on request. |
| `0006_accounts` | `accounts`, `message_accounts`, rekeys `labels` | The account dimension, added before a second export could be ingested destructively. [ADR-006](adr/006-multi-account-schema.md). |

## Two of these are not migrations in the usual sense

`0005` and `0006` change what `raw_sha256` means or what the schema can
represent, and neither can be applied to a live archive by running SQL alone.

**`0005` breaks older code.** Any release before `0.3.0` queries
`messages.body_html`, so running a `0.2.x` image against a `0.3.0` database
returns 503 on every message detail page. The compose image tag is pinned to
the project version and a test enforces it, because this failure looks healthy
from the outside — the container passes its healthcheck.

**`0006` had to land during a rebuild.** Label rows are written during ingest
from the per-account export they came from, so the account a label belongs to
cannot be recovered afterwards. Adding the dimension later would mean a second
full re-ingest.

Separately, some fixes are not migrations at all. The mboxrd unquoting fix
([ADR-002](adr/002-mboxrd-unquoting.md)) and the mbox separator fix
([#53](https://github.com/evanwtf/gmail-archive/issues/53)) both change the
bytes that get hashed, and therefore change every primary key in the archive.
The only way to apply one is to ingest the export again — see
[Rebuilding the archive from scratch](runbook.md#rebuilding-the-archive-from-scratch),
which does it beside the working archive rather than destroying it.

## Adding one

1. `migrations/000N_short_name.sql`, with a header comment explaining what
   forced it — the files are read far more often than they are written.
2. Idempotent DDL (`if not exists`, `add column if not exists`), so a partial
   apply can be re-run.
3. `uv run gmail-archive migrate` against a scratch database, then again, to
   confirm the second run applies nothing.
4. If it changes what a query reads, check whether the currently published
   image can still run against it. If not, the version bump is a minor, not a
   patch.

The `migrations/` directory is mounted read-only over the copy baked into the
image, so `migrate` applies what is in the working tree rather than whatever
existed when the image was last built.
