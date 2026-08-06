"""Resumable, idempotent mbox ingest pipeline.

Reads a Google Takeout mbox file, parses every message, stores raw bytes in the
content-addressed blob store, and writes derived metadata to Postgres. Designed
to survive a container kill mid-run: the checkpoint lives in the database, not
in a sidecar file.

Architecture
------------

    mbox file ──→ scan boundaries ──→ process pool ──→ batch COPY ──→ Postgres
                    (sequential)        (parallel)       (periodic)

1. **Scan** — the main process scans the mbox for `From_` separators (cheap,
   sequential) and builds a list of `(offset, length)` ranges.

2. **Dispatch** — ranges are sent to a process pool. Each worker `pread`s its
   own range, strips the envelope, hashes, parses, writes the blob, and returns
   only the small metadata dict.

3. **Batch** — the main process collects results and, at `batch_size` boundaries,
   writes them to Postgres via `COPY` and updates the checkpoint.

4. **Resume** — on restart, any message whose byte offset is before the
   checkpoint is skipped. Idempotency is enforced by `ON CONFLICT DO NOTHING`
   at the row level, so even a partial batch replay is safe.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from gmail_archive.config import Settings
from gmail_archive.mbox import read_message, scan, strip_envelope
from gmail_archive.parser import parse
from gmail_archive.storage import BlobStore

logger = logging.getLogger(__name__)

# Maximum bytes of raw message to store in failed_messages.raw_prefix. Capped
# so a handful of 25 MB failures does not bloat pg_dump.
_FAILED_PREFIX_MAX = 8192


@dataclass
class IngestReport:
    """Summary of an ingest run."""

    source_path: str
    messages_seen: int
    messages_new: int
    messages_duplicate: int
    failures: int
    elapsed_seconds: float
    run_id: int | None = None


@dataclass
class WorkerResult:
    """Result from a single worker task."""

    offset: int
    length: int
    success: bool
    # On success
    metadata: dict[str, Any] | None = None
    # On failure
    error: str | None = None
    traceback_str: str | None = None
    raw_prefix: bytes | None = None


def _worker_task(
    offset: int,
    length: int,
    mbox_path: str,
    blob_dir: str,
) -> WorkerResult:
    """Process one message in a worker process.

    This is a module-level function so it is picklable by multiprocessing.
    Each worker opens the mbox file independently, reads its range, hashes,
    parses, writes the blob, and returns metadata.
    """
    mbox = Path(mbox_path)
    store = BlobStore(Path(blob_dir))

    try:
        raw = read_message(mbox, offset, length)
        body_bytes = strip_envelope(raw)
        parsed = parse(body_bytes, already_unquoted=True)

        # Write the blob (idempotent — no-op if already present).
        blob_result = store.put(body_bytes, sha256=parsed.raw_sha256)

        metadata: dict[str, Any] = {
            "raw_sha256": parsed.raw_sha256,
            "size_bytes": parsed.size_bytes,
            "message_id": parsed.message_id,
            "gmail_id": parsed.gmail_id,
            "thread_id": parsed.thread_id,
            "subject": parsed.subject,
            "from_addr": parsed.from_addr,
            "to_addrs": parsed.to_addrs,
            "cc_addrs": parsed.cc_addrs,
            "bcc_addrs": parsed.bcc_addrs,
            "reply_to": parsed.reply_to,
            "in_reply_to": parsed.in_reply_to,
            "references_ids": parsed.references_ids,
            "internal_date": parsed.internal_date,
            "labels": parsed.labels,
            "body_text": parsed.body_text,
            "body_html": parsed.body_html,
            "search_text": parsed.search_text,
            "attachments": [
                {
                    "part_index": i,
                    "filename": a.filename,
                    "mime_type": a.mime_type,
                    "size_bytes": a.size,
                    "content_sha256": a.sha256,
                }
                for i, a in enumerate(parsed.attachments)
            ],
            "parse_warnings": json.dumps(
                [{"code": w.code.value, "detail": w.detail}
                 for w in parsed.parse_warnings]
            ),
            "blob_written": blob_result.written,
        }

        return WorkerResult(
            offset=offset,
            length=length,
            success=True,
            metadata=metadata,
        )
    except Exception as exc:
        tb = traceback.format_exc()
        prefix = raw[: _FAILED_PREFIX_MAX] if "raw" in dir() else b""
        return WorkerResult(
            offset=offset,
            length=length,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
            traceback_str=tb,
            raw_prefix=prefix,
        )


def _ensure_run(
    conn: psycopg.Connection[object],
    source_path: str,
) -> tuple[int, int]:
    """Find or create an ingest run. Returns (run_id, checkpoint_offset)."""
    # Look for an incomplete run for this source file.
    row = conn.execute(
        "select id, checkpoint_offset from ingest_runs "
        "where source_path = %s and status in ('running', 'interrupted') "
        "order by started_at desc limit 1",
        (source_path,),
    ).fetchone()

    if row is not None:
        run_id, checkpoint = int(row[0]), int(row[1])  # type: ignore[index]
        logger.info(
            "resuming run %d for %s from checkpoint %d",
            run_id,
            source_path,
            checkpoint,
        )
        return run_id, checkpoint

    # Create a new run.
    row = conn.execute(
        "insert into ingest_runs (source_path, status) "
        "values (%s, 'running') returning id",
        (source_path,),
    ).fetchone()
    run_id = int(row[0])  # type: ignore[index]
    logger.info("created run %d for %s", run_id, source_path)
    return run_id, 0


def _write_batch(
    conn: psycopg.Connection[object],
    run_id: int,
    source_path: str,
    results: list[WorkerResult],
) -> None:
    """Write a batch of worker results to Postgres in a single transaction."""
    successes = [r for r in results if r.success and r.metadata is not None]
    failures = [r for r in results if not r.success]

    if not successes and not failures:
        return

    # Extract metadata dicts — mypy cannot narrow r.metadata through a list
    # comprehension filter, so we build a parallel list.
    meta: list[Any] = [r.metadata for r in successes]

    with conn.transaction():
        # ── blobs ────────────────────────────────────────────────────────
        blob_rows = [(m["raw_sha256"], m["size_bytes"], "message") for m in meta]
        if blob_rows:
            with conn.cursor().copy(
                "copy blobs (sha256, size_bytes, kind) from stdin"
            ) as copy:
                for blob_row in blob_rows:
                    copy.write_row(blob_row)

        # ── messages ────────────────────────────────────────────────────
        msg_rows = [
            (
                m["raw_sha256"],
                m["size_bytes"],
                m["message_id"],
                m["gmail_id"],
                m["thread_id"],
                m["subject"],
                m["from_addr"],
                m["to_addrs"],
                m["cc_addrs"],
                m["bcc_addrs"],
                m["reply_to"],
                m["in_reply_to"],
                m["references_ids"],
                m["internal_date"],
                m["body_text"],
                m["body_html"],
                m["search_text"],
                m["parse_warnings"],
            )
            for m in meta
        ]
        if msg_rows:
            with conn.cursor().copy(
                "copy messages ("
                "raw_sha256, size_bytes, message_id, gmail_id, thread_id, "
                "subject, from_addr, to_addrs, cc_addrs, bcc_addrs, "
                "reply_to, in_reply_to, references_ids, internal_date, "
                "body_text, body_html, search_text, parse_warnings"
                ") from stdin"
            ) as copy:
                for msg_row in msg_rows:
                    copy.write_row(msg_row)

        # ── labels ──────────────────────────────────────────────────────
        label_rows = [
            (m["raw_sha256"], label)
            for m in meta
            for label in m["labels"]
        ]
        if label_rows:
            with conn.cursor().copy(
                "copy labels (raw_sha256, label) from stdin"
            ) as copy:
                for label_row in label_rows:
                    copy.write_row(label_row)

        # ── attachments ──────────────────────────────────────────────────
        attach_rows = [
            (m["raw_sha256"], a["part_index"], a["filename"], a["mime_type"],
             a["size_bytes"], a["content_sha256"], None)
            for m in meta
            for a in m["attachments"]
        ]
        if attach_rows:
            with conn.cursor().copy(
                "copy attachments (raw_sha256, part_index, filename, "
                "mime_type, size_bytes, content_sha256, blob_sha256) "
                "from stdin"
            ) as copy:
                for attach_row in attach_rows:
                    copy.write_row(attach_row)

        # ── message_sightings ────────────────────────────────────────────
        sighting_rows = [
            (m["raw_sha256"], source_path, r.offset, r.length)
            for r, m in zip(successes, meta, strict=True)
        ]
        if sighting_rows:
            with conn.cursor().copy(
                "copy message_sightings (raw_sha256, source_path, "
                "byte_offset, byte_length) from stdin"
            ) as copy:
                for sighting_row in sighting_rows:
                    copy.write_row(sighting_row)

        # ── failed_messages ──────────────────────────────────────────────
        failed_rows = [
            (run_id, source_path, r.offset, r.length, r.error, r.traceback_str,
             r.raw_prefix, r.raw_prefix is not None and len(r.raw_prefix) < r.length)
            for r in failures
        ]
        if failed_rows:
            with conn.cursor().copy(
                "copy failed_messages (run_id, source_path, byte_offset, "
                "byte_length, error, traceback, raw_prefix, truncated) "
                "from stdin"
            ) as copy:
                for failed_row in failed_rows:
                    copy.write_row(failed_row)


def _checkpoint(
    conn: psycopg.Connection[object],
    run_id: int,
    checkpoint_offset: int,
    messages_seen: int,
    messages_new: int,
    failures: int,
) -> None:
    """Update the run's checkpoint and counters."""
    conn.execute(
        "update ingest_runs set checkpoint_offset = %s, "
        "messages_seen = %s, messages_new = %s, failures = %s "
        "where id = %s",
        (checkpoint_offset, messages_seen, messages_new, failures, run_id),
    )


