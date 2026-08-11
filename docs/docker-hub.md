# gmail-archive

**Turn a Google Takeout Gmail export into a searchable local archive you own.**

You give it the `.mbox` file Google hands you; it gives you back your mail as a
Gmail-shaped web app that runs entirely on your own machine — inbox, labels,
full-text search with `from:` and `before:` operators, attachments, and a
read-only IMAP server you can point a real mail client at. Nothing leaves the
box, and no account is involved after the export.

Built for roughly twenty years of mail: a few hundred thousand messages and
tens of gigabytes.

[![Source on GitHub](https://img.shields.io/badge/source-github.com%2Fevanwtf%2Fgmail--archive-181717?logo=github)](https://github.com/evanwtf/gmail-archive)
[![CI](https://github.com/evanwtf/gmail-archive/actions/workflows/ci.yml/badge.svg)](https://github.com/evanwtf/gmail-archive/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/evanwtf/gmail-archive/blob/main/LICENSE)

| | |
|---|---|
| **Source, issues, docs** | https://github.com/evanwtf/gmail-archive |
| **Getting started** | [docs/getting-started.md](https://github.com/evanwtf/gmail-archive/blob/main/docs/getting-started.md) |
| **Compose file you need** | [docker-compose.yml](https://github.com/evanwtf/gmail-archive/blob/main/docker-compose.yml) |
| **Operations guide** | [docs/runbook.md](https://github.com/evanwtf/gmail-archive/blob/main/docs/runbook.md) |
| **Known defects** | [README](https://github.com/evanwtf/gmail-archive#known-defects) |

## This image is not standalone

It needs a Postgres database and a writable volume for the blob store. Raw
message bytes live on disk in a content-addressed store, not in the database,
so the database stays small enough that `pg_dump` is worth running — but that
means the container needs both, and neither is bundled.

Use the compose file from the repository:

```bash
git clone https://github.com/evanwtf/gmail-archive.git
cd gmail-archive
cp .env.example .env                  # set POSTGRES_PASSWORD
uv run gmail-archive set-password     # put the printed hash in .env
docker compose pull
docker compose up -d
docker compose run --rm web migrate
```

Then put your Takeout mbox in `./data/mbox/` and:

```bash
docker compose --profile ingest run --rm ingest /mbox/All.mbox
docker compose run --rm web analyze   # classify senders; People and Trends need this
```

The UI is at `http://localhost:8000`.

## Read this before exposing it

**With no password configured the UI is served with no authentication at all**,
and the compose file publishes it on every interface. Anyone who can reach the
port can read the whole archive. Run `gmail-archive set-password` and put the
hash in `.env`.

The server speaks plain HTTP, so the password crosses the wire in the clear.
On a home network that is a real improvement over nothing; put it behind a
TLS-terminating proxy if it needs to be more than that.

## Tags

Every release publishes an exact tag and a moving minor tag — `0.4.0` and
`0.4`. **There is deliberately no `latest`.** The database schema changes
between versions and nothing migrates it for you, so a floating tag would
invite an upgrade across a breaking change by accident. Pin a version and read
the release notes before moving.

That is not hypothetical. `0.3.0` dropped `messages.body_html`, so a `0.2.x`
image against a `0.3.0` database returns 503 on every message detail page —
while still passing its healthcheck. The compose default tag tracks the
version in `pyproject.toml` and a test enforces it, for exactly this reason.

`docker pull evandhoffman/gmail-archive` with no tag therefore fails. That is
intended.

`linux/amd64` today.

## What is in the image

The runtime stage only: a virtualenv, the package source, and the SQL
migrations. It runs as a non-root user (uid 65532) and contains no mail, no
credentials and no host paths — `.env`, `data/`, `blobs/` and `*.mbox` are all
excluded from the build context.

`/version` reports the version, commit and build time of the running image.

## Configuration

Everything is environment variables; `.env.example` in the repository
documents them all. The ones that matter:

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | **Required.** Password for the Postgres container |
| `GMAIL_ARCHIVE_WEB_PASSWORD_HASH` | Web UI password hash. Empty means no authentication |
| `GMAIL_ARCHIVE_DATABASE_URL` | Postgres DSN — compose builds this for you |
| `GMAIL_ARCHIVE_BLOB_HOST_PATH` | Where the blob store lives on the host |
| `MBOX_HOST_DIR` | Directory holding the Takeout export, mounted read-only |
| `GMAIL_ARCHIVE_WORKERS` | Parse workers; empty means one per CPU |

## Commands

The image's entrypoint is the CLI, so a subcommand is the argument:

```bash
docker compose run --rm web stats
docker compose run --rm web search "invoice"
docker compose run --rm web verify --deep
docker compose run --rm web export /export/all.mbox --format mbox
```

`migrate`, `ingest`, `stats`, `search`, `verify`, `export`, `labels`,
`analyze`, `imap`, `imap-backfill`, `gen-fixture`, `serve`, `version`,
`set-password`.

## Personal tool, no support

Published in the open because there is no reason not to. It is not a product:
no support, no release process beyond tags, and no commitment to backwards
compatibility. Several known defects are listed in the
[repository README](https://github.com/evanwtf/gmail-archive#known-defects). If
you find it useful, fork it.

MIT licensed. Source: https://github.com/evanwtf/gmail-archive
