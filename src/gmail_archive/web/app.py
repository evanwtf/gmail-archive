"""FastAPI application — web UI for browsing the archive.

Phase 7: server-rendered HTML with Jinja2 templates, HTMX for interactivity,
nh3 for HTML sanitization, and CSP headers for defense-in-depth.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import nh3
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gmail_archive.config import Settings
from gmail_archive.query import (
    get_message_full,
    get_thread_messages,
    list_labels,
    list_messages_keyset,
    search,
    stats,
)
from gmail_archive.storage import BlobStore
from gmail_archive.version import build_info

HERE = Path(__file__).parent

app = FastAPI(title="gmail-archive", docs_url="/docs")
templates = Jinja2Templates(directory=str(HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# ── Security middleware ────────────────────────────────────────────


@app.middleware("http")
async def add_security_headers(request: Request, call_next: Any) -> Response:
    """Set CSP and nosniff headers on every response.

    The CSP is the primary defense against injected content in archived
    messages. The sandboxed iframe in message.html is the second layer.
    """
    response: Response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://unpkg.com/htmx.org@2.0.4; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ── Helpers ───────────────────────────────────────────────────────


def _get_conn() -> psycopg.Connection[object]:
    """Open a database connection from environment settings."""
    settings = Settings.from_env()
    return psycopg.connect(settings.database_url)


def _get_store() -> BlobStore:
    """Create a BlobStore from environment settings."""
    settings = Settings.from_env()
    return BlobStore(settings.blob_dir)


# ── Phase 1 stub routes (unchanged) ───────────────────────────────


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness only — deliberately does not touch Postgres."""
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
    """Return build metadata."""
    return build_info()


# ── Phase 7 web UI routes ─────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Stats dashboard."""
    try:
        with _get_conn() as conn:
            s = stats(conn)
    except Exception:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )
    return templates.TemplateResponse(request, "index.html", {"stats": s})


@app.get("/messages", response_class=HTMLResponse)
def messages_page(
    request: Request,
    after_date: str | None = None,
    after_sha: str | None = None,
    label: str | None = None,
    limit: int = 50,
) -> HTMLResponse:
    """Message list with keyset pagination.

    Pass ``after_date`` and ``after_sha`` from the last message on the
    previous page to get the next page. Supports optional ``label`` filter.
    """
    after_date_dt: datetime | None = None
    if after_date:
        try:
            after_date_dt = datetime.fromisoformat(after_date)
        except ValueError:
            after_date_dt = None

    try:
        with _get_conn() as conn:
            msgs = list_messages_keyset(
                conn,
                after_date=after_date_dt,
                after_sha=after_sha,
                limit=limit + 1,
                label=label,
            )
    except psycopg.Error:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    has_more = len(msgs) > limit
    if has_more:
        msgs = msgs[:limit]

    next_cursor: dict[str, str] | None = None
    if has_more and msgs:
        last = msgs[-1]
        next_cursor = {
            "after_date": str(last.internal_date) if last.internal_date else "",
            "after_sha": last.raw_sha256,
        }

    return templates.TemplateResponse(
        request,
        "messages.html",
        {
            "messages": msgs,
            "next_cursor": next_cursor,
            "label": label,
        },
    )


@app.get("/messages/{sha256}", response_class=HTMLResponse)
def message_detail(request: Request, sha256: str) -> HTMLResponse:
    """Full message detail with body rendering and parse warnings."""
    try:
        with _get_conn() as conn:
            msg = get_message_full(conn, sha256)
    except psycopg.Error:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    sanitized_html = nh3.clean(msg.body_html or "") if msg.body_html else ""

    return templates.TemplateResponse(
        request,
        "message.html",
        {
            "msg": msg,
            "sanitized_html": sanitized_html,
        },
    )


@app.get("/thread/{thread_id}", response_class=HTMLResponse)
def thread_view(request: Request, thread_id: str) -> HTMLResponse:
    """All messages in a thread, ordered by date."""
    try:
        with _get_conn() as conn:
            msgs = get_thread_messages(conn, thread_id)
    except psycopg.Error:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    return templates.TemplateResponse(
        request,
        "thread.html",
        {
            "thread_id": thread_id,
            "messages": msgs,
        },
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "",
    offset: int = 0,
    limit: int = 50,
) -> HTMLResponse:
    """Full-text search with highlighted snippets."""
    try:
        with _get_conn() as conn:
            result = search(conn, q, limit=limit, offset=offset)
    except psycopg.Error:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "query": q,
            "results": result.messages,
            "total": result.total,
            "offset": offset,
            "limit": limit,
        },
    )


@app.get("/labels", response_class=HTMLResponse)
def labels_page(request: Request) -> HTMLResponse:
    """List all labels with message counts."""
    try:
        with _get_conn() as conn:
            labels = list_labels(conn)
    except psycopg.Error:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    return templates.TemplateResponse(
        request,
        "labels.html",
        {"labels": labels},
    )


@app.get("/raw/{sha256}")
def raw_message(sha256: str) -> Response:
    """Download the raw RFC822 bytes of a message.

    Served with ``Content-Disposition: attachment`` so the browser never
    renders it inline, regardless of MIME type.
    """
    store = _get_store()
    try:
        data = store.get(sha256)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Message not found") from None
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{sha256}.eml"'},
    )
