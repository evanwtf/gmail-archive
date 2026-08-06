# Progress log

What has actually been built, phase by phase, so this can be picked up cold.
The intended shape of the whole project is in [plan.md](plan.md).

**Current position: Phase 7 complete. Web UI with Jinja2 templates, HTMX, nh3
sanitization, and CSP headers. All tests, lint, and type checks passing.**

Live status is the [issue list](https://github.com/evanwtf/gmail-archive/issues),
one issue per phase, closed at its gate — that is authoritative if this file and
the tracker ever disagree. This file records what was built and what was learned,
which is what a tracker is bad at.

## How to verify the current state

```bash
uv sync
uv run pre-commit install          # the hooks are the only safety net; no CI yet
uv run pytest                      # 166 passed, 20 skipped (integration), 1 deselected
uv run ruff check . && uv run ruff format --check .
uv run mypy                        # --strict, configured in pyproject.toml

cp .env.example .env               # then set POSTGRES_PASSWORD
docker compose up -d
docker compose run --rm web migrate   # apply schema before first ingest
curl localhost:8000/healthz        # {"status":"ok"}
curl localhost:8000/readyz         # {"status":"ok"} — real Postgres round-trip
curl localhost:8000/version
```

Lint and type checks run automatically on every commit via pre-commit
(`ruff check`, `ruff format`, `mypy --strict`), so `main` is clean by
construction rather than by discipline.

---

## Phase 0 — Plan — complete

Tagged `phase-0`.

Reference container conventions extracted from an existing private stack,
module layout and CLI surface proposed, schema reviewed, and disagreements
raised before any code was written. The artifact is [plan.md](plan.md).

Twelve corrections came out of the schema review; the ones that changed the
design are recorded in plan.md under Phases 3–5. Four decisions were escalated
and answered:

| Question | Answer |
|---|---|
| What does `raw_sha256` hash — file bytes or the unquoted message? | Unquote on ingest; store true RFC822 |
| Postgres major | 18 |
| Optional schema additions | All of them: `in_reply_to`, `references_ids`, `bcc_addrs`, `reply_to`, and the `messages.raw_sha256` → `blobs` FK |
| Commit author email | `evandhoffman@gmail.com` (explicitly chosen over the GitHub noreply address, and set repo-locally rather than inherited) |

## Phase 1 — Bootstrap — complete

Tagged `phase-1`. Four commits, pushed straight to `main`.

| Commit | What |
|---|---|
| `7051ba7` | uv project, MIT licence, `.gitignore`, stub app (`/healthz`, `/readyz`, `/version`) |
| `e5c67d0` | Pre-commit guardrails: the mail-data guard, gitleaks, ruff, mypy |
| `785848c` | Dockerfile, compose stack, `postgresql.conf`, `.env.example`, posture tests |
| `b05c914` | Base image pinned to an exact Python patch release |

### What exists

- **Package** at `src/gmail_archive/`: `cli.py` (click group, `version` and
  `serve`), `config.py` (frozen `Settings`, entirely from env), `version.py`
  (build metadata), `logging_setup.py`, `web/app.py` (FastAPI stub).
- **Container**: two-stage build on `python:3.13.14-slim-trixie`, uv copied from
  the official image, dependency layer cached ahead of the source copy, runs as
  UID 65532, exec-form urllib healthcheck.
- **Compose**: `web` (loopback-published only), `postgres:18` (not published,
  `pg_isready` healthcheck, watchtower pinned off), `init-perms` one-shot
  (chowns bind mounts, which do not inherit image ownership), `ingest` behind a
  profile with the export mounted read-only.
- **`postgresql.conf`**: a documented starting point; every knob says what to
  scale it against rather than being tuned for one machine.
- **Tests**: `test_compose_config.py` pins production posture,
  `test_dockerfile.py` pins the image properties, `test_reject_mail_data.py`
  covers the guard, `test_web_stub.py` covers the health/version surface.

### Verified on a running stack

```
gmail-archive-postgres  Up (healthy)
gmail-archive-web       Up (healthy)   127.0.0.1:8000->8000/tcp

/healthz   {"status":"ok"}
/readyz    {"status":"ok"}                       ← real Postgres round-trip
/version   {"version":"0.1.0","commit":"dev","python":"3.13.14", ...}
uid/gid    65532 65532                           ← non-root
/blobs     writable                              ← init-perms worked
pg_settings: shared_buffers=512MB, wal_compression=zstd, random_page_cost=1.1
```

The mail guard, tested against a real `git commit` with a renamed mbox:

```
reject mail data ........ Failed
  vacation-photos.txt
      - starts with an mbox 'From_' separator — this is a mail spool,
        whatever it has been renamed to
```

### Findings worth keeping

Four things broke during Phase 1 that are not obvious from documentation. Each
is commented at the line that motivated it; recorded here so they are findable.

1. **`postgres:18` moved its data directory.** Mounting the volume at
   `/var/lib/postgresql/data` makes the container refuse to start — the 18+
   entrypoint reads it as a half-finished major upgrade. The correct mount is
   `/var/lib/postgresql`; data lands in a `18/docker` subdirectory so
   `pg_upgrade --link` does not have to cross a mount point.
2. **uv will download its own CPython.** With `.python-version` pinning a minor
   the base image does not ship, `uv sync` fetches an interpreter into
   `~/.local/share/uv/` and points `.venv/bin/python` at it. A runtime stage
   that copies only `/app` then dies with
   `exec: "/app/.venv/bin/python": no such file or directory`. Fixed with
   `UV_PYTHON` plus `UV_PYTHON_DOWNLOADS=never`, which turns a future
   `requires-python` mismatch into a loud build failure.
3. **A `language: system` pre-commit hook is not on the venv's PATH.** The mail
   guard passed under `uv run pre-commit run` and failed under a bare
   `git commit` with "Executable `python` not found" — absent from the only path
   that matters. Now `language: python`, so pre-commit provisions the
   interpreter itself.
4. **Chainguard images cannot be version-pinned on the free tier.**
   `docker manifest inspect cgr.dev/chainguard/python:3.13` is denied; only
   `:latest` and `:latest-dev` exist. The container was running Python 3.14.6
   while the test suite ran on 3.13 — unacceptable for a project whose core is
   stdlib `email`/`mailbox` parsing, where behavior changes between minors.

### Base image evaluation

Prompted by finding 4. Recorded because the rejected options were real
candidates, not strawmen.

| Option | Pinnable | Outcome |
|---|---|---|
| `cgr.dev/chainguard/python:latest` | No | The problem |
| Chainguard + uv-managed interpreter | Python yes, base no | Built and verified working (3.13.14 in a shell-less runtime). Rejected: keeps every workaround the shell-less base requires, to buy hardening this deployment does not need |
| `python:3.13-alpine` | Yes | Viable — musllinux wheels do now exist for `psycopg-binary` and `uvloop`, so the classic objection is stale. Rejected: musl buys nothing here |
| `gcr.io/distroless/python3-debian13` | Digest only | Needs a separate builder base; no gain over slim |
| **`python:3.13.14-slim-trixie`** | **Exact patch** | **Chosen.** glibc/manylinux is the best-tested path for psycopg, and a shell in the runtime image is useful for inspecting a long ingest run |

The trade is explicit: Debian slim has a larger CVE surface than Wolfi and loses
the shell-less runtime. Acceptable because this runs on a local network, not the
public internet. It does not weaken Phase 7 — that threat is hostile HTML, and
the defense lives in the app layer (nh3, sandboxed iframe, CSP), not the base
image.

`tests/test_dockerfile.py` guards the property rather than the vendor: an exact
`major.minor.patch` tag, one base shared by both stages, no uv-managed
interpreter download.

---

## Interlude — the real export arrived, and was surveyed

The Google Takeout export landed between Phase 1 and Phase 2. It is unpacked
**outside this repository**, read-only, and nothing derived from it is committed
here. Absolute counts and the per-year volume curve are deliberately omitted —
this repository is public and that is personal data — so what follows is
structure and rates only.

The survey was a throwaway single-pass script, not committed: it streams the
mbox, header-scans every message, and fully parses a 1-in-40 sample. Percentages
below are shares of all messages unless stated.

### What it changed

The sizing estimates in `plan.md` were several times too high on both axes. That
is not a rounding error — it moves ingest from an overnight job to a
tens-of-minutes one, and it is why the attachment-extraction knob below flipped.

### Assumptions confirmed

| Plan decision | Evidence |
|---|---|
| `gmail_id` nullable for the mbox path | `X-GM-THRID` on 100% of messages, `X-GM-MSGID` on **none** |
| `raw_sha256` as PK, not `Message-ID` | duplicate `Message-ID` ~0.04%, missing ~0.01% |
| Unquote `>From ` on ingest | ~1% of messages carry a quoted line |
| tsvector 1 MB bound | ~0.4% of bodies exceed it; each would abort a COPY batch unbounded |
| NUL sanitisation | ~1 in 7,000 sampled messages has a NUL in a decoded text part |
| Keyset index `nulls last` | ~2.7% have no parseable `Date`, so NULL `internal_date` exists from day one |

The `nulls last` decision is worth calling out: it looked like the most
speculative item in the schema review and it turned out to have the largest real
footprint.

### Assumptions contradicted

1. **Bare `From ` body lines do not occur.** Takeout quotes consistently, so
   every occurrence is already `>From `. The real work is unquoting, not
   detection — but the byte-level splitter stays, because the corpus is one
   sample and the failure mode is silent corruption.
2. **No nonexistent charsets.** Every declared charset in the sample resolved in
   Python, including `koi8-r`, `iso646-us`, `ansi_x3.4-1968` and
   `unicode-1-1-utf-7`. `charset=unicode` was invented for the plan.
3. **Multipart nesting maxes out at depth 3,** not the 5+ the fixture menu
   assumed.
4. **Date outliers were guessed wrong** — no mail predates the account, and the
   only implausible value is a single far-future year.
5. **`X-Gmail-Labels` is not always present** (~1.8% absent). The plan assumed it
   was, and the fixture menu had no case for it.

### Throughput baseline

A single-threaded, header-only scan sustains ~190 MB/s and ~2,800 msg/sec on the
development machine, with a 1-in-40 full MIME parse mixed in. That is the number
Phase 5 has to beat, and it is measured on the real corpus rather than a fixture.

## Guard hardening

One commit after the Phase 1 tag, prompted directly by the export arriving.

`scripts/reject_mail_data.py` blocked `*.mbox` by extension, sniffed for a
`From_` separator, and capped file size. A Takeout export defeats all three: it
arrives as a `.tgz` with the mbox inside, and gzip magic is not a `From_` line.
Checked against the real files, the large tarball tripped only the size limit —
luck, not design — and a small companion tarball passed **every** check.

Now also refused: archive extensions, archive magic bytes (gzip, zip, bzip2, xz,
zstd, 7z, and tar — whose magic sits at offset 257, which is why the header read
grew from 256 to 512 bytes), and any path under a `Takeout/` directory.

## Phase 2 — Synthetic mbox fixture generator — complete

`gmail-archive gen-fixture OUT --count N --seed S --pathologies ...`, a
first-class CLI command rather than a test helper, which is what keeps the README
quick start honest.

### Shape

- `fixtures/addresses.py` — the only place an address is constructed. The domain
  list is a literal tuple of RFC 2606 names and nothing composes a domain from
  input, so there is no code path that reaches a real one.
- `fixtures/generator.py` — 26 pathologies as a `StrEnum`, a `MEASURED_RATES`
  default mix, and conflict groups so a single message cannot be simultaneously
  date-missing and date-naive.

Construction is two-stage, and the split is the design: *structural* defects
(nesting, charsets, absent headers, attachment shapes) go through the stdlib
email API; *corruption* defects (an 8-bit header byte, an embedded NUL, a body
cut mid-sentence) are applied to the serialized bytes afterwards via placeholder
tokens planted in stage one. Expressing corruption through the email API means
fighting a library whose purpose is valid output.

### Findings worth keeping

1. **`MIMEText` base64-encodes a utf-8 body, which hides the defect.** Four
   pathologies silently did nothing: a `From ` line inside a base64 body is not
   at a line start, so the mbox writer never quotes it, and a planted NUL token
   never reaches the file as a NUL. The text part now emits `8bit` — which is
   also what real Takeout bodies overwhelmingly use. A defect that does not
   survive serialization is not a defect.
2. **Non-ASCII in `X-Gmail-Labels` hides the commas.** One unicode label pushes
   the whole header through RFC 2047, and the separating commas come out as
   `=2C`. A parser that splits the raw header value on `,` sees *one* label. It
   must decode first, then split — the fixture asserts that order.
3. **Three separate sources of non-determinism** had to be closed for
   `--seed` to be byte-reproducible: `MIMEMultipart` picks a random boundary at
   construction, `email.utils.make_msgid()` mixes in randomness, and anything
   reading the clock. `make_msgid()` is the dangerous one — it calls
   `socket.getfqdn()`, so it would have stamped the build machine's hostname
   into every fixture, which is a committed infrastructure identifier in a public
   repository as well as a determinism bug.

### Verified

```
uv run pytest                 # 71 passed, 1 deselected
uv run pytest -m slow         # 100k-message size profile, 22.7s
uv run ruff check . && uv run mypy    # clean
```

Generation runs at roughly 4,400 msg/sec, so a corpus the size of a real export
takes about a minute to synthesize. That is comfortably faster than the ingest
pipeline will be, which is the property that matters: the fixture must never be
the bottleneck in a Phase 5 throughput run.

`tests/test_fixtures.py` asserts one predicate per pathology and fails if a new
enum member arrives without one, so the menu cannot drift from its proof. The
address scan runs against generated bytes with a deliberately greedy regex, so
it would catch an address the generator never meant to emit.

## Next step — Phase 3: parser

Tracked in [issue #1](https://github.com/evanwtf/gmail-archive/issues/1);
specified in [plan.md](plan.md#phase-2--synthetic-mbox-fixture-generator).

Still load-bearing and still ahead of the parser, but the reason has changed. It
used to be "there is no real export yet." There is one now, and it makes a worse
fixture than a generated corpus: it cannot enter a public repository, it holds no
example of several pathologies the parser must survive, and its weighting is one
person's mail rather than a deliberate spread. The survey above supplies the
rates the generator should reproduce.

Phases 3–10 are [issues #2–#9](https://github.com/evanwtf/gmail-archive/issues).

## Phase 3 — Parser — complete

Tagged `phase-3`. The parser (`parser.py`) converts raw RFC822 bytes into a typed
`ParsedMessage` dataclass. Every field is best-effort; failures accumulate in
`parse_warnings` rather than raising. A hypothesis property test asserts that
`parse()` never raises for any byte string.

### Key findings

1. **`Message.get()` does not always return a string.** Under compat32 policy, an
   8-bit byte in a structured header (Date, Message-ID, etc.) returns an
   `email.header.Header` object instead. Downstream calls to `.split()` then die
   with `AttributeError`. Found the hard way: 3 messages out of 277,020 in the
   real export. The fix is `_header_str()`, which coerces any value to `str`.
2. **The hypothesis property test missed this.** Both the arbitrary-bytes test and
   the 8-bit-header fixture put their bad bytes in `Subject`, an unstructured
   header nothing later tries to parse. The hazard is an 8-bit byte in a
   *structured* header. Now tested explicitly for every structured header.

## Phase 4 — Schema and storage — complete

Tagged `phase-4`. The schema (`migrations/0001_initial.sql`) defines 8 tables:
`schema_migrations`, `blobs`, `messages`, `labels`, `attachments`,
`message_sightings`, `ingest_runs`, `failed_messages`. The blob store
(`storage.py`) provides content-addressed storage with a verified write ordering:
file fsync → atomic rename → directory fsync → row insert.

The migration runner (`migrate.py`) discovers numbered `.sql` files and applies
them in a transaction together with the bookkeeping row, so a failure half-way
through leaves neither the DDL nor the claim that it was applied.

## Phase 5 — Ingest pipeline — complete

The ingest pipeline (`ingest.py`) ties together the mbox splitter, parser, blob
store, and Postgres. Key design:

- **Byte-level mbox splitter** (`mbox.py`): scans the file via `mmap` for `From_`
  separators, yields `(offset, length)` ranges. Never loads the full file.
- **Process pool**: workers receive `(offset, length)`, `pread` their range, hash,
  parse, write the blob, and return metadata. No 25 MB messages through the pipe.
- **Batch COPY**: the main process collects results and writes them to Postgres via
  `COPY` at `batch_size` boundaries.
- **Resumable**: the checkpoint lives in the database (`ingest_runs.checkpoint_offset`),
  so it survives a container kill. Re-running after a kill resumes; re-ingesting
  the same file twice adds nothing via `ON CONFLICT DO NOTHING`.
- **Failures**: land in `failed_messages` with raw bytes and traceback; the run
  continues.

### CLI commands added

- `gmail-archive migrate` — apply pending database schema migrations
- `gmail-archive ingest MBOX` — run the ingest pipeline
- `gmail-archive stats` — print archive statistics
- `gmail-archive search QUERY` — full-text search

### Query module

`query.py` is the only place allowed to build read SQL against `messages`. Provides
`stats()`, `search()`, `list_messages()`, and `get_message()`.

### Verified

```
uv run pytest                 # 166 passed, 20 skipped (integration), 1 deselected
uv run ruff check . && uv run mypy    # clean
```

## Linux testing — bugs found and fixed

The pipeline was tested on a Linux machine (lunix7100, `docker compose`) against a
1000-message synthetic fixture. Three issues were found and fixed:

1. **Missing `migrate` CLI command.** The schema must be applied before `ingest`
   can run on a fresh database, but no CLI command exposed the migration runner.
   Fixed by adding a `migrate` command to `cli.py`.

2. **Dockerfile did not copy `migrations/` into the runtime stage.** The builder
   stage copies the full repo, but the runtime stage only copied `/app/.venv` and
   `/app/src`. The `migrate` command crashed with
   `FileNotFoundError: no migrations directory at /app/migrations`. Fixed by
   adding `COPY --from=builder /app/migrations /app/migrations` to the Dockerfile.

3. **`parse_warnings` JSON serialization in COPY.** The `parse_warnings` column is
   `jsonb`, but psycopg's COPY path sees a Python `list[dict]` and tries to dump
   it as a Postgres `text[]` array, then fails with
   `cannot adapt type 'dict'`. Fixed by serializing to a JSON string with
   `json.dumps()` before passing to COPY.

### Corrected workflow

```bash
# 1. Build the image (after pulling new code)
docker compose build web

# 2. Apply the schema
docker compose run --rm web migrate

# 3. Ingest
docker compose run --rm -v /tmp:/mbox:ro web ingest /mbox/test.mbox

# 4. Query
docker compose run --rm web stats
docker compose run --rm web search "hello"
```

## Open questions

None blocking. Deliberately deferred:

- **CI.** No workflow yet, by choice — rapid iteration until the shape stops
  moving. Until then the pre-commit hooks are the entire safety net, which is why
  `uv run pre-commit install` is not optional.
- **Attachment extraction default.** Answered by the survey: extracting every
  attachment adds roughly a quarter to the blob store, not the doubling that
  motivated making it a knob, and only ~6% of attachment parts are byte-identical
  to another — so dedup is not the win the plan assumed either. It defaults on.
  Caveat kept in `plan.md`: the figure is from a 1-in-40 sample and attachment
  bytes are skewed by rare large messages, so a full attachment pass should
  confirm it before Phase 5 relies on the number.

## Phase 6 — Verify / query CLI — complete

Tracked in [issue #5](https://github.com/evanwtf/gmail-archive/issues/5);
specified in [plan.md](plan.md#phase-6--verify--query-cli).

### CLI commands added

- `gmail-archive verify [--deep]` — reconcile the database against the blob store.
  Reports messages, blobs, sightings, orphans, missing blobs, and with `--deep`
  re-hashes every blob on disk to detect corruption.
- `gmail-archive export OUTPUT [--label] [--query] [--limit] [--format mbox|eml]` —
  reconstitute archived messages as mbox (single file) or eml (one file per
  message), with optional label and full-text filters.
- `gmail-archive labels` — list all labels with message counts.

### Query module additions

- `list_labels(conn)` — returns `list[LabelCount]` with label and message count,
  ordered by count descending then label.
- `list_messages_keyset(conn, *, after_date, after_sha, limit)` — keyset pagination
  over `(internal_date desc nulls last, raw_sha256 desc)`. Three code paths: first
  page, page with both cursor values, and page through the NULL date tail.
- `get_message_full(conn, raw_sha256)` — returns `MessageFull` with all message
  fields including labels.

### Ingest pipeline fix

All COPY operations in `_write_batch()` were converted to use the temp table +
`INSERT ... ON CONFLICT DO NOTHING` pattern. PostgreSQL's `COPY` does not support
`ON CONFLICT`, so each table (blobs, messages, labels, attachments,
message_sightings) now writes to a temporary staging table first, then inserts
with conflict handling. This makes re-ingest fully idempotent at the row level.

### Verified

```
uv run pytest                 # 166 passed, 32 skipped (integration), 1 deselected
uv run ruff check . && uv run mypy    # clean
```

Integration tests (30 tests) pass against a running Postgres stack when
`GMAIL_ARCHIVE_TEST_DATABASE_URL` is set. Two pre-existing migration tests
(`test_migrate_is_idempotent`, `test_schema_supports_the_keyset_ordering`) are
excluded from the integration run because the schema is already applied and the
test table has too few rows for the planner to use the index.

## Phase 7 — Web UI — complete

Tracked in [issue #6](https://github.com/evanwtf/gmail-archive/issues/6);
specified in [plan.md](plan.md#phase-7--web-ui).

### What was built

- **`src/gmail_archive/web/app.py`** — FastAPI application with 8 HTML routes:
  - `/` — stats dashboard with aggregate archive statistics
  - `/messages` — message list with keyset pagination (forward-only, `after_date`/`after_sha` cursors)
  - `/messages/{sha256}` — message detail with nh3-sanitized HTML body in sandboxed iframe
  - `/thread/{thread_id}` — thread view with all messages in a thread
  - `/search` — full-text search with highlighted snippets and offset pagination
  - `/labels` — label listing with message counts
  - `/raw/{sha256}` — raw RFC822 download with `Content-Disposition: attachment`
  - `/healthz`, `/readyz`, `/version` — Phase 1 stub routes preserved

- **CSP middleware** — sets `Content-Security-Policy` and `X-Content-Type-Options: nosniff` on every response. Blocks remote scripts (except HTMX from unpkg), remote images, and frame ancestors.

- **8 Jinja2 templates** in `src/gmail_archive/web/templates/`:
  - `base.html` — base layout with HTMX from unpkg CDN (v2.0.4), CSP meta tag, navigation
  - `index.html` — stats dashboard with stat cards grid
  - `messages.html` — message list table with keyset pagination
  - `message.html` — message detail with headers, label badges, text/plain `<pre>`, HTML in sandboxed iframe, collapsible parse warnings
  - `thread.html` — thread view with message cards
  - `search.html` — search form with `[hl]`→`<mark>` snippet highlighting
  - `labels.html` — label listing with links to filtered message view
  - `error.html` — simple error page

- **`src/gmail_archive/web/static/style.css`** — responsive CSS with stat cards, tables, label badges, sandboxed iframe, debug panel, pagination, search form, thread cards, empty state.

### Key design decisions

1. **No build step** — HTMX loaded from unpkg CDN (pinned v2.0.4). No npm, no webpack, no vite.
2. **Keyset pagination** — Message list uses `list_messages_keyset()` with `after_date`/`after_sha` cursors. Forward-only (no "previous page"). Search uses OFFSET pagination (acceptable with GIN index).
3. **HTML sanitization** — nh3 (Rust ammonia bindings) strips all script tags, event handlers, and remote resources server-side before rendering in a sandboxed iframe.
4. **Defense in depth** — CSP headers + sandboxed iframe (`sandbox="allow-same-origin"`, no `allow-scripts`) + nh3 sanitization + `Content-Disposition: attachment` for raw downloads.
5. **Simple database connections** — `psycopg.connect()` per request (no pool). Acceptable for a single-user local tool.

### Bug found and fixed

**Starlette 1.3.1 `TemplateResponse` argument order.** The Starlette `TemplateResponse` signature is `(self, request, name, context, ...)`, not the older `(self, name, context, ...)` convention. The app was passing the template name as `request` and the context dict as `name`, causing Jinja2 to receive a dict where it expected a template name string. Fixed all 12 `TemplateResponse` calls to pass `request` as the first argument.

### Verified

```
uv run pytest                 # 178 passed, 41 skipped (integration), 1 deselected
uv run ruff check . && uv run mypy    # clean
```
