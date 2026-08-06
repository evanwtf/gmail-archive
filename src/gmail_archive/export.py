"""Export archived messages as mbox or .eml files.

Reconstitutes messages from the content-addressed blob store. The raw bytes
stored are the unquoted RFC822 message; mbox export re-quotes ``>From `` lines
so the output is valid mboxrd.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

from gmail_archive.storage import BlobStore

logger = logging.getLogger(__name__)

#: Mboxrd separator line. The leading `From ` is the mbox message delimiter;
#: the remainder is the envelope sender and a timestamp.
_MBOX_SEP = b"From MAILER-DAEMON@archive  Thu Jan  1 00:00:00 1970\n"


def _requote(body: bytes) -> bytes:
    """Re-quote ``>From `` lines for mboxrd format.

    The blob store holds unquoted RFC822 bytes. Mboxrd requires that any line
    starting with ``From `` be prefixed with ``>``, and any line starting with
    ``>From `` be prefixed with another ``>``, etc.
    """
    lines = body.split(b"\n")
    out: list[bytes] = []
    for line in lines:
        if line.startswith(b"From ") or line.startswith(b">From "):
            out.append(b">" + line)
        else:
            out.append(line)
    return b"\n".join(out)


def export_mbox(
    conn: psycopg.Connection[object],
    store: BlobStore,
    output: Path,
    *,
    label: str | None = None,
    query: str | None = None,
    limit: int | None = None,
) -> int:
    """Export messages as an mbox file.

    Args:
        conn: Database connection.
        store: Blob store instance.
        output: Path to write the mbox file.
        label: Optional label to filter by.
        query: Optional full-text search query to filter by.
        limit: Maximum number of messages to export.

    Returns:
        Number of messages exported.
    """
    # Build the query to get raw_sha256 values.
    conditions: list[str] = []
    params: list[str | int] = []

    if label is not None:
        conditions.append(
            "exists (select 1 from labels l"
            " where l.raw_sha256 = m.raw_sha256 and l.label = %s)"
        )
        params.append(label)

    if query is not None:
        conditions.append("m.search_tsv @@ websearch_to_tsquery('english', %s)")
        params.append(query)

    where_clause = ""
    if conditions:
        where_clause = "where " + " and ".join(conditions)

    limit_clause = ""
    if limit is not None:
        limit_clause = "limit %s"
        params.append(limit)

    sql = (
        "select m.raw_sha256 from messages m"
        f" {where_clause}"
        " order by m.internal_date desc nulls last, m.raw_sha256 desc"
        f" {limit_clause}"
    )

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        logger.info("no messages match the filter criteria")
        return 0

    count = 0
    with output.open("wb") as fh:
        for row in rows:
            sha256 = str(row[0])  # type: ignore[index]
            try:
                body = store.get(sha256)
            except FileNotFoundError:
                logger.warning("blob missing for %s, skipping", sha256)
                continue

            fh.write(_MBOX_SEP)
            fh.write(_requote(body))
            fh.write(b"\n")
            count += 1

    logger.info("exported %d messages to %s", count, output)
    return count


def export_eml(
    conn: psycopg.Connection[object],
    store: BlobStore,
    output_dir: Path,
    *,
    label: str | None = None,
    query: str | None = None,
    limit: int | None = None,
) -> int:
    """Export messages as individual .eml files.

    Each file is named ``<raw_sha256>.eml``.

    Args:
        conn: Database connection.
        store: Blob store instance.
        output_dir: Directory to write .eml files into.
        label: Optional label to filter by.
        query: Optional full-text search query to filter by.
        limit: Maximum number of messages to export.

    Returns:
        Number of messages exported.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build the query to get raw_sha256 values.
    conditions: list[str] = []
    params: list[str | int] = []

    if label is not None:
        conditions.append(
            "exists (select 1 from labels l"
            " where l.raw_sha256 = m.raw_sha256 and l.label = %s)"
        )
        params.append(label)

    if query is not None:
        conditions.append("m.search_tsv @@ websearch_to_tsquery('english', %s)")
        params.append(query)

    where_clause = ""
    if conditions:
        where_clause = "where " + " and ".join(conditions)

    limit_clause = ""
    if limit is not None:
        limit_clause = "limit %s"
        params.append(limit)

    sql = (
        "select m.raw_sha256 from messages m"
        f" {where_clause}"
        " order by m.internal_date desc nulls last, m.raw_sha256 desc"
        f" {limit_clause}"
    )

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        logger.info("no messages match the filter criteria")
        return 0

    count = 0
    for row in rows:
        sha256 = str(row[0])  # type: ignore[index]
        try:
            body = store.get(sha256)
        except FileNotFoundError:
            logger.warning("blob missing for %s, skipping", sha256)
            continue

        out_path = output_dir / f"{sha256}.eml"
        out_path.write_bytes(body)
        count += 1

    logger.info("exported %d messages to %s", count, output_dir)
    return count
