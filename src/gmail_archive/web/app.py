"""FastAPI application.

Phase 1 stub: enough surface to prove the image builds, boots as nonroot,
passes its exec-form healthcheck, and can reach Postgres.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from gmail_archive.config import Settings
from gmail_archive.version import build_info

app = FastAPI(title="gmail-archive", docs_url="/docs")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness only — deliberately does not touch Postgres.

    This is what the container HEALTHCHECK calls. Wiring a database round-trip
    into it would turn a Postgres restart into an unhealthy app container and
    then a restart loop, so the database check lives in /readyz instead.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness — round-trips to Postgres."""
    settings = Settings.from_env()
    if not settings.database_url:
        return JSONResponse(
            {"status": "unconfigured", "detail": "GMAIL_ARCHIVE_DATABASE_URL is unset"},
            status_code=503,
        )
    try:
        with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
            conn.execute("select 1")
    except psycopg.Error as exc:
        return JSONResponse(
            {"status": "unavailable", "detail": type(exc).__name__}, status_code=503
        )
    return JSONResponse({"status": "ok"})


@app.get("/version")
def version() -> dict[str, Any]:
    return build_info()
