"""FastAPI application — web UI for browsing the archive.

Phase 7: server-rendered HTML with Jinja2 templates, HTMX for interactivity,
nh3 for HTML sanitization, and CSP headers for defense-in-depth.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import re
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import nh3
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool, PoolTimeout

from gmail_archive.analytics import (
    correspondent,
    correspondent_years,
    lost_touch,
    profile_summary,
    top_domains,
    top_recipients,
    top_senders,
    yearly_activity,
)
from gmail_archive.config import Settings
from gmail_archive.parser import extract_html_body, iter_attachment_payloads
from gmail_archive.query import (
    CATEGORY_TABS,
    DEFAULT_SEARCH_SORT,
    MAILBOXES,
    SEARCH_SORTS,
    SYSTEM_LABELS,
    LabelCount,
    database_stats,
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
from gmail_archive.web.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    LoginThrottle,
    issue_session,
    verify_password,
    verify_session,
)
from gmail_archive.web.filters import (
    defang,
    filesize,
    gmail_date,
    highlight_snippet,
    relative_date,
    sender_name,
)

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent

#: The most rows any page will render, whatever the query string asks for.
#:
#: `limit` used to go straight into SQL. `?limit=200000` really did render
#: 200,000 rows: 51 seconds and a 210 MB response, measured. The pool caps at
#: 8 connections, so eight such requests make the UI unavailable to everyone —
#: and a bookmarked URL with a fat limit does it by accident.
#:
#: 200 is generous against a 50-row default and keeps the worst case well
#: under a second.
MAX_PAGE_LIMIT = 200

#: The deepest OFFSET a search will honour.
#:
#: Postgres walks and discards every preceding row, so a large offset is the
#: same denial of service wearing a different hat. Measured on a query with
#: ~18,000 matches: offset 0 is 0.23s, 1,000 is 2.1s, 5,000 is 9.0s. The first
#: cap tried here was 10,000, which is still multiple seconds — a bound that
#: only prevents the very worst case is not much of a bound.
#:
#: 1,000 is page six at the maximum page size. Going deeper than that in a
#: ranked result set is not really browsing, it is scraping.
#:
#: The real fix is keyset pagination, which the mailbox already uses and which
#: search could use for its two date orderings — only `relevance` genuinely
#: needs an offset, because rank is not a stable sort key.
MAX_SEARCH_OFFSET = 1_000


def _page_limit(value: int) -> int:
    """Clamp a caller-supplied page size into something serveable."""
    return max(1, min(value, MAX_PAGE_LIMIT))


#: A content hash is 64 lowercase hex characters and nothing else. Checked
#: before the blob store sees it: `BlobStore.path_for` raises ValueError on a
#: wrong-length string, which would surface as a 500 rather than a 404.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _asset_version() -> str:
    """A short fingerprint of the static assets, for cache-busting URLs.

    Without this, `StaticFiles` sends no `Cache-Control`, so browsers apply
    heuristic freshness and happily serve a cached stylesheet against freshly
    deployed HTML. The failure mode is nasty because it is silent and partial:
    new markup styled by old CSS, which looks like a layout bug rather than a
    stale file.

    Computed once at import from the files' size and mtime, so a rebuilt image
    always produces a new URL and an unchanged one keeps its cache.
    """
    fingerprint = hashlib.sha256()
    for name in sorted(("style.css", "htmx.min.js")):
        path = HERE / "static" / name
        if path.is_file():
            stat = path.stat()
            fingerprint.update(f"{name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return fingerprint.hexdigest()[:12]


ASSET_VERSION = _asset_version()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Return the pool's connections on shutdown.

    Startup is deliberately not here: the pool is created lazily on first use,
    because `TestClient(app)` outside a `with` block never runs lifespan, and
    because the environment has to be read at request time for tests that
    monkeypatch it.
    """
    yield
    _close_pool()


app = FastAPI(title="gmail-archive", docs_url="/docs", lifespan=_lifespan)
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.globals["asset_version"] = ASSET_VERSION
templates.env.filters["relative_date"] = relative_date
templates.env.filters["gmail_date"] = gmail_date
templates.env.filters["sender_name"] = sender_name
templates.env.filters["defang"] = defang
templates.env.filters["filesize"] = filesize
templates.env.filters["highlight_snippet"] = highlight_snippet
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# ── Authentication ─────────────────────────────────────────────────

#: Paths served without a session. An allowlist, not a blocklist, because
#: routes keep being added — /people, /trends and the attachment route all
#: appeared in one week — and a blocklist leaks each new one until someone
#: remembers. Everything not named here requires authentication.
_PUBLIC_PATHS = frozenset({"/login", "/logout", "/healthz"})
_PUBLIC_PREFIXES = ("/static/",)

