# Runbook

Operations guide for the gmail-archive stack.

## Table of contents

- [First-time setup](#first-time-setup)
- [Ingesting mail](#ingesting-mail)
- [Resuming a failed ingest](#resuming-a-failed-ingest)
- [Verifying integrity](#verifying-integrity)
- [Restoring a single message](#restoring-a-single-message)
- [Exporting messages](#exporting-messages)
- [Starting the web UI](#starting-the-web-ui)
- [Starting the IMAP server](#starting-the-imap-server)
- [Backfilling IMAP data](#backfilling-imap-data)
- [Postgres bulk-load settings](#postgres-bulk-load-settings)
- [Rebuilding the archive from scratch](#rebuilding-the-archive-from-scratch)
- [Backup and restore](#backup-and-restore)
- [Troubleshooting](#troubleshooting)

## First-time setup

```bash
# Clone and install
git clone https://github.com/evanwtf/gmail-archive.git
cd gmail-archive
uv sync
uv run pre-commit install

# Configure
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD, GMAIL_ARCHIVE_IMAP_PASSWORD

# Start the stack
docker compose up -d

# Apply the schema
docker compose run --rm web migrate
```

## Ingesting mail

Place your Google Takeout mbox file in the mbox directory and run:

```bash
# Place the mbox file
cp /path/to/Takeout/Mail/All.mbox ./data/mbox/

# Ingest (single-threaded, good for small archives)
docker compose run --rm -v ./data/mbox:/mbox:ro web ingest /mbox/All.mbox

# Ingest (multi-worker, good for large archives)
docker compose run --rm -v ./data/mbox:/mbox:ro web ingest /mbox/All.mbox --workers 4
```

There is also a dedicated `ingest` service behind a compose profile, which
already mounts `$MBOX_HOST_DIR` read-only at `/mbox` and reads
`GMAIL_ARCHIVE_WORKERS` / `GMAIL_ARCHIVE_BATCH_SIZE` from `.env`, so no ad-hoc
`-v` is needed:

```bash
docker compose --profile ingest run --rm ingest /mbox/All.mbox
```

The ingest pipeline is resumable: if it is killed mid-run, re-running the same
command picks up where it left off. Re-ingesting the same file twice adds
nothing via `ON CONFLICT DO NOTHING`.

### After a large ingest: vacuum and analyze

**Do this before judging query performance.** A bulk ingest leaves the planner
working from statistics gathered when the tables were small or empty, and it
will pick bad plans for search — full scans where an index scan was available.
Symptom: searches take seconds, and the same search is fast the second time
but a new term is slow again.

```bash
docker compose exec postgres \
    psql -U gmail_archive -d gmail_archive \
    -c "vacuum (analyze) messages, blobs, labels, attachments"
```

Measured on a 277k-message archive: ~17 seconds to run, and it took search
from multi-second to well under 100ms. Autovacuum gets there eventually, but
"eventually" is after you have already formed an opinion about how slow the
archive is.

The vacuum also builds the visibility map, which is what lets aggregate
queries use index-only scans rather than reading the heap.

### Tuning ingest performance

For the initial import of a large archive, you can temporarily tune Postgres
for bulk loading. See [Postgres bulk-load settings](#postgres-bulk-load-settings).

## Resuming a failed ingest

If the ingest process is killed (container restart, OOM, Ctrl+C):

```bash
# Just re-run the same command — it resumes automatically
docker compose run --rm -v ./data/mbox:/mbox:ro web ingest /mbox/All.mbox
```

The checkpoint lives in the `ingest_runs` table. The pipeline reads the
`checkpoint_offset` of the most recent incomplete run and starts from there.

> **Known defect:** the checkpoint is written from the last result to arrive in
> a batch, not the furthest-along offset, and workers return out of order. A run
> that is interrupted and resumed can skip messages permanently. See
> [#12](https://github.com/evanwtf/gmail-archive/issues/12) — until it is fixed,
> prefer a full re-ingest over a resume for anything you care about.

To force a full re-ingest (e.g. after a parser fix), drop the run record. Reach
Postgres with `psql` in the database container rather than through the app
container — the app image's entrypoint is `python -m gmail_archive`, so
`docker compose run web python ...` is passed to the CLI as a subcommand name
and fails:

```bash
docker compose exec postgres \
    psql -U gmail_archive -d gmail_archive \
    -c "DELETE FROM ingest_runs WHERE status IN ('running', 'interrupted')"
```

Deleting the run makes the next ingest start from offset 0. It does not delete
any message rows: `ON CONFLICT DO NOTHING` means the messages already stored are
recognised as duplicates and only the gap is filled.

To start genuinely from scratch, truncate the tables **and** clear the blob
store — truncating only the database leaves every blob on disk as an orphan:

```bash
docker compose exec postgres \
    psql -U gmail_archive -d gmail_archive \
    -c "TRUNCATE messages, blobs, labels, attachments, message_sightings,
        ingest_runs, failed_messages, imap_folders, imap_uids CASCADE"

rm -rf ./data/blobs/*        # or $GMAIL_ARCHIVE_BLOB_HOST_PATH
```

## Verifying integrity

```bash
# Standard verification (reconciles DB against blob store)
docker compose run --rm web verify

# Deep verification (re-hashes every blob on disk)
docker compose run --rm web verify --deep
```

The verify command reports:
- **Messages in DB**: total message rows
- **Blobs in DB**: total blob rows
- **Blobs on disk**: total blob files found
- **Orphaned blobs**: files on disk with no DB row (harmless, can be cleaned up)
- **Missing blobs**: DB rows with no file on disk (data loss)
- **Deep corrupt**: files whose content hash doesn't match their filename
- **Sighting mismatches**: messages whose source file offset disagrees with
  the sightings table

## Restoring a single message

To extract a single message by its content hash:

```bash
# Find the hash
docker compose run --rm web search "some query"

# Export as .eml. For --format eml the output path is a DIRECTORY (one
# <sha256>.eml per message), and it must be a mounted volume — anything written
# elsewhere in the container disappears with --rm.
mkdir -p ./data/export
docker compose run --rm -v ./data/export:/export web \
    export /export --format eml --query "some query" --limit 1
```

Or directly from the blob store:

```bash
# The blob path is data/blobs/{sha256[:2]}/{sha256}
cp data/blobs/ab/cdef1234... /tmp/message.eml
```

## Exporting messages

The output path must land on a mounted volume. A path like `/tmp/export.mbox`
is inside the container and is destroyed by `--rm` the moment the command ends.

```bash
mkdir -p ./data/export

# Export all messages as mbox
docker compose run --rm -v ./data/export:/export web export /export/all.mbox

# Export messages with a specific label
docker compose run --rm -v ./data/export:/export web \
    export /export/labeled.mbox --label "Important"

# Export matching a search query
docker compose run --rm -v ./data/export:/export web \
    export /export/search.mbox --query "hello world"

# Export as individual .eml files (the path is a directory in this mode)
docker compose run --rm -v ./data/export:/export web \
    export /export/eml --format eml --limit 100
```

> **Known defect:** mbox export currently double-quotes `From ` lines, because
> ingest stores mbox-quoted bytes while the exporter assumes unquoted ones. See
> [#10](https://github.com/evanwtf/gmail-archive/issues/10) and
> [#18](https://github.com/evanwtf/gmail-archive/issues/18). `.eml` export is
> affected by the same root cause. Exports are readable but do not round-trip
> byte-identically yet.

## Starting the web UI

```bash
# The web UI runs by default with docker compose up -d
# Access at http://localhost:8000

# Or run directly (without Docker)
gmail-archive serve --host 127.0.0.1 --port 8000
```

## Starting the IMAP server

> **The IMAP server does not work today.** Every login is rejected, including
> one with the configured password —
> [#11](https://github.com/evanwtf/gmail-archive/issues/11). Compose also has no
> way to run it: the password is never passed into the container and no port is
> published — [#25](https://github.com/evanwtf/gmail-archive/issues/25). The
> commands below are the intended interface, not a working procedure.

```bash
# Directly, outside Docker
GMAIL_ARCHIVE_IMAP_PASSWORD=yourpassword gmail-archive imap

# With a custom user
gmail-archive imap --user myuser --password mypass

# Connect with any IMAP client
#   Server: localhost
#   Port: 1143
#   Encryption: none (STARTTLS not configured by default)
#   Username: archive (or --user value)
#   Password: as configured
```

**Note:** the server binds 127.0.0.1 by default. `--host 0.0.0.0` exposes it to
the network — which, with one shared password and no TLS, should be a
deliberate choice rather than the way you get it working inside a container.

## Backfilling IMAP data

After the initial ingest, run the IMAP backfill to compute envelope and
bodystructure for all messages and assign UIDs per folder:

```bash
docker compose run --rm web imap-backfill
```

It reads every message from the blob store, parses it with pymap's MIME parser,
and stores the results in the database. Subsequent IMAP FETCH responses use the
cached data.

> **Do not re-run this after a second ingest.** The envelope/bodystructure half
> is safe to repeat (it skips rows that already have both), but the UID half
> assigns UIDs by position and collides with the `(folder_id, uid)` primary key
> as soon as the message set has changed, aborting partway through with earlier
> folders already committed. See
> [#13](https://github.com/evanwtf/gmail-archive/issues/13).

## Postgres bulk-load settings

For the initial import of a large archive, you can temporarily tune Postgres
for bulk loading. These settings reduce WAL volume and checkpoint frequency:

```sql
-- Apply before the initial import
ALTER SYSTEM SET wal_level = 'minimal';
ALTER SYSTEM SET max_wal_senders = 0;
ALTER SYSTEM SET checkpoint_timeout = '1h';
ALTER SYSTEM SET maintenance_work_mem = '2GB';

-- Reload configuration
SELECT pg_reload_conf();

-- After the import, revert to safe defaults
ALTER SYSTEM SET wal_level = 'replica';
ALTER SYSTEM SET max_wal_senders = 10;
ALTER SYSTEM SET checkpoint_timeout = '5min';
ALTER SYSTEM SET maintenance_work_mem = '64MB';
SELECT pg_reload_conf();
```

**Warning:** `wal_level = 'minimal'` means no point-in-time recovery. Revert
immediately after the import.

## Rebuilding the archive from scratch

Some changes rewrite every `raw_sha256` — the mboxrd unquoting fix and the mbox
separator fix both did. Those are not migrations. The only way to apply them is
to ingest the export again, and the point of this section is that **you never
have to destroy the working archive to do it.**

The whole procedure is: build a second archive beside the first, verify it,
then switch a single line of `.env`. That line is also the undo.

### Before you start

- **The original export.** Not a fresh one — a new Takeout is a different file,
  with mail added and deleted since. Confirm it is the same one:
  `select max(byte_offset + byte_length) from message_sightings` should equal
  the file's size in bytes exactly.
- **Room for a second blob store.** It is the size of the export. Check the
  filesystem you are putting it on, not `/` in general.
- **Not `/tmp`.** On many systems that is a RAM disk of a few GB.

### The procedure

```bash
# 1. A dump of the current database, purely as insurance — nothing here
#    modifies it.
docker compose exec -T postgres pg_dump -U gmail_archive -d gmail_archive -Fc \
  > ~/gmail-archive-pre-rebuild.dump

# 2. A fresh database and a fresh blob store, both beside the originals.
docker compose exec -T postgres psql -U gmail_archive -d postgres \
  -c "create database gmail_archive_v2"

export GMAIL_ARCHIVE_DATABASE_URL=".../gmail_archive_v2"
export GMAIL_ARCHIVE_BLOB_DIR=/path/to/blobs-v2
uv run gmail-archive migrate

# 3. Ingest. Time it — this is the only place real throughput is observable.
uv run gmail-archive ingest "/path/to/All mail Including Spam and Trash.mbox"

# 4. Classify senders, then check the result end to end.
uv run gmail-archive analyze
uv run gmail-archive verify --deep
```

`verify --deep` re-hashes every blob against its filename, which is the check
that matters here: it proves the new store is internally consistent under the
new hashing, independent of the old one.

### Cutting over

Only after verify passes. Two lines of `.env`, then restart:

```
GMAIL_ARCHIVE_BLOB_HOST_PATH=/path/to/blobs-v2
GMAIL_ARCHIVE_DB=gmail_archive_v2
```

```bash
docker compose up -d web
```

**If you ran the ingest outside the container, fix ownership first.** The
container runs as uid 65532 and the blobs will be owned by you, mode 0600, so
it cannot read a single one — and the failure is quiet: pages render with
headers and no body, because a missing blob is deliberately not a 404.

```bash
sudo chown -R 65532:65532 /path/to/blobs-v2
```

Verify the container really moved, rather than trusting the restart:

```bash
docker compose exec web env | grep -o "5432/[a-z_0-9]*"
```

Keep the old database and blob store until you have used the new archive in
anger. Reverting is those same two lines changed back — nothing was destroyed,
so there is nothing to restore. Once you are satisfied, drop the old database
and `rm -rf` the old store; because the two stores never shared a hash, there
is no risk of deleting something the new one points at.

### What to expect

Message counts should match, or differ only by duplicates collapsing
differently — a hashing change can merge two rows that previously differed by a
framing byte. A large discrepancy is a bug, not a rounding difference.

## Backup and restore

The database and blob store must be backed up together to produce a consistent
snapshot.

### Database backup

```bash
# Dump the metadata (small, can run daily)
docker compose exec postgres pg_dump -U gmail_archive gmail_archive > archive_$(date +%F).sql

# Restore
docker compose exec -T postgres psql -U gmail_archive gmail_archive < archive.sql
```

### Blob store backup

The blob store is at `./data/blobs/` (or whatever `GMAIL_ARCHIVE_BLOB_HOST_PATH`
points to). Back it up with your filesystem tool of choice:

```bash
# rsync to a backup location
rsync -av ./data/blobs/ /backup/gmail-archive/blobs/

# Or create a tarball
tar caf archive-blobs-$(date +%F).tar.gz ./data/blobs/
```

### Full restore procedure

```bash
# 1. Restore the blob store first
rsync -av /backup/gmail-archive/blobs/ ./data/blobs/

# 2. Start the stack
docker compose up -d

# 3. Restore the database
docker compose exec -T postgres psql -U gmail_archive gmail_archive < archive.sql

# 4. Verify integrity
docker compose run --rm web verify
```

## Troubleshooting

### "GMAIL_ARCHIVE_DATABASE_URL is not set"

The database URL is required by every command that touches Postgres.

Inside Docker it is **not** read from `.env` directly — `docker-compose.yml`
builds it from `POSTGRES_PASSWORD` and injects it into the container. If you see
this error from a compose command, `POSTGRES_PASSWORD` is unset or empty.

Outside Docker, export it yourself, pointing at whatever host and port Postgres
is reachable on (`localhost:5432` needs the `ports:` block in
`docker-compose.yml` uncommented):

```bash
export GMAIL_ARCHIVE_DATABASE_URL=postgresql://gmail_archive:password@localhost:5432/gmail_archive
```

### Container exits immediately

Check the logs:

```bash
docker compose logs web
docker compose logs postgres
```

### "no migrations directory"

The Dockerfile must copy the `migrations/` directory. Rebuild:

```bash
docker compose build web
```

### IMAP connection refused

Ensure the IMAP server is running and the port is accessible:

```bash
# Check if the server is listening
netstat -tlnp | grep 1143

# Try connecting
openssl s_client -connect localhost:1143 -starttls imap
# or
nc -C localhost 1143
```

### Slow IMAP FETCH responses

Ensure the backfill has been run:

```bash
gmail-archive imap-backfill
```

Without backfill, every FETCH response requires reading the raw message from
the blob store and parsing it with pymap's MIME parser.
