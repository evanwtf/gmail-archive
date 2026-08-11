# gmail-archive: Agent guide

This file helps AI agents understand the project structure, conventions, and
key files. It is generated from the actual codebase state.

## Project overview

gmail-archive ingests a Google Takeout Gmail mbox export into Postgres for
permanent local archival, search, and export. It provides a web UI (FastAPI +
Jinja2; an HTMX script tag is present but fails SRI and never runs, #14) and a
read-only IMAP server (pymap, currently non-functional — #11).

**Stack:** Python 3.13, uv, psycopg 3.x, FastAPI, pymap, click, pytest,
ruff + mypy --strict, pre-commit.

## Repository structure

```
gmail-archive/
├── src/gmail_archive/          # Package root
│   ├── cli.py                  # Click CLI surface (thin, delegates immediately)
│   ├── config.py               # Frozen Settings dataclass, entirely from env
│   ├── parser.py               # RFC822 parser: bytes → ParsedMessage
│   ├── mbox.py                 # Byte-level mbox splitter (mmap-based)
│   ├── ingest.py               # Resumable ingest pipeline
│   ├── storage.py              # Content-addressed blob store
│   ├── migrate.py              # Numbered .sql migration runner
│   ├── query.py                # Read-only query surface (stats, search, list)
│   ├── searchquery.py          # Search operator grammar (from:, label:, ...)
│   ├── analytics.py            # Sender profiling, correspondents, yearly trends
│   ├── export.py               # Message export (mbox or eml)
│   ├── verify.py               # Integrity verification
│   ├── version.py              # Build metadata
│   ├── logging_setup.py        # Logging configuration
│   ├── fixtures/               # Synthetic mbox fixture generator
│   │   ├── addresses.py        # RFC 2606 address construction
│   │   └── generator.py        # 27 pathologies, measured-rate default mix
│   ├── web/                    # FastAPI web UI
│   │   ├── app.py              # Routes, auth + CSP middleware
│   │   ├── auth.py             # scrypt hashing, HMAC session cookies, throttle
│   │   ├── filters.py          # Jinja filters (presentation only, no I/O)
│   │   ├── templates/          # 16 Jinja2 templates
│   │   └── static/             # CSS, vendored htmx
│   ├── sources/                # Message source protocol
│   │   ├── protocol.py         # MessageSource protocol
│   │   ├── mbox_source.py      # MboxSource adapter
│   │   └── gmail_api_source.py # GmailApiSource (httpx + OAuth2)
│   └── imap/                   # pymap IMAP backend
│       ├── backend.py          # Backend, Login, Identity, Session, Config
│       ├── mailbox.py          # MailboxData, MailboxSet
│       └── message.py          # Message, LoadedMessage
├── migrations/                 # Numbered .sql, forward-only; see docs/schema.md
│   ├── 0001_initial.sql        # Core schema
│   ├── 0002_imap.sql           # IMAP folder/UID model
│   ├── 0003_analytics.sql      # sender_profiles
│   ├── 0004_message_headers.sql# Kept headers, for bulk-vs-human
│   ├── 0005_drop_body_html.sql # Reclaimed ~1.7 GB; re-derived from the blob
│   └── 0006_accounts.sql       # Account dimension; rekeys labels (ADR-006)
├── tests/                      # 22 files: 629 tests — 502 unit, 123 integration, 4 slow
│   ├── conftest.py             # Shared fixtures, scratch_database helper
│   ├── test_parser.py          # Parser + hypothesis property tests
│   ├── test_ingest.py          # Ingest pipeline, ETA, storability invariants
│   ├── test_imap_*.py          # auth, mailbox (incl. e2e over a socket), backfill
│   ├── test_compose_config.py  # Pins the production posture of docker-compose.yml
│   └── ...                     # Integration tests skip without a test DSN
├── docs/
│   ├── plan.md                 # Full project specification
│   ├── progress.md             # Build log with findings
│   ├── getting-started.md      # First run, end to end
│   ├── runbook.md              # Operations guide
│   ├── schema.md               # What each migration did, and why
│   ├── docker-hub.md           # Image build and publish
│   └── adr/                    # Architecture Decision Records
├── Dockerfile                  # Multi-stage build (python:3.13.14-slim-trixie)
├── docker-compose.yml          # web + postgres + init-perms + imap/ingest profiles
└── pyproject.toml              # Project config, entry points, tool settings
```

## Key conventions

### Code style
- **Line length:** 88 (ruff default)
- **Imports:** `from __future__ import annotations` in every file
- **Logging:** `logger = logging.getLogger(__name__)` per module, never print()
- **Types:** mypy --strict, all functions annotated

### Database
- Raw message bytes are on disk (blob store), not in Postgres
- `raw_sha256` is the primary key for messages: the sha256 of the message
  bytes after the mbox `From_` line is stripped (#53) and mboxrd quoting is
  reversed (#10, ADR-002). Both fixes changed every key in the archive, so
  neither could be a migration — see docs/schema.md
- All message fields are best-effort (nullable)
- Keyset pagination: `(internal_date DESC NULLS LAST, raw_sha256 DESC)`
- Migrations are numbered `.sql` files applied by an in-repo runner, and are
  forward-only: recovery is a re-ingest from the export, not a rollback
- `labels` is keyed `(account_id, raw_sha256, label)` since 0006, so lookups
  by hash alone rely on the Postgres 18 skip scan (#60)

### Testing
- 629 tests: 502 unit, 123 integration (skip without DSN), 4 slow (deselected
  by default via `addopts`)
- Integration tests gated on `GMAIL_ARCHIVE_TEST_DATABASE_URL`
- **Two mutually exclusive env shapes.** That variable tells the suite which
  tests to un-skip; `GMAIL_ARCHIVE_DATABASE_URL` is what the app itself reads.
  Setting only the first fails the `TestHtmlRoutesWithDb` tests; setting both
  fails the `TestHtmlRoutesNoDb` tests, which assert the 503. CI runs `pytest
  -q` with neither, then `pytest -m integration -q` with both. A local full run
  showing failures in `tests/test_web.py` is expected, not a regression
- Hypothesis property test: `parse()` never raises for any byte string
- respx for HTTP mocking (no real network in tests)
- `imap/` is covered now (#16): auth, mailbox, and an end-to-end test that
  drives a real server on an ephemeral port with `imaplib`

### Before trusting this code

A full-repo review on 2026-08-06 found defects in ingest, IMAP, export, and the
web UI, several of which contradicted the docstrings and ADRs in the same
files. Most are now fixed, including the two that changed every `raw_sha256`
in the archive (#10 mboxrd unquoting, #53 the mbox separator byte). Read the
table at the end of `docs/progress.md` under "Post-build review" as a **dated
snapshot**, not as current status.

The live list is the GitHub issues. `open`/`closed` is the signal to trust —
this file has carried stale "known defect" claims twice, which is worse than
carrying none.

### Public repository hygiene
- No personal data in fixtures (RFC 2606 domains only)
- No infrastructure identifiers in committed files
- Pre-commit hooks reject mail data, secrets, and large files
- The mail data guard (`scripts/reject_mail_data.py`) checks extension,
  content sniffing, archive magic bytes, and size

## Entry points

- **CLI:** `gmail_archive.cli:main` (registered as `gmail-archive` script)
- **pymap backend:** `gmail_archive.imap:GmailArchiveBackend` (registered in
  `pymap.backend` entry point group)

## Key design decisions

See `docs/adr/` for full ADRs:

1. **Content-addressed blob store** — raw bytes on disk, not in Postgres
2. **mboxrd unquoting** — hash the unquoted RFC822, not the file bytes
3. **Keyset pagination** — `NULLS LAST` for the ~2.7% of messages without dates
4. **pymap for IMAP** — protocol library over hand-rolling
5. **Read-only archive** — no mutation after ingest
6. **Account dimension as a join table** — a message can belong to two
   accounts, so it cannot be a column on `messages`

## Common tasks

```bash
# Run tests
uv run pytest
uv run pytest -m integration  # needs database
uv run pytest -m slow

# Lint and type check
uv run ruff check .
uv run ruff format --check .
uv run mypy

# Generate fixture
uv run gmail-archive gen-fixture /tmp/f.mbox --count 100 --seed 1

# Apply migrations
uv run gmail-archive migrate

# Ingest
uv run gmail-archive ingest /tmp/f.mbox

# Start web UI
uv run gmail-archive serve

# Start IMAP server
GMAIL_ARCHIVE_IMAP_PASSWORD=pass uv run gmail-archive imap

# Backfill IMAP data
uv run gmail-archive imap-backfill
```