_throttle = LoginThrottle()


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


def _client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def require_authentication(request: Request, call_next: Any) -> Response:
    """Deny by default.

    With no password configured the archive is served open, as it always has
    been — refusing to start would break a running deployment on upgrade. It
    says so loudly in the log and in the UI instead.
    """
    settings = Settings.from_env()
    password_hash = settings.web_password_hash

    if not password_hash or _is_public(request.url.path):
        response: Response = await call_next(request)
        return response

    if verify_session(request.cookies.get(SESSION_COOKIE), password_hash):
        authorised: Response = await call_next(request)
        return authorised

    # Send a browser to the login page, but give anything else a bare 401 —
    # a redirect to HTML is a confusing answer to a scripted request.
    accepts_html = "text/html" in request.headers.get("accept", "")
    if accepts_html:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(
            f"/login?next={quote(target, safe='')}", status_code=303
        )
    return JSONResponse({"detail": "Authentication required"}, status_code=401)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> HTMLResponse:
    if not Settings.from_env().web_password_hash:
        return RedirectResponse("/", status_code=303)  # type: ignore[return-value]
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next)})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request) -> Response:
    settings = Settings.from_env()
    password_hash = settings.web_password_hash
    if not password_hash:
        return RedirectResponse("/", status_code=303)

    # Parsed by hand rather than with `request.form()`, which needs
    # python-multipart. The login form is two urlencoded fields; a dependency
    # for that is not worth it in a project this deliberate about its
    # dependency surface.
    fields = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    password = fields.get("password", [""])[0]
    target = _safe_next(fields.get("next", ["/"])[0])
    client = _client_id(request)

    wait = _throttle.locked_for(client)
    if wait > 0:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": target, "error": f"Too many attempts. Wait {int(wait) + 1}s."},
            status_code=429,
        )

    if not verify_password(password, password_hash):
        _throttle.record_failure(client)
        logger.warning("failed web login from %s", client)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": target, "error": "Incorrect password."},
            status_code=401,
        )

    _throttle.record_success(client)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(password_hash),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        # Only when the request arrived over TLS. Setting it unconditionally
        # would make the cookie unusable on the plain-HTTP LAN setup this
        # normally runs as, and the browser would silently drop it.
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/logout")
def logout() -> Response:
    """Sign out. POST only (#48).

    This was a GET, which meant any page anywhere could sign you out with
    `<img src="http://archive.local:8000/logout">`. Pure nuisance — nothing
    can be read and the fix is to log in again — but a state-changing GET is
    the precondition for CSRF, and this was the app's only one. Making it POST
    turns "every GET here is safe" from a coincidence into a property.

    Note what this does and does not do. The cookie is deleted, so this
    browser forgets the session; the token itself stays valid until it
    expires, because validity is an HMAC over an expiry rather than
    server-side state. That is the trade that makes the cookie stateless, and
    on a single-user archive it is a fair one — but it means logout is "forget
    this device", not "revoke everywhere". The help panel says so, since the
    button alone implies otherwise.
    """
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


def _safe_next(target: str) -> str:
    """Only ever redirect within this app.

    Parsed rather than pattern-matched. The previous version rejected a
    leading `//` but accepted `/\\evil.example` — browsers normalise a
    backslash to a slash in the authority position, so that is an off-site
    redirect wearing a local-looking prefix.

    The property is "no scheme and no host", and "starts with a slash" was
    only ever a proxy for it. Proxies for security properties are how that
    bug survived being written and reviewed.
    """
    # Normalise the way a browser will, then check what it will actually see.
    # Two normalisations matter, and skipping either leaves a hole:
    #   `\` becomes `/` in the authority position, so `/\evil.example` is
    #   `//evil.example`; and dot segments are removed, so `/..//evil.example`
    #   collapses to `//evil.example` — protocol-relative, and off-site.
    parts = urlsplit(target.replace("\\", "/"))
    if parts.scheme or parts.netloc:
        return "/"
    path = posixpath.normpath(parts.path)
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    return urlunsplit(("", "", path, parts.query, ""))


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

    # Static assets are served from versioned URLs (see `_asset_version`), so a
    # versioned request can be cached hard — the URL changes when the file
    # does. An unversioned request gets `no-cache`, which still allows a 304
    # via the ETag but never lets a browser serve a stale copy without asking.
    if request.url.path.startswith("/static/"):
        if request.query_params.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
    return response


# ── Helpers ───────────────────────────────────────────────────────


#: The pool, created on first use rather than at import or in a lifespan hook.
#: Lazily, because `Settings.from_env()` must be read at request time for the
#: tests that monkeypatch the environment, and because `TestClient(app)` used
#: without a `with` block never runs lifespan at all.
_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

