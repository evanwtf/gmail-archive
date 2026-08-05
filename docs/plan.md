# Scoped plan

The build plan for gmail-archive, phase by phase. This is the shape the project
is aiming at, not a contract — Phases 1–5 are specified seriously, Phase 6
onward is directional and the details are guesses. See
[progress.md](progress.md) for what has actually been built.

## Goal

Ingest a Google Takeout Gmail mbox export — roughly twenty years, expect
300k–800k messages and 30–80 GB — into Postgres for permanent local archival,
search, and export, with a local web UI for browsing. A read-only IMAP server
and a Gmail API sync path may follow.

**Every line of code must be exercisable against synthetic fixtures the project
generates itself.** "Works on day one with zero real input" is an acceptance
criterion, not an aspiration: the real export does not exist yet, and the tool
should still be fully demonstrable without it.

## Locked decisions

| Decision | Choice | Why |
|---|---|---|
| `raw_sha256` hashes | The **unquoted** RFC822 message | mbox prefixes body lines starting with `From ` with `>`, so file bytes are not the original. Unquoting means `.eml` export is correct; mbox export re-quotes and still round-trips byte-identically |
| Postgres | 18, major pinned | Longest support runway for a store meant to outlive the hardware |
| Base image | `python:3.13.14-slim-trixie`, exact patch | Chainguard cannot be version-pinned on the free tier, and stdlib `email`/`mailbox` behavior changes between Python minors |
| Licence | MIT, from commit #1 | Adding it later does not cleanly cover earlier commits |
| Schema extras | `in_reply_to`, `references_ids`, `bcc_addrs`, `reply_to`, and `messages.raw_sha256` FK to `blobs` | All cheap now; the FK turns "row points at a missing blob" into a constraint rather than a report |

## Stack

Python 3.13, uv, `psycopg[binary,pool]` 3.x with raw SQL (no ORM), numbered
`.sql` migrations applied by a small in-repo runner (no Alembic), pytest +
hypothesis, respx for HTTP mocking, click for the CLI, ruff + `mypy --strict` +
pre-commit, FastAPI + Jinja + HTMX for the web UI. Standard library `mailbox`
and `email` for parsing — no third-party MIME library.

Integration tests against Postgres use a service container plus a DSN env-var
gate (`GMAIL_ARCHIVE_TEST_DATABASE_URL`), not testcontainers. Unit tests run
Docker-free by default; integration tests skip cleanly without the DSN.

## Working rules

- **TDD where it pays.** Strict red → green → refactor for the parser, the
  ingest pipeline, and the storage invariants — stable surfaces, expensive bugs,
  irreplaceable data. For the CLI surface, the web views, and anything cosmetic,
  tests follow the code; that layer will churn.
- **No invented APIs.** Check the installed package or say so.
- **Conventions are artifacts, not prose.** A rule that lives only in a document
  is invisible. Make each one a `.gitignore` entry, a pre-commit hook, a test, or
  a comment on the line that motivated it.
- **Comments are archaeological.** Record the failure that motivated the line. A
  comment restating the command is noise.
- **Small commits, conventional messages,** each one self-consistent.
- **Stop at each phase gate.** Summarize, show test output, wait.

## Public repository hygiene

This repository is public and its history is permanent; the remedy for a mistake
is a history rewrite, not a revert. These rules outrank everything else here.

- **No personal data in fixtures, ever.** Generated addresses use RFC 2606
  reserved domains (`example.com`, `.invalid`, `.test`). The fixture factory must
  be *incapable* of emitting a real-looking address — enforced by a test.
- **No infrastructure identifiers in committed files:** no hostnames, internal
  IPs, log-aggregation endpoints, secret-manager vault or item names, VPN
  details. Anything host-specific is an `.env` var with a generic placeholder in
  `.env.example`.
- **No log-shipper service in the committed compose.** Only the optional
  `logging=promtail` labels, inert without an agent.
