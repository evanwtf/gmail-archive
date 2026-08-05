# gmail-archive

Ingest a Google Takeout Gmail mbox export into Postgres for permanent local
archival, search, and export, with a local web UI for browsing.

Roughly twenty years of mail — expect a few hundred thousand messages and tens
of gigabytes. Raw message bytes live in a content-addressed blob store on disk,
not in Postgres; the database holds derived metadata and the search index, so a
`pg_dump` stays small enough to be useful.

**Status: early. Nothing here is finished.** See "Project status" below for what
actually works today.

## Personal tool, no support, no stability guarantees

This is a personal archival tool published in the open because there's no reason
not to. It is not a product. There is no support, no release process, and no
commitment to backwards compatibility — the schema, the CLI, and the storage
layout may all change without migration paths. If you find it useful, fork it.

## Quick start

Everything runs in Docker, and everything can be exercised against generated
fixture data — you do not need a real mbox export to run this.

```bash
git clone https://github.com/evanwtf/gmail-archive.git
cd gmail-archive
cp .env.example .env      # then edit POSTGRES_PASSWORD
docker compose up -d
curl localhost:8000/healthz
curl localhost:8000/version
```

For local development outside Docker:

```bash
uv sync
uv run pre-commit install   # required: the hooks are what keep real mail out of git
uv run pytest
uv run gmail-archive version
```

`uv run pre-commit install` is not optional housekeeping. This repository is
public, and the hooks reject staged `.mbox` files, anything under `blobs/`, and
oversized files. Without them there is nothing between a stray `git add .` and a
permanent public commit of real mail.

## Project status

Working today:

- Docker image (Debian slim pinned to an exact Python patch release, runs as a
  non-root user) and a compose stack with a dedicated Postgres 18
- `/healthz`, `/readyz`, `/version`; `gmail-archive version`, `gmail-archive serve`

Not built yet: the fixture generator, the parser, the schema and migrations, the
ingest pipeline, search, export, and the web UI.

## License

MIT — see [LICENSE](LICENSE).
