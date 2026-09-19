"""Read-only query surface for the archive.

This module is the **only** place allowed to build read SQL against the `messages`
table. The CLI, the web UI, and any future IMAP SEARCH all go through here. A
test greps for stray SQL against an explicit allowlist and fails.

The keyset pagination ordering must match the `messages_keyset_idx` index exactly:
`(internal_date desc nulls last, raw_sha256 desc)`. A mismatch means a sequential
scan over the whole archive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


def _row(row: object) -> tuple[Any, ...]:
    """Cast a psycopg Row to a tuple for indexing.

    psycopg's type stubs return `object` from fetchone/fetchall, which mypy
    refuses to index. This cast is safe because psycopg rows always support
    tuple-like access.
    """
    return tuple(row)  # type: ignore[arg-type]


@dataclass
class ArchiveStats:
    """Aggregate statistics about the archive."""

    total_messages: int
    total_blobs: int
    total_attachments: int
    total_labels: int
    total_failures: int
    total_runs: int
    total_bytes: int
    date_earliest: datetime | None
    date_latest: datetime | None
    blob_bytes: int


@dataclass
class MessageRow:
    """One row from a message listing or search result."""

    raw_sha256: str
    subject: str | None
    from_addr: str | None
    to_addrs: list[str]
    internal_date: datetime | None
    thread_id: str | None
    snippet: str = ""


@dataclass
class SearchResult:
    """Result of a full-text search."""

    messages: list[MessageRow]
    total: int
    query: str


def stats(conn: psycopg.Connection[object]) -> ArchiveStats:
    """Return aggregate statistics about the archive."""
    raw = conn.execute(
        "select"
        "  (select count(*) from messages) as total_messages,"
        "  (select count(*) from blobs) as total_blobs,"
        "  (select count(*) from attachments) as total_attachments,"
        "  (select count(*) from labels) as total_labels,"
        "  (select count(*) from failed_messages) as total_failures,"
        "  (select count(*) from ingest_runs) as total_runs,"
        "  (select coalesce(sum(size_bytes), 0) from messages) as total_bytes,"
        "  (select min(internal_date) from messages) as date_earliest,"
        "  (select max(internal_date) from messages) as date_latest,"
        "  (select coalesce(sum(size_bytes), 0) from blobs) as blob_bytes"
    ).fetchone()
    assert raw is not None
    row = _row(raw)
    return ArchiveStats(
        total_messages=int(row[0]),
        total_blobs=int(row[1]),
        total_attachments=int(row[2]),
        total_labels=int(row[3]),
        total_failures=int(row[4]),
        total_runs=int(row[5]),
        total_bytes=int(row[6]),
        date_earliest=row[7],
        date_latest=row[8],
        blob_bytes=int(row[9]),
    )


def search(
    conn: psycopg.Connection[object],
    query: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    """Full-text search over messages using `websearch_to_tsquery`.

    Returns messages ranked by relevance, with highlighted snippets.
    """
    if not query.strip():
        return SearchResult(messages=[], total=0, query=query)

    # Count first (cheap with the GIN index).
    raw_count = conn.execute(
        "select count(*) from messages "
        "where search_tsv @@ websearch_to_tsquery('english', %s)",
        (query,),
    ).fetchone()
    assert raw_count is not None
    total = int(_row(raw_count)[0])

    if total == 0:
        return SearchResult(messages=[], total=0, query=query)

    raw_rows = conn.execute(
        "select"
        "  raw_sha256,"
        "  subject,"
        "  from_addr,"
        "  to_addrs,"
        "  internal_date,"
        "  thread_id,"
        "  ts_headline("
        "    'english',"
        "    coalesce(subject, '') || ' ' || coalesce(search_text, ''),"
        "    websearch_to_tsquery('english', %s),"
        "    'MaxWords=40, MinWords=20, StartSel=[hl], StopSel=[/hl]'"
        "  ) as snippet"
        " from messages"
        " where search_tsv @@ websearch_to_tsquery('english', %s)"
        " order by ts_rank(search_tsv, websearch_to_tsquery('english', %s)) desc,"
        "  internal_date desc nulls last, raw_sha256 desc"
        " limit %s offset %s",
        (query, query, query, limit, offset),
    ).fetchall()

    messages = [
        MessageRow(
            raw_sha256=str(r[0]),
            subject=r[1],
            from_addr=r[2],
            to_addrs=list(r[3]) if r[3] else [],
            internal_date=r[4],
            thread_id=r[5],
            snippet=r[6] or "",
        )
        for r in (_row(rr) for rr in raw_rows)
    ]

    return SearchResult(messages=messages, total=total, query=query)


def list_messages(
    conn: psycopg.Connection[object],
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[MessageRow]:
    """List messages in keyset order, newest first.

    The ordering matches `messages_keyset_idx` exactly so the query planner
    can use the index rather than a sequential scan.
    """
    raw_rows = conn.execute(
        "select"
        "  raw_sha256, subject, from_addr, to_addrs, internal_date, thread_id"
        " from messages"
        " order by internal_date desc nulls last, raw_sha256 desc"
        " limit %s offset %s",
        (limit, offset),
    ).fetchall()

    return [
        MessageRow(
            raw_sha256=str(r[0]),
            subject=r[1],
            from_addr=r[2],
            to_addrs=list(r[3]) if r[3] else [],
            internal_date=r[4],
            thread_id=r[5],
        )
        for r in (_row(rr) for rr in raw_rows)
    ]


def get_message(
    conn: psycopg.Connection[object],
    raw_sha256: str,
) -> MessageRow | None:
    """Fetch a single message by its content hash."""
    raw = conn.execute(
        "select"
        "  raw_sha256, subject, from_addr, to_addrs, internal_date, thread_id"
        " from messages where raw_sha256 = %s",
        (raw_sha256,),
    ).fetchone()
    if raw is None:
        return None
    row = _row(raw)
    return MessageRow(
        raw_sha256=str(row[0]),
        subject=row[1],
        from_addr=row[2],
        to_addrs=list(row[3]) if row[3] else [],
        internal_date=row[4],
        thread_id=row[5],
    )