def _finalize_run(
    conn: psycopg.Connection[object],
    run_id: int,
    status: str,
    messages_seen: int,
    messages_new: int,
    failures: int,
) -> None:
    """Mark a run as complete, failed, or interrupted."""
    conn.execute(
        "update ingest_runs set status = %s, finished_at = now(), "
        "messages_seen = %s, messages_new = %s, failures = %s "
        "where id = %s",
        (status, messages_seen, messages_new, failures, run_id),
    )


def ingest(
    settings: Settings,
    mbox_path: Path,
    *,
    workers: int | None = None,
    batch_size: int | None = None,
) -> IngestReport:
    """Ingest an mbox file into Postgres.

    Args:
        settings: Application settings.
        mbox_path: Path to the mbox file.
        workers: Worker count (defaults to settings.workers).
        batch_size: Messages per batch (defaults to settings.batch_size).

    Returns:
        An IngestReport summarising the run.
    """
    start = datetime.now(UTC)
    mbox_path = mbox_path.resolve()
    source_path = str(mbox_path)
    n_workers = workers or settings.workers
    n_batch = batch_size or settings.batch_size
    store = BlobStore(settings.blob_dir)

    # Sweep any temporaries from a previous interrupted run.
    store.sweep_temporaries()

    # Scan the mbox for message boundaries.
    logger.info("scanning %s", source_path)
    mbox_scan = scan(mbox_path)
    total = mbox_scan.message_count
    logger.info("found %d messages in %s", total, source_path)

    if total == 0:
        return IngestReport(
            source_path=source_path,
            messages_seen=0,
            messages_new=0,
            messages_duplicate=0,
            failures=0,
            elapsed_seconds=0.0,
        )

    # ── Database setup ──────────────────────────────────────────────────
    conn = psycopg.connect(settings.database_url)
    run_id = 0
    messages_seen = 0
    messages_new = 0
    failures = 0
    try:
        run_id, checkpoint = _ensure_run(conn, source_path)

        # Filter to messages after the checkpoint.
        pending = [
            (offset, length)
            for offset, length in mbox_scan.offsets
            if offset >= checkpoint
        ]
        skipped = total - len(pending)
        logger.info(
            "resuming at offset %d (%d messages skipped, %d pending)",
            checkpoint,
            skipped,
            len(pending),
        )

        if not pending:
            _finalize_run(
                conn, run_id, "complete",
                messages_seen=total, messages_new=0, failures=0,
            )
            return IngestReport(
                source_path=source_path,
                messages_seen=total,
                messages_new=0,
                messages_duplicate=0,
                failures=0,
                elapsed_seconds=0.0,
                run_id=run_id,
            )

        # ── Process pool ────────────────────────────────────────────────
        ctx = multiprocessing.get_context("spawn")
        messages_seen = skipped
        messages_new = 0
        failures = 0
        batch: list[WorkerResult] = []

        with ctx.Pool(n_workers) as pool:
            tasks = [
                (offset, length, source_path, str(settings.blob_dir))
                for offset, length in pending
            ]
            results_iter = pool.starmap_async(
                _worker_task, tasks, chunksize=1
            )

            for result in results_iter.get():
                messages_seen += 1
                batch.append(result)

                if (
                    result.success
                    and result.metadata
                    and result.metadata["blob_written"]
                ):
                    messages_new += 1

                if not result.success:
                    failures += 1

                # Flush batch at batch_size boundary or at end.
                if len(batch) >= n_batch:
                    _write_batch(conn, run_id, source_path, batch)
                    # Checkpoint at the last offset in the batch.
                    last_offset = batch[-1].offset + batch[-1].length
                    _checkpoint(
                        conn, run_id, last_offset,
                        messages_seen, messages_new, failures,
                    )
                    logger.info(
                        "checkpoint: %d/%d messages, %d new, %d failures",
                        messages_seen, total, messages_new, failures,
                    )
                    batch = []

        # Flush remaining batch.
        if batch:
            _write_batch(conn, run_id, source_path, batch)
            last_offset = batch[-1].offset + batch[-1].length
            _checkpoint(
                conn, run_id, last_offset,
                messages_seen, messages_new, failures,
            )

        # Mark the run as complete.
        _finalize_run(
            conn, run_id, "complete",
            messages_seen, messages_new, failures,
        )

        elapsed = (datetime.now(UTC) - start).total_seconds()
        logger.info(
            "ingest complete: %d seen, %d new, %d failures in %.1fs",
            messages_seen, messages_new, failures, elapsed,
        )

        return IngestReport(
            source_path=source_path,
            messages_seen=messages_seen,
            messages_new=messages_new,
            messages_duplicate=messages_seen - skipped - messages_new - failures,
            failures=failures,
            elapsed_seconds=elapsed,
            run_id=run_id,
        )
    except BaseException:
        # Try to mark the run as interrupted so it can be resumed.
        try:
            _finalize_run(
                conn, run_id, "interrupted",
                messages_seen, messages_new, failures,
            )
        except Exception:
            logger.exception("failed to mark run as interrupted")
        raise
    finally:
        conn.close()