#: What a route treats as "the database is unavailable". `PoolTimeout` is not a
#: `psycopg.Error`, so without naming it here an exhausted or unreachable pool
#: would surface as a 500 rather than the 503 every route already handles.
DB_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error, PoolTimeout)


def _get_pool() -> ConnectionPool:
    """The process-wide connection pool.

    Every route used to call `psycopg.connect()` and throw the connection away
    — about 10ms of TCP and backend fork on every page view, and one Postgres
    backend per concurrent request, which makes `max_connections` the
    concurrency limit for the UI. The IMAP backend has always pooled; the web
    app was the odd one out.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                settings = Settings.from_env()
                _pool = ConnectionPool(
                    settings.database_url,
                    min_size=1,
                    max_size=8,
                    # Fail a request rather than hanging it when the database
                    # is down: the routes render a 503 and the page still
                    # loads.
                    timeout=5.0,
                    open=True,
                )
    return _pool


def _get_conn() -> Any:
    """A pooled connection, as a context manager.

    Returned rather than yielded so callers keep the existing
    `with _get_conn() as conn:` shape; the pool returns the connection on exit
    instead of closing it.
    """
    settings = Settings.from_env()
    if not settings.database_url:
        # Short-circuit rather than letting the pool spend its timeout finding
        # out there is nowhere to connect to. Keeps an unconfigured instance
        # responsive, and keeps the unit suite fast.
        raise psycopg.OperationalError("GMAIL_ARCHIVE_DATABASE_URL is not set")
    return _get_pool().connection()


def _close_pool() -> None:
    """Return the pool's connections on the way out."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


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
        "authenticated": bool(Settings.from_env().web_password_hash),
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
    limit = _page_limit(limit)

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
    except DB_ERRORS:
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
    elif after_date_dt is not None and on_day is None:
        # The dated messages ran out. Undated ones sort last and a row
        # comparison against NULL never matches, so the walk used to simply
        # stop here — leaving ~2.7% of the archive stored, searchable, and
        # unreachable by browsing, with nothing on screen to say so (#15).
        #
        # `after_date` empty with the highest possible sha enters the NULL
        # tail, which `list_messages_keyset` already knows how to page.
        next_cursor = {"after_date": "", "after_sha": "f" * 64}

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


@app.get("/people", response_class=HTMLResponse)
def people_page(request: Request, kind: str = "human") -> HTMLResponse:
    """Who you talk to, and who just mails you.

    ``kind`` is human (default), bulk, or all. Human first because two thirds
    of this archive is not correspondence, and an unfiltered ranking of
    senders is a ranking of marketing departments.
    """
    if kind not in ("human", "bulk", "all"):
        kind = "human"
    selected = None if kind == "all" else kind

    try:
        with _get_conn() as conn:
            summary = profile_summary(conn)
            senders = top_senders(conn, kind=selected, limit=40)
            domains = top_domains(conn, kind=selected, limit=25)
            recipients = top_recipients(conn, limit=40)
            faded = lost_touch(conn, limit=20)
            context = _chrome(conn)
    except DB_ERRORS:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    context.update(
        {
            "kind": kind,
            "summary": summary,
            "senders": senders,
            "domains": domains,
            "recipients": recipients,
            "lost_touch": faded,
        }
    )
    return templates.TemplateResponse(request, "people.html", context)


@app.get("/people/{address}", response_class=HTMLResponse)
def correspondent_page(request: Request, address: str) -> HTMLResponse:
    """One correspondent: volume, span, and activity by year."""
    try:
        with _get_conn() as conn:
            profile = correspondent(conn, address)
            years = correspondent_years(conn, address) if profile else []
            context = _chrome(conn)
    except DB_ERRORS:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    if profile is None:
        raise HTTPException(status_code=404, detail="No such correspondent")

    context.update({"profile": profile, "years": years})
    return templates.TemplateResponse(request, "correspondent.html", context)


