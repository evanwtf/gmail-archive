"""FastAPI application — web UI for browsing the archive.

Phase 7: server-rendered HTML with Jinja2 templates, HTMX for interactivity,
nh3 for HTML sanitization, and CSP headers for defense-in-depth.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
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
    CATEGORY_TABS,
    DEFAULT_SEARCH_SORT,
    MAILBOXES,
    SEARCH_SORTS,
    SYSTEM_LABELS,
    LabelCount,
    date_bounds,
    get_message,
    get_message_full,
    get_thread_messages,
    label_counts,
    list_labels,
    list_messages_keyset,
    search,
    stats,
)
from gmail_archive.storage import BlobStore
from gmail_archive.version import build_info
from gmail_archive.web.filters import (
    defang,
    gmail_date,
    highlight_snippet,
    relative_date,
    sender_name,
)

HERE = Path(__file__).parent

#: A content hash is 64 lowercase hex characters and nothing else. Checked
#: before the blob store sees it: `BlobStore.path_for` raises ValueError on a
#: wrong-length string, which would surface as a 500 rather than a 404.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

app = FastAPI(title="gmail-archive", docs_url="/docs")
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["relative_date"] = relative_date
templates.env.filters["gmail_date"] = gmail_date
templates.env.filters["sender_name"] = sender_name
templates.env.filters["defang"] = defang
templates.env.filters["highlight_snippet"] = highlight_snippet
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# ── Security middleware ────────────────────────────────────────────


@app.middleware("http")
async def add_security_headers(request: Request, call_next: Any) -> Response:
    """Set CSP and nosniff headers on every response.

    The CSP is the primary defense against injected content in archived
    messages. The sandboxed iframe in message.html is the second layer.
    """
    response: Response = await call_next(request)
    # 'self' throughout: htmx is vendored into /static, so no origin outside
    # this app needs to be reachable for a page to render. The duplicate
    # <meta http-equiv> copy of this policy was removed — one policy, one
    # place, no drift.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
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


def _chrome(conn: psycopg.Connection[object]) -> dict[str, Any]:
    """Everything the Gmail shell needs on every page: rail counts and labels.

    A single grouped scan of `labels` serves both — splitting it into a
    mailbox-count query and a label-list query meant scanning a million-row
    table twice for every page render.
    """
    counts = label_counts(conn)
    user_labels = sorted(
        (
            LabelCount(label=name, message_count=count)
            for name, count in counts.items()
            if name not in SYSTEM_LABELS and not name.startswith("Category")
        ),
        key=lambda entry: (-entry.message_count, entry.label),
    )
    return {
        "mailboxes": MAILBOXES,
        "mailbox_counts": counts,
        "category_tabs": CATEGORY_TABS,
        "user_labels": user_labels[:20],
    }


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    after_date: str | None = None,
    after_sha: str | None = None,
    label: str | None = "Inbox",
    category: str | None = None,
    on: str | None = None,
    picker: bool = False,
    inbox_only: bool = False,
    limit: int = 50,
) -> HTMLResponse:
    """The front door: the Gmail inbox.

    Defaults to Gmail's own ``Inbox`` label. ``?label=`` selects any other
    mailbox or user label; ``?label=`` with an empty value is All Mail.
    ``?category=`` selects an inbox tab, and the Primary tab is expressed as
    "in the inbox and in none of the other categories".

    ``?on=YYYY-MM-DD`` restricts the view to one calendar day. An unparseable
    date is ignored rather than raising — a hand-edited query string should
    land you in the mailbox, not on an error page.

    ``picker=1`` marks a submission from the day picker, where the "Only show
    Inbox" checkbox decides the scope. The marker is needed because an
    unchecked checkbox submits nothing at all, so without it "unchecked" and
    "not from the picker" are the same request.
    """
    after_date_dt: datetime | None = None
    if after_date:
        try:
            after_date_dt = datetime.fromisoformat(after_date)
        except ValueError:
            after_date_dt = None

    on_day: date | None = None
    if on:
        try:
            on_day = date.fromisoformat(on)
        except ValueError:
            on_day = None
    # No cursor reset here on purpose: the picker submits a bare `on`, so a
    # fresh jump already starts at the top of the day, and leaving the cursor
    # alone is what lets a day with more than one page still page.

    if picker:
        # The checkbox is authoritative for a picker submission: checked means
        # the Inbox, unchecked means everything. It deliberately overrides the
        # mailbox you were in — "Only show Inbox" would be a lie if unchecking
        # it still left you inside Starred.
        label = "Inbox" if inbox_only else ""
        if not inbox_only:
            category = None  # Gmail's tabs only exist inside the inbox

    # Primary is everything the other tabs do not claim.
    exclude: tuple[str, ...] = ()
    if category == "primary":
        exclude = tuple(name for name, _ in CATEGORY_TABS if name)
    elif category:
        label = category

    try:
        with _get_conn() as conn:
            msgs = list_messages_keyset(
                conn,
                after_date=after_date_dt,
                after_sha=after_sha,
                limit=limit + 1,
                label=label or None,
                exclude_labels=exclude,
                on_day=on_day,
            )
            context = _chrome(conn)
            earliest, latest = date_bounds(conn)
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

    context.update(
        {
            "messages": msgs,
            "next_cursor": next_cursor,
            "label": label,
            "category": category,
            "limit": limit,
            "on_day": on_day,
            "prev_day": (on_day - timedelta(days=1)) if on_day else None,
            "next_day": (on_day + timedelta(days=1)) if on_day else None,
            "earliest": earliest,
            "latest": latest,
            "title": (
                on_day.strftime("%A, %-d %B %Y") if on_day else (label or "All Mail")
            ),
        }
    )
    return templates.TemplateResponse(request, "mailbox.html", context)


@app.get("/messages", response_class=HTMLResponse)
def messages_page(
    request: Request,
    after_date: str | None = None,
    after_sha: str | None = None,
    label: str | None = None,
    limit: int = 50,
) -> HTMLResponse:
    """All Mail. Kept as its own path because it predates the inbox front door
    and is linked from the docs; the inbox is the same view with a label."""
    return index(
        request,
        after_date=after_date,
        after_sha=after_sha,
        label=label,
        limit=limit,
    )


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request) -> HTMLResponse:
    """Archive statistics — the dashboard that used to be the front door."""
    try:
        with _get_conn() as conn:
            s = stats(conn)
            context = _chrome(conn)
    except psycopg.Error:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )
    context["stats"] = s
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/messages/{sha256}", response_class=HTMLResponse)
def message_detail(request: Request, sha256: str) -> HTMLResponse:
    """Full message detail with body rendering and parse warnings."""
    try:
        with _get_conn() as conn:
            msg = get_message_full(conn, sha256)
            context = _chrome(conn)
    except psycopg.Error:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    # Sanitize first, then defang: nh3 decides what markup survives, and
    # defanging then neutralises every URL that survived with it. Doing it in
    # the other order would let nh3 re-normalise a defanged attribute.
    sanitized_html = defang(nh3.clean(msg.body_html)) if msg.body_html else ""

    context.update({"msg": msg, "sanitized_html": sanitized_html})
    return templates.TemplateResponse(request, "message.html", context)


@app.get("/thread/{thread_id}", response_class=HTMLResponse)
def thread_view(request: Request, thread_id: str) -> HTMLResponse:
    """All messages in a thread, ordered by date."""
    try:
        with _get_conn() as conn:
            msgs = get_thread_messages(conn, thread_id)
            context = _chrome(conn)
    except psycopg.Error:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    context.update({"thread_id": thread_id, "messages": msgs})
    return templates.TemplateResponse(request, "thread.html", context)


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "",
    offset: int = 0,
    limit: int = 50,
    sort: str = DEFAULT_SEARCH_SORT,
) -> HTMLResponse:
    """Full-text search with highlighted snippets.

    ``sort`` is one of ``date`` (newest first, the default), ``date-asc``, or
    ``relevance``. An unrecognised value falls back to the default rather than
    erroring — a hand-edited query string should not produce a 500.
    """
    if sort not in SEARCH_SORTS:
        sort = DEFAULT_SEARCH_SORT

    try:
        with _get_conn() as conn:
            result = search(conn, q, limit=limit, offset=offset, sort=sort)
            context = _chrome(conn)
    except psycopg.Error:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    context.update(
        {
            "query": q,
            "results": result.messages,
            "total": result.total,
            "offset": offset,
            "limit": limit,
            "sort": sort,
        }
    )
    return templates.TemplateResponse(request, "search.html", context)


@app.get("/labels", response_class=HTMLResponse)
def labels_page(request: Request) -> HTMLResponse:
    """List all labels with message counts."""
    try:
        with _get_conn() as conn:
            labels = list_labels(conn)
            context = _chrome(conn)
    except psycopg.Error:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    context["labels"] = labels
    return templates.TemplateResponse(request, "labels.html", context)


#: Rendered inline by /messages/{sha}/raw. Past this, the page stops being
#: useful and starts being a way to make the browser chew on a 25 MB base64
#: attachment; the download link is the right tool at that size.
RAW_VIEW_MAX_BYTES = 512_000


@app.get("/messages/{sha256}/raw", response_class=HTMLResponse)
def raw_message_view(request: Request, sha256: str) -> HTMLResponse:
    """Show the raw RFC822 source in the browser.

    Distinct from ``/raw/{sha256}``, which forces a download. The bytes are
    escaped into a ``<pre>`` by Jinja rather than served as their own document,
    so a message whose body is HTML — or claims to be — cannot render or
    execute here.
    """
    if not _SHA256_RE.fullmatch(sha256):
        raise HTTPException(status_code=404, detail="Message not found")

    store = _get_store()
    try:
        data = store.get(sha256)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Message not found") from None

    truncated = len(data) > RAW_VIEW_MAX_BYTES
    shown = data[:RAW_VIEW_MAX_BYTES] if truncated else data

    # errors="replace": these are twenty years of real mail, and plenty of it
    # is not valid UTF-8. A mojibake byte is worth showing; an exception is not.
    text = shown.decode("utf-8", errors="replace")

    subject: str | None = None
    context: dict[str, Any] = {}
    try:
        with _get_conn() as conn:
            msg = get_message(conn, sha256)
            subject = msg.subject if msg else None
            context = _chrome(conn)
    except psycopg.Error:
        # The blob is the point of this page; the subject and the surrounding
        # rail are only decoration, and the page is still worth serving.
        pass

    context.update(
        {
            "sha256": sha256,
            "subject": subject,
            "text": text,
            "truncated": truncated,
            "shown_bytes": len(shown),
            "total_bytes": len(data),
        }
    )
    return templates.TemplateResponse(request, "raw.html", context)


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
