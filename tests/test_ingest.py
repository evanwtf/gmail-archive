"""Ingest pipeline tests.

The ingest pipeline ties together the mbox splitter, parser, blob store, and
Postgres. Unit tests cover the bookkeeping functions; integration tests (gated
behind GMAIL_ARCHIVE_TEST_DATABASE_URL) exercise the full pipeline against a
real database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gmail_archive.config import Settings
from gmail_archive.ingest import (
    WorkerResult,
    _checkpoint,
    _ensure_run,
    _finalize_run,
    _write_batch,
    ingest,
)

DSN = os.environ.get("GMAIL_ARCHIVE_TEST_DATABASE_URL")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=DSN or "",
        blob_dir=tmp_path / "blobs",
        workers=2,
        batch_size=10,
        log_level="WARNING",
        imap_password="",
    )


def _mbox(tmp_path: Path, lines: list[bytes]) -> Path:
    """Write a minimal mbox file from message bodies."""
    path = tmp_path / "test.mbox"
    with open(path, "wb") as f:
        for i, body in enumerate(lines):
            f.write(
                f"From user{i}@e.com Mon Jan 01 00:00:00 2000\n".encode()
            )
            f.write(body)
    return path


# ── _ensure_run tests ────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestEnsureRun:
    def test_creates_a_new_run(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            run_id, checkpoint = _ensure_run(conn, "/tmp/test.mbox")
            assert run_id > 0
            assert checkpoint == 0
            # Clean up
            conn.execute("delete from ingest_runs where id = %s", (run_id,))

    def test_resumes_an_interrupted_run(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            # Create an interrupted run.
            conn.execute(
                "insert into ingest_runs (source_path, status, checkpoint_offset) "
                "values (%s, 'interrupted', 500)",
                ("/tmp/resume.mbox",),
            )
            run_id, checkpoint = _ensure_run(conn, "/tmp/resume.mbox")
            assert checkpoint == 500
            # Clean up
            conn.execute("delete from ingest_runs where id = %s", (run_id,))


# ── _checkpoint and _finalize_run tests ──────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestRunBookkeeping:
    def test_checkpoint_updates_offset(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            row = conn.execute(
                "insert into ingest_runs (source_path, status) "
                "values ('/tmp/cp.mbox', 'running') returning id"
            ).fetchone()
            run_id = int(row[0])  # type: ignore[index]
            _checkpoint(conn, run_id, 999, 50, 10, 2)
            row = conn.execute(
                "select checkpoint_offset, messages_seen, messages_new, failures "
                "from ingest_runs where id = %s", (run_id,)
            ).fetchone()
            assert row is not None
            assert int(row[0]) == 999
            assert int(row[1]) == 50
            assert int(row[2]) == 10
            assert int(row[3]) == 2
            conn.execute("delete from ingest_runs where id = %s", (run_id,))

    def test_finalize_marks_complete(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            row = conn.execute(
                "insert into ingest_runs (source_path, status) "
                "values ('/tmp/fin.mbox', 'running') returning id"
            ).fetchone()
            run_id = int(row[0])  # type: ignore[index]
            _finalize_run(conn, run_id, "complete", 100, 80, 5)
            row = conn.execute(
                "select status, finished_at from ingest_runs where id = %s",
                (run_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "complete"
            assert row[1] is not None
            conn.execute("delete from ingest_runs where id = %s", (run_id,))


# ── _write_batch tests ───────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestWriteBatch:
    def test_writes_successful_results(self) -> None:
        import hashlib

        import psycopg


        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            # Create a run.
            row = conn.execute(
                "insert into ingest_runs (source_path, status) "
                "values ('/tmp/batch.mbox', 'running') returning id"
            ).fetchone()
            run_id = int(row[0])  # type: ignore[index]

            # Create a successful result.
            body = b"Subject: test\n\nhello"
            sha256 = hashlib.sha256(body).hexdigest()
            result = WorkerResult(
                offset=0,
                length=len(body),
                success=True,
                metadata={
                    "raw_sha256": sha256,
                    "size_bytes": len(body),
                    "message_id": "<test@e.com>",
                    "gmail_id": None,
                    "thread_id": "thread1",
                    "subject": "test",
                    "from_addr": "a@e.com",
                    "to_addrs": ["b@e.com"],
                    "cc_addrs": [],
                    "bcc_addrs": [],
                    "reply_to": None,
                    "in_reply_to": None,
                    "references_ids": [],
                    "internal_date": None,
                    "labels": ["Inbox"],
                    "body_text": "hello",
                    "body_html": "",
                    "search_text": "test hello",
                    "attachments": [],
                    "parse_warnings": [],
                    "blob_written": True,
                },
            )

            _write_batch(conn, run_id, "/tmp/batch.mbox", [result])

            # Verify blob row.
            blob = conn.execute(
                "select sha256, size_bytes, kind from blobs where sha256 = %s",
                (sha256,),
            ).fetchone()
            assert blob is not None
            assert blob[0] == sha256

            # Verify message row.
            msg = conn.execute(
                "select raw_sha256, subject, from_addr from messages "
                "where raw_sha256 = %s", (sha256,)
            ).fetchone()
            assert msg is not None
            assert msg[1] == "test"

            # Verify label row.
            label = conn.execute(
                "select label from labels where raw_sha256 = %s", (sha256,)
            ).fetchone()
            assert label is not None
            assert label[0] == "Inbox"

            # Verify sighting row.
            sighting = conn.execute(
                "select byte_offset from message_sightings where raw_sha256 = %s",
                (sha256,),
            ).fetchone()
            assert sighting is not None
            assert int(sighting[0]) == 0

            # Clean up
            conn.execute(
                "delete from message_sightings where raw_sha256 = %s", (sha256,)
            )
            conn.execute("delete from labels where raw_sha256 = %s", (sha256,))
            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))
            conn.execute("delete from ingest_runs where id = %s", (run_id,))

    def test_writes_failed_results(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            row = conn.execute(
                "insert into ingest_runs (source_path, status) "
                "values ('/tmp/fail.mbox', 'running') returning id"
            ).fetchone()
            run_id = int(row[0])  # type: ignore[index]

            result = WorkerResult(
                offset=100,
                length=50,
                success=False,
                error="ValueError: bad data",
                traceback_str="Traceback (most recent call last):\n  ...",
                raw_prefix=b"From: bad\xffdata\n\nbody",
            )

            _write_batch(conn, run_id, "/tmp/fail.mbox", [result])

            failed = conn.execute(
                "select byte_offset, error, truncated from failed_messages "
                "where run_id = %s", (run_id,)
            ).fetchone()
            assert failed is not None
            assert int(failed[0]) == 100
            assert "ValueError" in failed[1]

            conn.execute("delete from failed_messages where run_id = %s", (run_id,))
            conn.execute("delete from ingest_runs where id = %s", (run_id,))


# ── Full pipeline integration test ───────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestIngest:
    def test_ingest_simple_mbox(self, tmp_path: Path) -> None:
        """Ingest a small mbox and verify the data made it into Postgres."""
        import psycopg

        mbox = _mbox(tmp_path, [
            b"Subject: first\n\nbody one\n",
            b"Subject: second\n\nbody two\n",
        ])
        settings = _settings(tmp_path)
        report = ingest(settings, mbox)

        assert report.messages_seen == 2
        assert report.messages_new == 2
        assert report.failures == 0
        assert report.run_id is not None

        # Verify in the database.
        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            count = conn.execute("select count(*) from messages").fetchone()
            assert count is not None
            assert int(count[0]) >= 2

            # Clean up all test data.
            conn.execute("delete from message_sightings")
            conn.execute("delete from labels")
            conn.execute("delete from attachments")
            conn.execute("delete from messages")
            conn.execute("delete from blobs")
            conn.execute("delete from ingest_runs")
            conn.execute("delete from failed_messages")

    def test_ingest_is_idempotent(self, tmp_path: Path) -> None:
        """Re-ingesting the same file must not duplicate rows."""
        import psycopg

        mbox = _mbox(tmp_path, [
            b"Subject: only\n\nbody\n",
        ])
        settings = _settings(tmp_path)

        ingest(settings, mbox)
        second = ingest(settings, mbox)

        assert second.messages_new == 0  # No new messages on re-ingest
        assert second.messages_seen == 1  # But we saw the message

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            count = conn.execute("select count(*) from messages").fetchone()
            assert count is not None
            assert int(count[0]) == 1

            conn.execute("delete from message_sightings")
            conn.execute("delete from labels")
            conn.execute("delete from messages")
            conn.execute("delete from blobs")
            conn.execute("delete from ingest_runs")

    def test_ingest_empty_mbox(self, tmp_path: Path) -> None:
        """An empty mbox must not crash."""
        mbox = _mbox(tmp_path, [])
        settings = _settings(tmp_path)
        report = ingest(settings, mbox)
        assert report.messages_seen == 0
        assert report.messages_new == 0
        assert report.failures == 0

    def test_ingest_with_parser_warnings(self, tmp_path: Path) -> None:
        """Messages with parser warnings should still be ingested."""
        import psycopg

        mbox = _mbox(tmp_path, [
            b"Subject: nul\n\nbefore\x00after\n",
        ])
        settings = _settings(tmp_path)
        report = ingest(settings, mbox)

        assert report.messages_seen == 1
        assert report.messages_new == 1
        assert report.failures == 0

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            row = conn.execute(
                "select parse_warnings from messages"
            ).fetchone()
            assert row is not None
            assert row[0] is not None

            conn.execute("delete from message_sightings")
            conn.execute("delete from labels")
            conn.execute("delete from messages")
            conn.execute("delete from blobs")
            conn.execute("delete from ingest_runs")