- **No secret-manager-based provisioning.** Plain `.env.example`.
- **README screenshots come only from generated fixture data.** This is the
  easiest way to leak twenty years of mail and it looks normal in a diff.

## Deployment target — undecided, so design for portability

Candidate hosts range from a 2-core/4-thread box to a 12-core desktop, some of
them already busy with other services. **No NFS or any network filesystem,
anywhere.**

- No host-specific paths, core counts, or memory sizes in committed files. Blob
  store path, pgdata location, worker count, and batch size all come from `.env`;
  workers default to `os.cpu_count()`.
- `postgresql.conf` knobs are a documented starting point with a comment saying
  what to scale each against — not tuned for one box.
- Assume local disk. If a design choice would behave differently over a network
  filesystem, don't make it.
- Moving the blob store or the whole stack between boxes must be an `.env` change
  and a data copy, never a redesign.

---

## Phase 0 — Plan

Container conventions extracted from a reference stack, module layout, CLI
surface, schema review, and disagreements surfaced before any code. **Complete**
— this document is its artifact.

## Phase 1 — Bootstrap

Public repository, pushed straight to `main` (no PRs, no CI yet — rapid
iteration until the shape stops moving).

- Standalone uv project, `pyproject.toml` + lockfile, `requires-python = ">=3.13"`
- LICENSE, README with a quick start and an honest "personal tool, no support"
  note
- `.gitignore`: `data/`, `*.mbox`, `blobs/`, `backups/`, `.env`, `.venv`
- Pre-commit hooks, including the guardrail that cannot be deferred: reject any
  staged `*.mbox`, anything under `blobs/`, or any file over ~5 MB. With no CI
  there is nothing else between a careless `git add` and an unrecoverable
  mistake. Plus gitleaks, ruff, and mypy.
- Dockerfile, `docker-compose.yml`, `.env.example`, `.dockerignore`,
  `postgresql.conf`
- Stub app serving `/healthz` and `/version`, proving the image builds, runs
  non-root, passes its exec-form healthcheck, and comes up healthy against
  Postgres before any real code depends on it
- `tests/test_compose_config.py` pinning the production posture

## Phase 2 — Synthetic mbox fixture generator

The load-bearing piece, and a first-class CLI command
(`gmail-archive gen-fixture OUT --count N --seed S --pathologies ...`) rather
than a test helper — that is what makes the README quick start real.

Configurable message counts and a menu of pathologies it can produce
individually:

- `X-Gmail-Labels` with commas, quotes, unicode, nested paths
- Missing / malformed / timezone-less `Date`; dates in 1998 and 2087
- Charsets that are wrong, absent, or nonexistent (`charset=unicode`)
- RFC 2047 encoded-words, including ones split mid-multibyte-character
- Multipart nesting 5+ deep; `alternative` inside `related` inside `mixed`
- Duplicate `Message-ID` across messages; messages with none
- Attachments: repeated across messages (dedup target), zero-byte, `../` and
  unicode in the filename, 25 MB near-limit
- Bare `From ` lines inside bodies — the classic mbox delimiter bug
- Base64 with bad padding; 8-bit bytes in a header
- One message truncated mid-body

`--seed` for reproducibility (asserted by generating twice and comparing bytes),
a size profile for 100k-message throughput runs behind a pytest marker. Test the
factory itself: assert each pathology appears when requested, and assert no
generated address escapes the RFC 2606 domains.

## Phase 3 — Parser

Bytes in, typed `ParsedMessage` out. The raw bytes are ground truth; the parse is
a derived view and never lossy.

Every field is best-effort. Failures accumulate in structured `parse_warnings`
rather than raising — one bad 2009 Outlook message must never kill a six-hour
run. Hypothesis property test: for any byte string, `parse()` returns or warns,
never raises.

Two hazards found in review that the parser must handle, each with a fixture:

- **Postgres `text` cannot contain NUL (U+0000)**, and decoded bodies will
  contain them. Unsanitized, one aborts a COPY batch of thousands. Lone
  surrogates are the same class of problem.
