"""Tests for the verify and export modules.

These are integration tests that require a running Postgres instance with the
schema applied. They skip cleanly when GMAIL_ARCHIVE_TEST_DATABASE_URL is not
set.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from gmail_archive.storage import BlobStore

DSN = os.environ.get("GMAIL_ARCHIVE_TEST_DATABASE_URL")

# Blob store path used by the running stack (from docker-compose.yml).
# Tests that only read use this; tests that write use a temp directory.
_BLOB_DIR = Path("/blobs")


def _make_store() -> tuple[BlobStore, Path]:
    """Create a temporary BlobStore for tests that need to write.
    Returns (store, tmp_dir) where store is a BlobStore and tmp_dir is the
    temporary directory path.
    """

    tmp = Path(f"/tmp/test-blobs-{os.urandom(4).hex()}")
    tmp.mkdir(parents=True, exist_ok=True)
    return BlobStore(tmp), tmp


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestVerify:
    def test_verify_returns_report(self) -> None:
        import psycopg

        from gmail_archive.storage import BlobStore
        from gmail_archive.verify import verify

        store = BlobStore(_BLOB_DIR)
        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            report = verify(conn, store)
            # The database has data from the ingest fixture.
            assert report.messages_in_db >= 0
            assert report.blobs_in_db >= 0
            assert report.blobs_on_disk >= 0
            assert isinstance(report.orphaned_blobs, list)
            assert isinstance(report.missing_blobs, list)

    def test_verify_deep(self) -> None:
        import psycopg

        from gmail_archive.storage import BlobStore
        from gmail_archive.verify import verify

        store = BlobStore(_BLOB_DIR)
        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            report = verify(conn, store, deep=True)
            assert report.deep_checked >= 0
            assert isinstance(report.deep_corrupt, list)

    def test_verify_orphaned_blob(self) -> None:
        import psycopg

        from gmail_archive.verify import verify

        store, tmp_dir = _make_store()
        # Write a blob to disk that has no database row.
        data = b"orphaned blob data"
        result = store.put(data)

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            report = verify(conn, store)
            assert result.sha256 in report.orphaned_blobs

        # Clean up
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestExport:
    def test_export_mbox_empty_query(self) -> None:
        import psycopg

        from gmail_archive.export import export_mbox
        from gmail_archive.storage import BlobStore

        store = BlobStore(_BLOB_DIR)
        out = Path("/tmp/test-export-empty.mbox")
        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            count = export_mbox(conn, store, out, query="nonexistent123xyz")
            assert count == 0
        out.unlink(missing_ok=True)

    def test_export_mbox_with_data(self) -> None:
        import psycopg

        from gmail_archive.export import export_mbox

        store, tmp_blob_dir = _make_store()
        data = b"Subject: test\n\nHello world.\n"
        sha256 = hashlib.sha256(data).hexdigest()
        store.put(data)

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind) "
                "values (%s, %s, 'message') on conflict do nothing",
                (sha256, len(data)),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, subject, "
                "search_text) values (%s, %s, 'test', 'hello world') "
                "on conflict do nothing",
                (sha256, len(data)),
            )
            conn.execute(
                "insert into message_sightings "
                "(raw_sha256, source_path, byte_offset, byte_length) "
                "values (%s, '/test', 0, %s) on conflict do nothing",
                (sha256, len(data)),
            )
            conn.commit()

            out = Path("/tmp/test-export-out.mbox")
            count = export_mbox(conn, store, out, query="hello")
            assert count >= 1
            assert out.exists()
            content = out.read_bytes()
            assert b"Subject: test" in content

            out.unlink(missing_ok=True)
            conn.execute(
                "delete from message_sightings where raw_sha256 = %s",
                (sha256,),
            )
            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))
            conn.commit()
        import shutil

        shutil.rmtree(tmp_blob_dir, ignore_errors=True)

    def test_export_eml(self) -> None:
        import psycopg

        from gmail_archive.export import export_eml

        store, tmp_blob_dir = _make_store()
        data = b"Subject: eml test\n\nEML body.\n"
        sha256 = hashlib.sha256(data).hexdigest()
        store.put(data)

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind) "
                "values (%s, %s, 'message') on conflict do nothing",
                (sha256, len(data)),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, subject) "
                "values (%s, %s, 'eml test') on conflict do nothing",
                (sha256, len(data)),
            )
            conn.commit()

            out_dir = Path("/tmp/test-eml-out")
            out_dir.mkdir(parents=True, exist_ok=True)
            # Use a query that matches only our test message.
            count = export_eml(conn, store, out_dir, query="eml test")
            assert count >= 1
            eml_path = out_dir / f"{sha256}.eml"
            assert eml_path.exists()
            assert eml_path.read_bytes() == data

            import shutil

            shutil.rmtree(out_dir, ignore_errors=True)
            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))
            conn.commit()
        import shutil

        shutil.rmtree(tmp_blob_dir, ignore_errors=True)
