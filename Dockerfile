# syntax=docker/dockerfile:1

# python:3.13.14-slim-trixie, pinned to an exact patch release.
#
# The obvious alternative, cgr.dev/chainguard/python, is a better hardening
# story but cannot be pinned: the free tier publishes only :latest and :latest-dev
# (`docker manifest inspect cgr.dev/chainguard/python:3.13` is denied), so the
# interpreter minor version moves under you between rebuilds. This project's
# core is stdlib `email` and `mailbox` parsing, whose behavior changes between
# Python minors, and it runs on a local network rather than the public
# internet — so a pinned interpreter is worth more here than a shell-less
# runtime.
#
# Debian slim over alpine: musllinux wheels do now exist for psycopg-binary and
# uvloop, but glibc/manylinux remains the better-tested path for psycopg, and
# the size difference does not matter for a tool that stores tens of gigabytes.
ARG PYTHON_IMAGE=python:3.13.14-slim-trixie

ARG GMAIL_ARCHIVE_VERSION
ARG GMAIL_ARCHIVE_COMMIT
ARG GMAIL_ARCHIVE_BUILD_TIME

# ── builder ───────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS builder

WORKDIR /app

# Re-declared: build args do not cross stage boundaries. The ARGs above the
# first FROM are global scope and are invisible inside a stage until named again.
ARG GMAIL_ARCHIVE_VERSION
ARG GMAIL_ARCHIVE_COMMIT
ARG GMAIL_ARCHIVE_BUILD_TIME

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    # Build against the base image's interpreter, never a uv-managed one.
    # Without this, uv resolves .python-version by downloading its own CPython
    # into ~/.local/share/uv/ and points .venv/bin/python at it; the runtime
    # stage copies only /app, so the symlink dangles and the container dies at
    # startup with `exec: "/app/.venv/bin/python": no such file or directory`.
    # DOWNLOADS=never turns a future requires-python mismatch into a loud build
    # failure rather than a silently broken image.
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never

# Dependency layer first, before the source copy, so editing a Python file does
# not invalidate the (slow) dependency resolve and build.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

# ── runtime ───────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

WORKDIR /app

ARG GMAIL_ARCHIVE_VERSION
ARG GMAIL_ARCHIVE_COMMIT
ARG GMAIL_ARCHIVE_BUILD_TIME

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    GMAIL_ARCHIVE_VERSION=$GMAIL_ARCHIVE_VERSION \
    GMAIL_ARCHIVE_COMMIT=$GMAIL_ARCHIVE_COMMIT \
    GMAIL_ARCHIVE_BUILD_TIME=$GMAIL_ARCHIVE_BUILD_TIME

# UID 65532 is not arbitrary: it is what the compose init-perms one-shot chowns
# the bind mounts to. Change it here and the app loses write access to the blob
# store. (The number is inherited from the Chainguard base this image replaced;
# keeping it means the existing blob store ownership stays valid.)
RUN groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin nonroot \
    && mkdir -p /blobs /data \
    && chown 65532:65532 /blobs /data

# The venv is built against this exact base image's interpreter, so its
# bin/python symlink resolves here. Ownership is set at copy time because the
# app runs as 65532 and needs to read every file in it.
COPY --from=builder --chown=65532:65532 /app/.venv      /app/.venv
COPY --from=builder --chown=65532:65532 /app/src        /app/src
COPY --from=builder --chown=65532:65532 /app/migrations /app/migrations

EXPOSE 8000
VOLUME ["/blobs", "/data"]
USER nonroot

# Exec form and no curl: slim ships neither curl nor wget, and urllib from the
# venv python needs no extra package. /healthz is liveness only and
# deliberately does not touch Postgres, so a database restart cannot mark this
# container unhealthy and put it in a restart loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", \
         "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]

# Exec-form and an absolute path to the venv interpreter, so PATH ordering
# cannot change which Python boots and a CMD is passed as arguments to the
# module rather than being swallowed as a script name.
ENTRYPOINT ["/app/.venv/bin/python", "-m", "gmail_archive"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