- **The tsvector 1 MB hard limit.** The body slice feeding the generated search
  column has to stay well under it, or a single large message aborts the batch.

## Phase 4 — Schema and storage

Raw bytes live in the content-addressed blob store, not in Postgres. `pg_dump`
stays a few GB of derived metadata instead of 80 GB; the filesystem gets
compression and snapshots on the bulk; `verify` becomes one mechanism instead of
two.

Write ordering is: blob to `.tmp` in the same directory → `fsync` → atomic
rename → **`fsync` the containing directory** → then insert the row. Without the
directory `fsync` the rename itself is not durable. Blob-then-row without
temp-and-rename can leave a truncated file that a valid row points at, which is
strictly worse than an orphan; orphans are recoverable, silent truncation is not.

Tables: `schema_migrations`, `messages`, `labels`, `blobs`, `attachments`,
`ingest_runs`, `failed_messages`, `message_sightings`. The authoritative
definition is `migrations/0001_initial.sql` once it lands; the decisions behind
it:

- **`messages.raw_sha256` is the primary key** — the sha256 of the verbatim
  RFC822 bytes, not any Gmail identifier. Idempotency becomes a database
  constraint (`ON CONFLICT DO NOTHING`) rather than pipeline bookkeeping, and it
  does not assume a field we have not verified exists.
- `gmail_id`, `thread_id`, `message_id` are all best-effort and nullable.
  Takeout supplies `X-GM-THRID` and `X-Gmail-Labels`, but no per-message Gmail
  id — expect `gmail_id` to be null for the whole mbox path.
- **The search column is a generated `tsvector` using the 2-arg
  `to_tsvector`** — the 1-arg form is `STABLE`, not `IMMUTABLE`, and Postgres
  rejects it in a generated column. `left()` bounds each input.
- **The keyset index needs `nulls last`.** `internal_date` is nullable, so a
  plain `desc` puts NULLs first and keyset pagination walks off the end.
  `query.py` must match the index ordering exactly.
- `labels` is indexed with **btree, not GIN**: GIN on a scalar text column needs
  `btree_gin`, buys nothing over btree for equality, and btree serves "all
  messages with label X" directly.
- `attachments.filename` and `.mime_type` are stored **as declared** and never
  trusted as a filesystem path or for serving.
- **Attachment extraction is a knob.** Attachment bytes already live inside the
  message raw blob, so extracting them stores a second decoded copy — 80 GB of
  mbox could become 120–140 GB after dedup. Metadata (sha256, size, filename,
  mime) is always recorded; writing the bytes is configurable, and `verify`
  reports "attachment rows with no blob" as an explicit state.
- `message_sightings` records each sighting of byte-identical duplicates that
  collapse into one row, so nothing is silently lost and `verify` can reconcile
  against the source. Inserts need `ON CONFLICT DO NOTHING` — resume replays the
  batch whose checkpoint had not advanced.
- `failed_messages` keeps raw bytes so a parser fix can replay them, capped so a
  handful of 25 MB failures does not undo the "keep `pg_dump` small" rationale.

Deferred to the IMAP phase deliberately: envelope, bodystructure, and the
folder/UID model. They serve no purpose for a web UI, and because raw bytes are
retained they can be added later as a backfill rather than a redesign.

**One module (`gmail_archive.query`) is the only place allowed to build read SQL
against `messages`;** `storage.py` is the only writer; `migrations/*.sql` define
it. The CLI, the web UI, and eventually IMAP SEARCH all go through `query`. A
test greps for stray SQL against an explicit allowlist and fails.

## Phase 5 — Ingest pipeline

- **Resumable and idempotent.** Checkpoint the byte offset; re-running after a
  kill resumes; re-ingesting the same file twice adds nothing. Verified with a
  container kill mid-run — the checkpoint has to survive the container, not just
  the process.
- **Stream the mbox, never load it.** A byte-level `From_` splitter, tested
  against the bare-`From `-line fixture.
