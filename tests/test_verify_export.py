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


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestExportRoundTrip:
    """Export then re-ingest must reproduce the same bytes (#21).

    This is the Phase 6 acceptance criterion that was never written, and its
    absence is how #10 shipped: ingest hashed mbox-quoted bytes for weeks
    while every unit test passed. The property it checks cannot be expressed
    in a unit test, because it spans the parser, the blob store, the exporter
    and the ingest pipeline — a round trip is the only place the disagreement
    between them shows up.

    #52 gates the re-ingest on this test, so it is the thing standing between
    a fix and rebuilding 277,000 messages on top of it.
    """

    #: Deliberately includes the quoting that #10 got wrong. A body line
    #: beginning `From ` must be stored unquoted and re-quoted on export; a
    #: `>From ` line in the file means a literal `From ` in the message.
    MESSAGES = (
        b"Subject: plain\r\nFrom: a@example.com\r\n\r\nnothing special\r\n",
        b"Subject: quoted\r\nFrom: b@example.com\r\n\r\n"
        b"From the desk of someone\r\nordinary line\r\n",
        b"Subject: deep\r\nFrom: c@example.com\r\n\r\n"
        b">From already quoted\r\n>>From twice quoted\r\n",
        b"Subject: mime\r\nFrom: d@example.com\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="x.pdf"\r\n\r\nPDF\r\n'
        b"--B--\r\n",
    )

    def _ingest_into(
        self, tmp_path: Path, mbox: Path, tag: str, dsn: str
    ) -> dict[str, bytes]:
        """Ingest an mbox into a private blob store; return {sha: blob bytes}."""
        from gmail_archive.config import Settings
        from gmail_archive.ingest import ingest

        blob_dir = tmp_path / f"blobs-{tag}"
        blob_dir.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            database_url=dsn,
            blob_dir=blob_dir,
            workers=1,
            batch_size=10,
            log_level="WARNING",
            imap_password="",
            web_password_hash="",
        )
        ingest(settings, mbox)
        store = BlobStore(blob_dir)
        return {sha: store.get(sha) for sha in store.iter_blobs()}

    def _write_mbox(self, path: Path, messages: tuple[bytes, ...]) -> Path:
        from gmail_archive.parser import requote_mbox

        with path.open("wb") as fh:
            for i, raw in enumerate(messages):
                fh.write(b"From MAILER-DAEMON@archive  Thu Jan  1 00:00:00 1970\r\n")
                # The writer is what quotes `From ` lines; a fixture that
                # skipped this would not be a valid mboxrd file, and the test
                # would be checking the wrong thing.
                fh.write(requote_mbox(raw))
                del i
        return path

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "#53: export writes the mbox blank-line separator, and the "
            "splitter counts it as the last byte of the message, so every "
            "hash changes on a round trip. Fixed in the splitter as part of "
            "the #52 rebuild; strict so this fails loudly once it passes."
        ),
    )
    def test_stored_bytes_survive_export_and_re_ingest(self, tmp_path: Path) -> None:
        import psycopg

        from conftest import scratch_database
        from gmail_archive.export import export_mbox
        from gmail_archive.migrate import migrate

        # An isolated database: `export_mbox` with no filter exports
        # everything it can see, so rows another test left behind would end up
        # in the file and be asserted about.
        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            original = self._write_mbox(tmp_path / "original.mbox", self.MESSAGES)
            first = self._ingest_into(tmp_path, original, "first", dsn)
            assert len(first) == len(self.MESSAGES), first.keys()

            exported = tmp_path / "exported.mbox"
            with psycopg.connect(dsn) as conn:
                store = BlobStore(tmp_path / "blobs-first")
                written = export_mbox(conn, store, exported, limit=None)
                assert written == len(self.MESSAGES), written

            second = self._ingest_into(tmp_path, exported, "second", dsn)

            # The property: every message that went in comes back out with
            # the same content hash and the same bytes.
            for sha, blob in first.items():
                assert sha in second, f"{sha} did not survive the round trip"
                assert second[sha] == blob, f"{sha} changed on the round trip"

    def test_the_quoted_from_line_is_stored_unquoted(self, tmp_path: Path) -> None:
        # The specific thing #10 got wrong: what reaches the blob store is the
        # RFC822 message, not the mbox file's escaping of it.
        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            original = self._write_mbox(tmp_path / "q.mbox", self.MESSAGES)
            blobs = self._ingest_into(tmp_path, original, "quoting", dsn)
        joined = b"\n".join(blobs.values())
        assert b"\nFrom the desk of someone" in joined
        assert b">From the desk of someone" not in joined

    def test_the_hash_is_the_hash_of_the_stored_bytes(self, tmp_path: Path) -> None:
        # The invariant the whole content-addressed store rests on, and the
        # one #10 broke: the name is the checksum.
        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            original = self._write_mbox(tmp_path / "h.mbox", self.MESSAGES)
            ingested = self._ingest_into(tmp_path, original, "hash", dsn)
            for sha, blob in ingested.items():
                assert hashlib.sha256(blob).hexdigest() == sha
