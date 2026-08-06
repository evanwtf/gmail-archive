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

The ingest pipeline is resumable: if it is killed mid-run, re-running the same
command picks up where it left off. Re-ingesting the same file twice adds
nothing via `ON CONFLICT DO NOTHING`.

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

To force a full re-ingest (e.g. after a parser fix), you can delete the run:

```bash
docker compose run --rm web python -c "
import psycopg
conn = psycopg.connect('host=postgres dbname=gmail_archive user=gmail_archive password=...')
conn.execute('DELETE FROM ingest_runs WHERE status = \$1', ['interrupted'])
conn.commit()
"
```

Or simply truncate and re-ingest:

```bash
docker compose run --rm web python -c "
import psycopg
conn = psycopg.connect('host=postgres dbname=gmail_archive user=gmail_archive password=...')
conn.execute('TRUNCATE messages, blobs, labels, attachments, message_sightings, ingest_runs, failed_messages CASCADE')
conn.commit()
"
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

# Export as .eml
docker compose run --rm web export /tmp/restore.eml --format eml --query "some query" --limit 1
```

Or directly from the blob store:

```bash
# The blob path is data/blobs/{sha256[:2]}/{sha256}
cp data/blobs/ab/cdef1234... /tmp/message.eml
```

## Exporting messages

```bash
# Export all messages as mbox
docker compose run --rm web export /tmp/export.mbox

# Export messages with a specific label
docker compose run --rm web export /tmp/labeled.mbox --label "Important"

# Export matching a search query
docker compose run --rm web export /tmp/search.mbox --query "hello world"

# Export as individual .eml files
docker compose run --rm web export /tmp/eml-dir --format eml --limit 100
```

## Starting the web UI

```bash
# The web UI runs by default with docker compose up -d
# Access at http://localhost:8000

# Or run directly (without Docker)
gmail-archive serve --host 127.0.0.1 --port 8000
```

## Starting the IMAP server

```bash
# With Docker (requires GMAIL_ARCHIVE_IMAP_PASSWORD in .env)
docker compose run --rm -p 1143:1143 web gmail-archive imap

# Or run directly
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

**Note:** The IMAP server binds to 127.0.0.1 by default. Use `--host 0.0.0.0`
to expose it to other machines on the network.

## Backfilling IMAP data

After the initial ingest, run the IMAP backfill to compute envelope and
bodystructure for all messages and assign UIDs per folder:

```bash
docker compose run --rm web gmail-archive imap-backfill
```

This is a one-time operation. It reads every message from the blob store,
parses it with pymap's MIME parser, and stores the results in the database.
Subsequent IMAP FETCH responses use the cached data.

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

The database URL is required by most commands. Set it in `.env`:

```
GMAIL_ARCHIVE_DATABASE_URL=postgres://gmail_archive:password@postgres:5432/gmail_archive
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