- **Workers receive offsets, not bytes.** The parent splitter scans for
  boundaries (cheap, sequential) and sends `(offset, length)`; each worker
  `pread`s its own range, hashes, parses, writes blobs directly, and returns only
  the small metadata row. Shipping 25 MB messages through a multiprocessing pipe
  would mean pickling and copying the entire 80 GB.
- CPU-bound parse across a process pool; DB writes via `COPY` in batches.
- Failures land in `failed_messages` with raw bytes and traceback; the run
  continues.
- Runs as a profiled one-shot against a read-only `/mbox` mount.
- **Benchmark and report both msg/sec and MB/sec** — the run moves 30–80 GB
  through sha256 and into storage, so parse rate alone will not be the headline.
  Measure before optimizing.

A minimal `stats` and `search` land at this gate rather than waiting for Phase 6,
so there is something to query as early as it is honest to have one.

---

*Everything below is a sketch, included so Phase 4's decisions are not made
blind. Expect revision after the prototype has been used. Do not start any of it
without a go-ahead.*

## Phase 6 — Verify / query CLI

`migrate`, `ingest`, `verify [--deep]`, `stats`, `search`, `export`, `serve`.

`verify --deep` re-hashes every blob against its `raw_sha256` primary key.
Because the PK *is* the content hash, that is a single pass with no side
bookkeeping — the real payoff of the schema decision. It also reports orphaned
blobs and reconciles row count against `message_sightings`.

`export` reconstitutes a filtered set as valid mbox or `.eml`. Round-trip test:
ingest a fixture, export it, re-ingest, assert byte-identical raw for every
message.

## Phase 7 — Web UI

FastAPI + Jinja + HTMX, server-rendered, no build step. Message list (keyset
pagination on `(internal_date, raw_sha256)` — `OFFSET` over 600k rows is not
acceptable), thread view, message view with `parse_warnings` in a debug panel,
search over `websearch_to_tsquery` with `ts_headline` snippets, stats dashboard.

**HTML rendering is the security-critical part.** Twenty years of mail is full of
tracking pixels and remote CSS from ad networks that may still resolve. Sanitize
with nh3; render bodies in a sandboxed iframe with no `allow-scripts` under a CSP
that blocks remote fetches; block remote images by default behind an explicit
per-message opt-in; serve attachments `Content-Disposition: attachment` with
`nosniff`, never inline, never trusting the declared MIME type. Test each with a
fixture that tries the corresponding attack.

The app binds `0.0.0.0` inside the container; the compose `ports:` mapping
restricts exposure, publishing as `127.0.0.1:PORT:8000`.

## Phase 8 — Gmail API sync (interface + mocks only)

A `MessageSource` protocol satisfied by both the mbox reader and a
`GmailApiSource`, implemented against respx mocks: pagination, 429 with
`Retry-After`, 403 rate-limit, token refresh, `historyId` incremental sync. No
network. Note the 250 units/sec/user quota and that `messages.get` costs 5.

## Phase 9 — Read-only IMAP server

Evaluate building on pymap rather than hand-rolling the protocol. Adds the
deferred envelope/bodystructure columns and the folder/UID model, backfilled from
the blob store.

Two things that bite if built casually. **BODYSTRUCTURE** is the fiddliest part
of IMAP — nested multiparts, `message/rfc822` parts carrying their own envelope
and line counts — so compute it in a backfill tested against the Phase 2
pathologies, not inside a live protocol session. And **UIDs**: Gmail labels are
many-to-many while IMAP folders are not, so one message appears in several
folders with a different, permanent UID in each. UIDs are assigned once, ascend
strictly within a folder, and are never reused; clients cache hard enough that
violating this looks like data loss.

## Phase 10 — Wrap up

- README with real usage examples and measured throughput
- `docs/runbook.md`: resuming a failed ingest, verifying integrity, restoring a
  single message, and which Postgres bulk-load settings to revert after the
  initial import
- `docs/adr/` with short ADRs for the decisions actually made
- Generate `AGENTS.md` against the finished repository — a single root file,
  produced from real evidence rather than hand-written
