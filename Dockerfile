# syntax=docker/dockerfile:1

ARG GMAIL_ARCHIVE_VERSION
ARG GMAIL_ARCHIVE_COMMIT
ARG GMAIL_ARCHIVE_BUILD_TIME

# ── builder ───────────────────────────────────────────────────────────────────
FROM cgr.dev/chainguard/python:latest-dev AS builder

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
    # .python-version pins 3.13 for local development; the Chainguard images
    # ship 3.14, so without this uv downloads its own CPython into
    # /home/nonroot/.local/share/uv/ and points .venv/bin/python at it. The
    # runtime stage copies only /app, so that symlink dangles and the container
    # dies at startup with:
    #   exec: "/app/.venv/bin/python": stat ...: no such file or directory
    # UV_PYTHON_DOWNLOADS=never turns a future requires-python mismatch into a
    # loud build failure instead of a silently broken image.
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never

# Dependency layer first, before the source copy, so editing a Python file does
# not invalidate the (slow) dependency resolve and build.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

# Prepared here because the runtime stage below is shell-less: a RUN there
# fails at build time with no /bin/sh. Writable directories are created now and
# carried over with ownership. Under /app because this stage also runs as the
# nonroot build user — `mkdir /scratch` at the filesystem root is denied.
RUN mkdir -p /app/scratch/blobs /app/scratch/data

# ── runtime ───────────────────────────────────────────────────────────────────
FROM cgr.dev/chainguard/python:latest AS runtime

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src   /app/src

ARG GMAIL_ARCHIVE_VERSION
ARG GMAIL_ARCHIVE_COMMIT
ARG GMAIL_ARCHIVE_BUILD_TIME

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    GMAIL_ARCHIVE_VERSION=$GMAIL_ARCHIVE_VERSION \
    GMAIL_ARCHIVE_COMMIT=$GMAIL_ARCHIVE_COMMIT \
    GMAIL_ARCHIVE_BUILD_TIME=$GMAIL_ARCHIVE_BUILD_TIME

EXPOSE 8000

# No RUN in this stage — cgr.dev/chainguard/python:latest ships no shell at all,
# so these arrive pre-made from the builder, chowned to nonroot (65532) at copy
# time. Bind mounts still need the init-perms one-shot in compose; host
# directories do not inherit image ownership.
COPY --from=builder --chown=65532:65532 /app/scratch/blobs /blobs
COPY --from=builder --chown=65532:65532 /app/scratch/data  /data
VOLUME ["/blobs", "/data"]
USER nonroot

# Exec form and no curl: there is no shell, so CMD-SHELL healthchecks and any
# external binary are both unavailable. urllib from the venv python is all
# there is. /healthz is liveness only and deliberately does not touch Postgres,
# so a database restart cannot mark this container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", \
         "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]

# Exec-form ENTRYPOINT naming the venv python explicitly: the base image ships
# ENTRYPOINT ["/usr/bin/python"], which would swallow a CMD as script arguments
# ("python python -m gmail_archive ..."). The absolute path also means PATH
# ordering cannot change which interpreter boots.
ENTRYPOINT ["/app/.venv/bin/python", "-m", "gmail_archive"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