@app.get("/trends", response_class=HTMLResponse)
def trends_page(request: Request) -> HTMLResponse:
    """Activity by year: the shape of 22 years of mail."""
    try:
        with _get_conn() as conn:
            years = yearly_activity(conn)
            context = _chrome(conn)
    except DB_ERRORS:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    context.update(
        {
            "years": years,
            "peak_sent": max((y.sent for y in years), default=0),
            "peak_received": max((y.received for y in years), default=0),
            "peak_people": max((y.people_mailed for y in years), default=0),
        }
    )
    return templates.TemplateResponse(request, "trends.html", context)


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request) -> HTMLResponse:
    """Archive statistics — the dashboard that used to be the front door."""
    try:
        with _get_conn() as conn:
            s = stats(conn)
            db = database_stats(conn)
            context = _chrome(conn)
    except DB_ERRORS:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )
    context["stats"] = s
    context["db"] = db
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/messages/{sha256}", response_class=HTMLResponse)
def message_detail(request: Request, sha256: str) -> HTMLResponse:
    """Full message detail with body rendering and parse warnings."""
    try:
        with _get_conn() as conn:
            msg = get_message_full(conn, sha256)
            context = _chrome(conn)
    except DB_ERRORS:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Database is unavailable."},
            status_code=503,
        )

    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    # Re-derived from the blob rather than read from a column (#32). The HTML
    # body was ~1.7 GB of a 6.0 GB database and every byte of it was already
    # here, in the raw message. This page was reading the blob anyway to list
    # attachments, so the extra cost is parsing, not I/O.
    #
    # A missing blob is not a 404: the message row is real and everything else
    # on the page — headers, labels, attachments, the plain-text body — is
    # still worth showing. `verify` is the thing that reports a gap in the
    # store; a detail page should not be the first place a user learns of one.
    try:
        raw_html = extract_html_body(_get_store().get(sha256))
    except (FileNotFoundError, ValueError):
        raw_html = ""

    # Sanitize first, then defang: nh3 decides what markup survives, and
    # defanging then neutralises every URL that survived with it. Doing it in
    # the other order would let nh3 re-normalise a defanged attribute.
    sanitized_html = defang(nh3.clean(raw_html)) if raw_html else ""

    context.update({"msg": msg, "sanitized_html": sanitized_html})
    return templates.TemplateResponse(request, "message.html", context)


@app.get("/thread/{thread_id}", response_class=HTMLResponse)
def thread_view(request: Request, thread_id: str) -> HTMLResponse:
    """All messages in a thread, ordered by date."""
    try:
        with _get_conn() as conn:
            msgs = get_thread_messages(conn, thread_id)
            context = _chrome(conn)
    except DB_ERRORS:
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
    limit = _page_limit(limit)
    offset = max(0, min(offset, MAX_SEARCH_OFFSET))

    try:
        with _get_conn() as conn:
            result = search(conn, q, limit=limit, offset=offset, sort=sort)
            context = _chrome(conn)
    except DB_ERRORS:
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
            "parsed": result.parsed,
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
    except DB_ERRORS:
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
    except DB_ERRORS:
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


#: Anything that could make a saved filename escape its directory or confuse a
#: shell. The archive stores the filename exactly as the sender declared it —
#: including `../../etc/passwd`, which the fixture generator produces on
#: purpose — so it is sanitised at the moment of serving, never before.
_UNSAFE_FILENAME_RE = re.compile(r'[/\\:\x00-\x1f"\']')


def _safe_filename(name: str | None, fallback: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name or "").strip(". ")
    return cleaned[:120] or fallback


@app.get("/messages/{sha256}/attachments/{index}")
def message_attachment(sha256: str, index: int) -> Response:
    """Serve one attachment, re-extracted from the raw message.

    Ingest records an attachment's metadata but not its bytes: the raw message
    already holds them, and storing them twice would roughly double the
    archive. So the message is re-parsed here and the part is pulled out by
    the same index ingest assigned — see `parser.iter_attachment_payloads`,
    which shares its predicate with `parse()` precisely so the numbering
    cannot drift.

    Served as `application/octet-stream` with `Content-Disposition:
    attachment`, regardless of the declared type. A twenty-year archive
    contains plenty of mail whose Content-Type is wrong, wishful, or hostile,
    and none of it is worth handing to a browser to render.
    """
    if not _SHA256_RE.fullmatch(sha256) or index < 0:
        raise HTTPException(status_code=404, detail="Attachment not found")

    store = _get_store()
    try:
        raw = store.get(sha256)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Message not found") from None

    for part_index, attachment, payload in iter_attachment_payloads(raw):
        if part_index == index:
            filename = _safe_filename(attachment.filename, f"attachment-{index}")
            return Response(
                content=payload,
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )

    raise HTTPException(status_code=404, detail="Attachment not found")


@app.get("/raw/{sha256}")
def raw_message(sha256: str) -> Response:
    """Download the raw RFC822 bytes of a message.

    Served with ``Content-Disposition: attachment`` so the browser never
    renders it inline, regardless of MIME type.
    """
    # Validated before the blob store sees it: `path_for` raises ValueError on
    # anything that is not 64 characters, which would surface as a 500 rather
    # than the 404 a malformed URL deserves.
    if not _SHA256_RE.fullmatch(sha256):
        raise HTTPException(status_code=404, detail="Message not found")

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
