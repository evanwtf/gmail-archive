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
│   ├── export.py               # Message export (mbox or eml)
│   ├── verify.py               # Integrity verification
│   ├── version.py              # Build metadata
│   ├── logging_setup.py        # Logging configuration
│   ├── fixtures/               # Synthetic mbox fixture generator
│   │   ├── addresses.py        # RFC 2606 address construction
│   │   └── generator.py        # 26 pathologies, measured-rate default mix
│   ├── web/                    # FastAPI web UI
│   │   ├── app.py              # Routes, CSP middleware
│   │   ├── templates/          # 8 Jinja2 templates
│   │   └── static/             # CSS
│   ├── sources/                # Message source protocol
│   │   ├── protocol.py         # MessageSource protocol
│   │   ├── mbox_source.py      # MboxSource adapter
│   │   └── gmail_api_source.py # GmailApiSource (httpx + OAuth2)
│   └── imap/                   # pymap IMAP backend
│       ├── backend.py          # Backend, Login, Identity, Session, Config
│       ├── mailbox.py          # MailboxData, MailboxSet
│       └── message.py          # Message, LoadedMessage
├── migrations/                 # Numbered .sql migrations
│   ├── 0001_initial.sql        # Core schema (8 tables)
│   └── 0002_imap.sql           # IMAP folder/UID model
├── tests/                      # 14 test files: 208 unit, 41 integration, 1 slow
│   ├── conftest.py             # Shared fixtures
│   ├── test_parser.py          # Parser + hypothesis property tests (44)
│   ├── test_ingest.py          # Ingest pipeline tests (10)
│   ├── test_sources.py         # Message source tests (30, respx mocks)
│   └── ...                     # NOTE: nothing covers imap/ — see issue #16
├── docs/
│   ├── plan.md                 # Full project specification
│   ├── progress.md             # Build log with findings
│   ├── runbook.md              # Operations guide
│   └── adr/                    # Architecture Decision Records
├── Dockerfile                  # Multi-stage build (python:3.13.14-slim-trixie)
├── docker-compose.yml          # web + postgres + init-perms + ingest profile
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
- `raw_sha256` is the primary key for messages (hash of unquoted RFC822 —
  intended, but ingest does not actually unquote today: #10)
- All message fields are best-effort (nullable)
- Keyset pagination: `(internal_date DESC NULLS LAST, raw_sha256 DESC)`
- Migrations are numbered `.sql` files applied by an in-repo runner

### Testing
- 208 unit tests, 41 integration tests (skip without DSN), 1 slow test
- Integration tests gated on `GMAIL_ARCHIVE_TEST_DATABASE_URL`
- Hypothesis property test: `parse()` never raises for any byte string
- respx for HTTP mocking (no real network in tests)
- **`src/gmail_archive/imap/` has no tests at all.** A green `pytest` run says
  nothing about the IMAP server, and the server does not currently work. Do not
  read the suite as evidence about that package (#16)

### Before trusting this code

A full-repo review on 2026-08-06 found defects in ingest, IMAP, export, and the
web UI, several of which contradict the docstrings and ADRs in the same files.
The findings and their issue numbers are tabulated at the end of
`docs/progress.md` under "Post-build review". Read that table before changing
`ingest.py`, `export.py`, or anything under `imap/` — in particular, ingest does
**not** currently unquote mboxrd despite ADR-002 and every docstring saying it
does (#10).

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
   (decided, not implemented — see #10)
3. **Keyset pagination** — `NULLS LAST` for the ~2.7% of messages without dates
4. **pymap for IMAP** — protocol library over hand-rolling
5. **Read-only archive** — no mutation after ingest

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
