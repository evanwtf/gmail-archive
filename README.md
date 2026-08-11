# gmail-archive

[![CI](https://github.com/evanwtf/gmail-archive/actions/workflows/ci.yml/badge.svg)](https://github.com/evanwtf/gmail-archive/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/tag/evanwtf/gmail-archive?label=release&sort=semver)](https://github.com/evanwtf/gmail-archive/releases)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue)](https://www.python.org/downloads/)
[![Postgres 18](https://img.shields.io/badge/postgres-18-336791)](https://www.postgresql.org/)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-2a6db2)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Docker image](https://img.shields.io/docker/v/evandhoffman/gmail-archive?label=docker&sort=semver&logo=docker)](https://hub.docker.com/r/evandhoffman/gmail-archive)
[![Image size](https://img.shields.io/docker/image-size/evandhoffman/gmail-archive?sort=semver)](https://hub.docker.com/r/evandhoffman/gmail-archive)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Ingest a Google Takeout Gmail mbox export into Postgres for permanent local
archival, search, and export. Three surfaces over one archive: a
`gmail-archive` CLI, a local FastAPI web UI, and a read-only IMAP server, all
running under Docker Compose alongside a dedicated Postgres. Raw message bytes
live in a content-addressed blob store on disk and only derived metadata and
the search index go in the database, so a `pg_dump` stays small enough to be
useful. Expect a few hundred thousand messages and tens of gigabytes from
twenty years of mail.

## Personal tool, no support, no stability guarantees

This is a personal archival tool published in the open because there's no reason
not to. It is not a product. There is no support, no release process, and no
commitment to backwards compatibility — the schema, the CLI, and the storage
layout may all change without migration paths. If you find it useful, fork it.

It is, however, tested and exercised against a real 277,020-message archive.
See [Known defects](#known-defects) for what is open today — and trust the
issue list over any prose here, including this sentence.

> **New here?** [docs/getting-started.md](docs/getting-started.md) walks the
> whole path, starting where you actually start: requesting the Takeout
> export, which takes hours to days and is the long pole.

## Quick start

Everything runs in Docker, and everything can be exercised against generated
fixture data — you do not need a real mbox export to run this.

```bash
git clone https://github.com/evanwtf/gmail-archive.git
cd gmail-archive
cp .env.example .env                  # then set POSTGRES_PASSWORD
uv run gmail-archive set-password     # then put the hash in .env
docker compose pull                   # published image; no build needed
docker compose up -d
docker compose run --rm web migrate   # apply the schema before first ingest
curl localhost:8000/healthz
```

Images are published to
[`evandhoffman/gmail-archive`](https://hub.docker.com/r/evandhoffman/gmail-archive)
on every release tag. Version tags only — there is deliberately no `latest`,
because the schema changes between versions and nothing migrates it for you.
To build from source instead, `docker compose build web`.

Without `set-password` the UI is served with **no authentication**, and compose
publishes it on all interfaces.

Then ingest something. Either a real export:

```bash
cp /path/to/Takeout/Mail/All.mbox ./data/mbox/
docker compose --profile ingest run --rm ingest /mbox/All.mbox
```

...or generated fixture data, which is what makes the rest of the project
exercisable without real mail:

```bash
uv run gmail-archive gen-fixture /tmp/fixture.mbox --count 500 --seed 1
uv run gmail-archive gen-fixture /tmp/menu.mbox --pathologies list
```

With no `--pathologies` the generator emits a realistic mix at defect rates
measured against a real 20-year export; naming them produces each at least
once. `--seed` is byte-reproducible, and every generated address is confined to
an RFC 2606 reserved domain (`example.com`, `.invalid`, `.test`).

## After an ingest

An ingest writes messages, blobs and labels. Three things it does **not**
write, because each needs a pass over the whole corpus:

```bash
docker compose run --rm web analyze        # sender classification
docker compose run --rm web verify --deep  # integrity
docker compose run --rm web imap-backfill  # IMAP projection
```

On the 277,020-message reference corpus: `analyze` 8s, `verify --deep`
3m 30s, `imap-backfill` the slow one by an order of magnitude. See
[Measured performance](#measured-performance).

| | without it | notes |
|---|---|---|
| `analyze` | `/people` shows "No sender profiles yet" | Signals are corpus-wide ("has this address ever been replied to?"), so they cannot be computed per message during ingest. Manual overrides are preserved. |
| `verify --deep` | nothing — but you have not checked | Re-hashes every blob against its own sha256 filename. Expect zeros across `orphaned_blobs`, `missing_blobs`, `deep_corrupt`, `sighting_mismatch`. |
| `imap-backfill` | IMAP shows folders with no messages | Envelope and bodystructure. The slow one, and resumable, so an interrupt costs only the time already spent. |

`ANALYZE` is **not** on that list: ingest runs it itself before declaring
success, so planner statistics are current the moment it finishes. A manual
`VACUUM` is still worth doing — it builds the visibility map that lets
aggregate queries use index-only scans — but it is an optimisation, not a
prerequisite. See
[docs/runbook.md](docs/runbook.md#after-a-large-ingest-vacuum).

All three are safe to re-run. `imap-backfill` in particular used to number
UIDs by position, so a single new message shifted everything after it and the
re-run collided on `(folder_id, uid)`
([#13](https://github.com/evanwtf/gmail-archive/issues/13)); it now assigns
only to messages that have no UID in that folder, counting up from the
folder's current maximum. Existing UIDs are never touched — clients cache them
hard and read a changed UID as data loss — so a re-run after a second ingest
simply appends.

## Starting over from the export

Re-ingesting is idempotent, so this is only for a genuine clean slate — a
change that rewrites every `raw_sha256`, or a rebuild you want to be sure of.
**If you only want to apply a parser fix without destroying anything, use
[the side-by-side rebuild](docs/runbook.md#rebuilding-the-archive-from-scratch)
instead**; it keeps the working archive intact and makes the cutover one line
of `.env`.

To actually start over:

```bash
docker compose down -v      # -v is the part that matters
sudo rm -rf ./data/* "$GMAIL_ARCHIVE_BLOB_HOST_PATH"/*
docker compose up -d postgres
docker compose run --rm web migrate
docker compose --profile ingest run --rm ingest "/mbox/All.mbox"
```

Three things go wrong here, and they go wrong independently — each one leaves
you believing you started fresh when you did not.

**`docker compose down` does not delete the database.** It removes containers
and the network; the data lives in the `pgdata` volume and survives everything
but `down -v`. Deleting the blob store while the database survives is the worst
of the three outcomes: every row is still there, every body is gone, and
because a missing blob is deliberately not a 404, pages render with headers and
an empty body rather than an error.

**A surviving `ingest_runs` row makes the next ingest a resume, not a restart.**
Runs are looked up by `source_path` and continue from `checkpoint_offset`, so a
stale record silently skips everything below it. The line to read is:

```
resuming at offset 0 (0 messages skipped, 277020 pending)
```

`0 messages skipped` means a genuinely fresh start. Any other number means it
found a checkpoint, and you did not get what you asked for.

**`POSTGRES_DB` is only read when the data directory is first initialised.**
It tracks `GMAIL_ARCHIVE_DB`, so a fresh volume creates the right database —
but changing that variable against an *existing* volume does nothing, and
`migrate` creates schemas, not databases. The symptom is a psycopg
`OperationalError: database "..." does not exist`, several commands after the
mistake, because `pg_isready` reports success for a database that is not there
and Postgres therefore goes healthy. Fix it with a one-off
`create database`, or start from `down -v`.

Finish with the three commands from [After an ingest](#after-an-ingest).

## Usage

### Web UI

`http://localhost:8000`. Compose publishes the port on all interfaces
(`0.0.0.0`), and **the UI has no authentication of its own** — it renders
twenty years of mail to anyone who can reach the port.

| Route | What |
|---|---|
| `/` | Inbox — the Gmail-shaped message list; `?label=`, `?limit=`, `?after_date=`, `?after_sha=` |
| `/messages` | The same list scoped to All Mail |
| `/messages/{sha256}` | Headers, body (HTML in a sandboxed iframe), labels, parse warnings |
| `/messages/{sha256}/raw` | Raw RFC822 source, rendered in the browser |
| `/messages/{sha256}/attachments/{index}` | One attachment, re-extracted from the raw message |
| `/thread/{thread_id}` | All messages in a thread |
| `/search` | Full-text search; `?q=`, `?sort=date\|date-asc\|relevance` (default `date`), `?limit=`, `?offset=` |
| `/labels` | All labels with message counts |
| `/people` | Correspondents, split human vs automated; `?kind=human\|bulk` |
| `/people/{address}` | One correspondent: volume, span, activity by year |
| `/trends` | Activity by year |
| `/stats` | Archive statistics and storage accounting — the dashboard that used to be at `/` |
| `/imports` | Provenance: which exports this archive was built from, and when |
| `/raw/{sha256}` | Raw download, `Content-Disposition: attachment` |
| `/login`, `/logout` | Session forms. `/logout` is POST-only, so a link cannot trigger it |
| `/healthz` | Liveness — deliberately does not touch Postgres |
| `/readyz` | Readiness — real Postgres round-trip; 503 when unavailable |
| `/version` | Build metadata as JSON |
| `/docs` | FastAPI's generated API docs |

Only `/healthz`, `/login`, `/logout` and `/static/` are reachable without a
session. **`/readyz` and `/version` are behind the password**, so an external
monitor has to use `/healthz` — which is why the container healthcheck does.

### CLI

Every command that touches Postgres needs `GMAIL_ARCHIVE_DATABASE_URL` and
aborts if it is unset. The image entrypoint is the CLI itself, so inside Docker
the subcommand is the argument: `docker compose run --rm web stats`.

| Command | Purpose | Key options |
|---|---|---|
| `version` | Print build and runtime identity | |
| `serve` | Run the web UI | `--host` (`0.0.0.0`), `--port` (`8000`) |
| `migrate` | Apply pending schema migrations | |
| `gen-fixture OUT` | Write a synthetic mbox fixture | `--count`, `--seed`, `--pathologies` (`list` prints the menu) |
| `ingest MBOX` | Ingest an mbox file | `--workers` (cpu count), `--batch-size` (1000) |
| `stats` | Print archive statistics as JSON | |
| `search QUERY...` | Full-text search | `--limit` (50), `--offset` (0) |
| `verify` | Reconcile database against blob store | `--deep` re-hashes every blob |
| `export OUTPUT` | Export messages | `--format mbox\|eml`, `--label`, `--query`, `--limit` |
| `labels` | List labels with message counts | |
| `imap` | Start the read-only IMAP server | `--host` (`127.0.0.1`), `--port` (`1143`), `--user`, `--password` |
| `imap-backfill` | Compute envelope/bodystructure, assign UIDs | |

```bash
uv run gmail-archive search "invoice" --limit 10
uv run gmail-archive export /tmp/out.mbox --label Important --limit 100
uv run gmail-archive verify --deep
```

`search`, `stats`, `labels`, `verify` and `version` print JSON to stdout.

### IMAP

Set `GMAIL_ARCHIVE_IMAP_PASSWORD` in `.env`, run the backfill once, then
bring it up on its own profile:

```bash
docker compose run --rm web imap-backfill
docker compose --profile imap up -d imap
# Server: localhost, Port: 1143, Username: archive
```

Or outside Docker: `GMAIL_ARCHIVE_IMAP_PASSWORD=... uv run gmail-archive imap`.

Gmail labels map to IMAP folders — one message appears in several, with a
different permanent UID in each. Published on loopback only, deliberately: one
shared password and no TLS. The server is strictly read-only and answers `NO`
to APPEND, COPY, MOVE, DELETE and flag updates.

Both of the defects that made this unusable are fixed: logins were rejected
even with the right password
([#11](https://github.com/evanwtf/gmail-archive/issues/11)) and Compose had no
way to run it ([#25](https://github.com/evanwtf/gmail-archive/issues/25)). What
remains is [#58](https://github.com/evanwtf/gmail-archive/issues/58) — every
message reports as read.

## Build & run

**Prerequisites:** Docker with Compose for the stack; [uv](https://docs.astral.sh/uv/)
and Python 3.13 for local development. Postgres 18 and the web image are both
provided by Compose.

```bash
uv sync                     # create .venv and install, including dev deps
uv run pre-commit install   # required — see below
uv run gmail-archive --help
```

`uv run pre-commit install` is not optional housekeeping. This repository is
public, and the hooks reject staged `.mbox` files, anything under `blobs/`, and
oversized files. Without them there is nothing between a stray `git add .` and a
permanent public commit of real mail. They also run `ruff`, `mypy --strict` and gitleaks.

CI runs the same checks on every push, plus the integration suite against a
real Postgres — those 41 tests skip silently without a database URL, so
before CI they ran nowhere.

```bash
uv run pytest                 # unit tests; no database needed
uv run pytest -m integration  # needs GMAIL_ARCHIVE_TEST_DATABASE_URL, else skips
uv run pytest -m slow         # large fixture generation, excluded by default
uv run ruff check . && uv run ruff format --check . && uv run mypy
docker compose build web
```

### Configuration

All configuration is environment variables. Copy `.env.example` to `.env` — it
documents every variable and their defaults. The ones you are most likely to
touch:

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | **Required.** Password for the Postgres container |
| `GMAIL_ARCHIVE_DATABASE_URL` | Postgres DSN. Compose builds this from `POSTGRES_PASSWORD`; set it yourself only outside Docker |
| `GMAIL_ARCHIVE_BLOB_HOST_PATH` | Host path for the blob store (default `./data/blobs`) |
| `MBOX_HOST_DIR` | Host directory holding the Takeout mbox, mounted read-only (default `./data/mbox`) |
| `GMAIL_ARCHIVE_WORKERS` / `_BATCH_SIZE` | Ingest tuning; empty means `os.cpu_count()` and `1000` |
| `GMAIL_ARCHIVE_TEST_DATABASE_URL` | Enables the integration tests |

## Architecture

```
Takeout mbox ──> ingest pipeline ──┬──> Postgres    (metadata, search index)
                 (scan, parse,     │
                  hash, COPY)      └──> blob store  (raw RFC822 bytes on disk)
                                            │
                        Web UI (FastAPI) ───┴─── IMAP (pymap)
```

`raw_sha256` — the hash of the message — is both the primary key and the blob's
path on disk, so integrity checking needs no stored checksum: the name *is* the
checksum. Migrations are numbered `.sql` files under `migrations/`, applied by
an in-repo runner and forward-only — see [docs/schema.md](docs/schema.md) for
what each one did and which cannot be applied to a live archive. Decisions are
recorded in [docs/adr/](docs/adr/).

## Measured performance

One real corpus, one machine — an Intel i3-7100 (2 cores, 4 threads) with the
Postgres container on the same box. Numbers from the full rebuild on
2026-08-09, not from a benchmark harness.

| | |
|---|---|
| Corpus | 277,020 messages, 18.9 GB mbox |
| Ingest | **42m 54s** — 108 msg/s, 7.3 MB/s |
| Peak RSS | 7.8 GB (the splitter mmaps the file; most of that is reclaimable page cache) |
| CPU | 61% of one core-equivalent — I/O and Postgres bound, not parse bound |
| `analyze` | 8s for 13,729 senders |
| `verify --deep` | 3m 30s to re-hash all 277,020 blobs |
| Resulting database | 3.87 GB |
| Resulting blob store | 19 GB |

**Treat the ingest figure as a floor.** Part of that run competed with a test
suite hitting the same Postgres, and per-checkpoint throughput swung between
62 and 390 msg/s. An idle machine will do better; the point of the number is
that a 20 GB export is well under an hour, not that it is exactly 108 msg/s.

Throughput is bound by bytes rather than message count — this corpus averages
68 KB a message, so a mailbox of the same size with smaller messages will show
a much higher msg/s and similar MB/s.

## Known defects

The live list is
[the bug label](https://github.com/evanwtf/gmail-archive/issues?q=is%3Aissue+is%3Aopen+label%3Abug),
which is the thing to trust — a hand-maintained table here has gone stale
twice, and a stale defect list is worse than none. What is open today:

- **`parse()` still builds an HTML body nobody reads**
  ([#57](https://github.com/evanwtf/gmail-archive/issues/57)). Wasted work on
  every message, not wrong output — `0005` dropped the column it used to feed,
  and the detail page re-derives the HTML from the blob instead.
- **IMAP reports every message as read, and none as flagged**
  ([#58](https://github.com/evanwtf/gmail-archive/issues/58)). `Seen` is
  applied unconditionally, so Gmail's `Unread` and `Starred` labels — 31,734
  and 57,104 messages on the reference archive — do not reach a mail client.
  Filed as an enhancement, but it is the one open item you will actually
  notice.

Two more things worth knowing that are not defects:

- **`gmail_archive.sources.GmailApiSource` is interface-only.** Nothing calls
  it. It retries 429 and 5xx and refreshes on 401, but does not handle 403 —
  which is how Gmail actually signals rate limiting
  ([#23](https://github.com/evanwtf/gmail-archive/issues/23), closed as out of
  scope rather than fixed). The supported ingest path is the Takeout mbox.
- **Updating the archive means a new Takeout export.** There is no incremental
  sync yet ([#55](https://github.com/evanwtf/gmail-archive/issues/55)).

The [2026-08-06 post-build review](docs/progress.md#post-build-review--2026-08-06)
has the reasoning behind most of these, but read it as a dated snapshot: much
of what it lists has since been fixed.

## Documentation

| Where | What |
|---|---|
| [GitHub issues](https://github.com/evanwtf/gmail-archive/issues) | Live status. `open`/`closed` is a real signal; a hand-edited status line goes stale silently |
| [docs/plan.md](docs/plan.md) | The scoped specification, all ten phases |
| [docs/progress.md](docs/progress.md) | What was built, how to verify it, and findings worth keeping |
| [docs/getting-started.md](docs/getting-started.md) | First run, end to end: Takeout export through to searching |
| [docs/runbook.md](docs/runbook.md) | Operations: ingesting, resuming, verifying, backup and restore, troubleshooting |
| [docs/schema.md](docs/schema.md) | What each migration did and why; which ones cannot be applied to a live archive |
| [docs/docker-hub.md](docs/docker-hub.md) | How the image is built and published |
| [docs/adr/](docs/adr/) | Architecture Decision Records |
| [AGENTS.md](AGENTS.md) | Conventions and repository map for AI agents |

## License

MIT — see [LICENSE](LICENSE).
