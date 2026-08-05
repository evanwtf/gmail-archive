# Progress log

What has actually been built, phase by phase, so this can be picked up cold.
The intended shape of the whole project is in [plan.md](plan.md).

**Current position: Phases 0 and 1 complete, reviewed, and tagged.
Next step is Phase 2 — the synthetic mbox fixture generator.**

Live status is the [issue list](https://github.com/evanwtf/gmail-archive/issues),
one issue per phase, closed at its gate — that is authoritative if this file and
the tracker ever disagree. This file records what was built and what was learned,
which is what a tracker is bad at.

## How to verify the current state

```bash
uv sync
uv run pre-commit install          # the hooks are the only safety net; no CI yet
uv run pytest                      # 30 passed
uv run ruff check . && uv run ruff format --check .
uv run mypy                        # --strict, configured in pyproject.toml

cp .env.example .env               # then set POSTGRES_PASSWORD
docker compose up -d
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
- **Tests (30)**: `test_compose_config.py` pins production posture,
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

## Next step — Phase 2: synthetic mbox fixture generator

Tracked in [issue #1](https://github.com/evanwtf/gmail-archive/issues/1);
specified in [plan.md](plan.md#phase-2--synthetic-mbox-fixture-generator).

Load-bearing, and deliberately ahead of the parser: there is no real mbox export
yet, so nothing downstream can be exercised until the project can generate its
own input.

Phases 3–10 are [issues #2–#9](https://github.com/evanwtf/gmail-archive/issues).

## Open questions

None blocking. Deliberately deferred:

- **CI.** No workflow yet, by choice — rapid iteration until the shape stops
  moving. Until then the pre-commit hooks are the entire safety net, which is why
  `uv run pre-commit install` is not optional.
- **Attachment extraction default.** Whether to write attachment bytes to the
  blob store by default, or record metadata only and re-materialize on demand,
  should be decided against a measurement rather than a guess. Phase 4/5.
