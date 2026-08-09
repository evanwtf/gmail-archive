"""Ingest pipeline tests.

The ingest pipeline ties together the mbox splitter, parser, blob store, and
Postgres. Unit tests cover the bookkeeping functions; integration tests (gated
behind GMAIL_ARCHIVE_TEST_DATABASE_URL) exercise the full pipeline against a
real database.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg

from gmail_archive.config import Settings
from gmail_archive.fixtures.generator import Pathology, generate
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
        web_password_hash="",
        imap_password="",
    )


def _default_account(conn: psycopg.Connection[object]) -> int:
    """The account id every pre-#39 row was attributed to by migration 0006."""
    row = conn.execute("select min(id) from accounts").fetchone()
    assert row is not None
    account_id = next(iter(row))  # type: ignore[call-overload]
    assert account_id is not None
    return int(account_id)


def _mbox(tmp_path: Path, lines: list[bytes]) -> Path:
    """Write a minimal mbox file from message bodies."""
    path = tmp_path / "test.mbox"
    with open(path, "wb") as f:
        for i, body in enumerate(lines):
            f.write(f"From user{i}@e.com Mon Jan 01 00:00:00 2000\n".encode())
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
                "from ingest_runs where id = %s",
                (run_id,),
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
                    # `_write_batch` indexes this directly, as it does every
                    # other key: the metadata dict is a contract between
                    # `_worker_task` and `_write_batch`, and a `.get` default
                    # here would let the two drift apart silently.
                    "kept_headers": [("List-Id", 0, "Dev <dev.example.com>")],
                    "parse_warnings": [],
                    "blob_written": True,
                },
            )

            _write_batch(
                conn, run_id, "/tmp/batch.mbox", [result], _default_account(conn)
            )

            header = conn.execute(
                "select name, seq, value from message_headers where raw_sha256 = %s",
                (sha256,),
            ).fetchone()
            assert header == ("List-Id", 0, "Dev <dev.example.com>")

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
                "where raw_sha256 = %s",
                (sha256,),
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

            _write_batch(
                conn, run_id, "/tmp/fail.mbox", [result], _default_account(conn)
            )

            failed = conn.execute(
                "select byte_offset, error, truncated from failed_messages "
                "where run_id = %s",
                (run_id,),
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

        mbox = _mbox(
            tmp_path,
            [
                b"Subject: first\n\nbody one\n",
                b"Subject: second\n\nbody two\n",
            ],
        )
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

        mbox = _mbox(
            tmp_path,
            [
                b"Subject: only\n\nbody\n",
            ],
        )
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

        mbox = _mbox(
            tmp_path,
            [
                b"Subject: nul\n\nbefore\x00after\n",
            ],
        )
        settings = _settings(tmp_path)
        report = ingest(settings, mbox)

        assert report.messages_seen == 1
        assert report.messages_new == 1
        assert report.failures == 0

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            row = conn.execute("select parse_warnings from messages").fetchone()
            assert row is not None
            assert row[0] is not None

            conn.execute("delete from message_sightings")
            conn.execute("delete from labels")
            conn.execute("delete from messages")
            conn.execute("delete from blobs")
            conn.execute("delete from ingest_runs")


class TestMboxUnquoting:
    """Ingest must store the unquoted RFC822 message (ADR-002).

    It previously told `parse()` the bytes were already unquoted when nothing
    had unquoted them, so `raw_sha256` hashed the mbox-quoted form and every
    blob carried `>From ` lines the message never had.
    """

    MBOX = (
        b"From MAILER-DAEMON Thu Jan  1 00:00:00 1970\n"
        b"From: a@example.com\n"
        b"Subject: quoting\n"
        b"\n"
        b">From the desk of someone\n"
        b"normal line\n"
    )

    def _ingest_one(self, tmp_path: object) -> tuple[bytes, str]:
        """Run a worker task over a one-message mbox; return (blob, sha)."""
        from pathlib import Path

        from gmail_archive.ingest import _worker_task
        from gmail_archive.mbox import scan

        base = Path(str(tmp_path))
        mbox = base / "one.mbox"
        mbox.write_bytes(self.MBOX)
        blobs = base / "blobs"
        blobs.mkdir()

        offset, length = scan(mbox).offsets[0]
        result = _worker_task(offset, length, str(mbox), str(blobs))
        assert result.success and result.metadata is not None
        sha = str(result.metadata["raw_sha256"])
        return (blobs / sha[:2] / sha).read_bytes(), sha

    def test_the_stored_blob_is_unquoted(self, tmp_path: object) -> None:
        blob, _ = self._ingest_one(tmp_path)
        assert b"\nFrom the desk of someone\n" in blob
        assert b">From the desk" not in blob

    def test_the_hash_is_of_the_unquoted_bytes(self, tmp_path: object) -> None:
        # The blob's name must be the hash of its own contents — the property
        # the whole content-addressed store rests on.
        import hashlib

        blob, sha = self._ingest_one(tmp_path)
        assert hashlib.sha256(blob).hexdigest() == sha

    def test_body_text_is_unquoted_too(self, tmp_path: object) -> None:
        from pathlib import Path

        from gmail_archive.ingest import _worker_task
        from gmail_archive.mbox import scan

        base = Path(str(tmp_path))
        mbox = base / "one.mbox"
        mbox.write_bytes(self.MBOX)
        blobs = base / "blobs"
        blobs.mkdir()
        offset, length = scan(mbox).offsets[0]
        result = _worker_task(offset, length, str(mbox), str(blobs))
        assert result.metadata is not None
        assert "From the desk" in result.metadata["body_text"]
        assert ">From the desk" not in result.metadata["body_text"]

    def test_ambiguous_quoting_is_still_recorded(self, tmp_path: object) -> None:
        # ADR-002 promises a warning on `>>From ` lines. It could never fire
        # before, because the code path that emits it never ran.
        import json
        from pathlib import Path

        from gmail_archive.ingest import _worker_task
        from gmail_archive.mbox import scan

        base = Path(str(tmp_path))
        mbox = base / "amb.mbox"
        mbox.write_bytes(
            b"From MAILER-DAEMON Thu Jan  1 00:00:00 1970\n"
            b"From: a@example.com\n\n"
            b">>From a double quote\n"
        )
        blobs = base / "blobs"
        blobs.mkdir()
        offset, length = scan(mbox).offsets[0]
        result = _worker_task(offset, length, str(mbox), str(blobs))
        assert result.metadata is not None
        codes = [w["code"] for w in json.loads(result.metadata["parse_warnings"])]
        assert "unquote-ambiguous" in codes


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestIngestExclusivity:
    """Only one ingest at a time (#46).

    Two concurrent runs corrupted each other twice over: `sweep_temporaries`
    deleted the other's in-flight blobs, and `_ensure_run` made both adopt the
    same run row and fight over its checkpoint.
    """

    def test_a_second_ingest_refuses_while_the_first_holds_the_lock(
        self, tmp_path: Path
    ) -> None:
        import psycopg

        from gmail_archive.ingest import (
            _INGEST_LOCK_KEY,
            IngestAlreadyRunningError,
            ingest,
        )

        mbox = _mbox(tmp_path, [b"Subject: one\n\nbody\n"])
        settings = _settings(tmp_path)

        # Stand in for the other ingest by holding its lock.
        holder = psycopg.connect(DSN)  # type: ignore[arg-type]
        try:
            got = holder.execute(
                "select pg_try_advisory_lock(%s)", (_INGEST_LOCK_KEY,)
            ).fetchone()
            assert got is not None and next(iter(got)) is True

            with pytest.raises(IngestAlreadyRunningError):
                ingest(settings, mbox)
        finally:
            holder.close()  # session-level lock, released on close

    def test_the_lock_is_released_when_the_run_finishes(self, tmp_path: Path) -> None:
        import psycopg

        from gmail_archive.ingest import _INGEST_LOCK_KEY, ingest

        mbox = _mbox(tmp_path, [b"Subject: two\n\nbody\n"])
        ingest(_settings(tmp_path), mbox)

        # A killed ingest must not leave the archive permanently locked, which
        # is why this is a session lock rather than a row.
        other = psycopg.connect(DSN)  # type: ignore[arg-type]
        try:
            got = other.execute(
                "select pg_try_advisory_lock(%s)", (_INGEST_LOCK_KEY,)
            ).fetchone()
            assert got is not None and next(iter(got)) is True, (
                "the lock outlived the run that took it"
            )
        finally:
            other.close()


# ── Storability: the contract no unit test can express (#41) ─────────────────


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestEverythingTheParserEmitsIsStorable:
    """Every value `parse()` produces must survive the trip into Postgres.

    Three bugs have now had exactly this shape — NUL bytes in decoded text
    (twice, the second time 194,000 messages into an IMAP backfill), lone
    surrogates from `surrogateescape`, and a timezone offset past ±15:59.
    Each was found by a batch of a thousand messages dying, and each got a
    unit test afterwards.

    None of those unit tests could have prevented the next one, because the
    failure is never in `parse()`. It returns happily; the COPY rejects the
    value later. The hypothesis property test asserts `parse()` does not
    raise, which stayed true through all three. The contract that was actually
    being violated only exists where the parser meets the database, so that is
    where it has to be checked.

    Every pathology the generator knows how to make, ingested for real. When a
    fourth one of these appears — and the pattern says it will, as something
    nobody anticipated — the fix is a new `Pathology` member, and this test
    covers it from then on with no new test to write.
    """

    def _fixture(self, tmp_path: Path, name: str, pathologies: list[str]) -> Path:
        out = tmp_path / f"{name}.mbox"
        selected = [Pathology(p) for p in pathologies]
        # `count` must cover every pathology at least once; the generator
        # refuses otherwise, which is the property that makes this test
        # meaningful rather than a coin flip.
        generate(out, count=max(len(selected) * 4, 40), seed=41, pathologies=selected)
        return out

    def test_every_pathology_at_once_ingests_without_a_single_failure(
        self, tmp_path: Path
    ) -> None:
        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        every = [p.value for p in Pathology]
        mbox = self._fixture(tmp_path, "every", every)

        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            settings = Settings(
                database_url=dsn,
                blob_dir=tmp_path / "blobs-every",
                workers=2,
                batch_size=10,
                log_level="WARNING",
                web_password_hash="",
                imap_password="",
            )
            report = ingest(settings, mbox)

        # `failures` is the whole assertion. A message Postgres refuses is
        # counted here, and the run still "succeeds" — which is why a passing
        # ingest was never evidence of anything.
        assert report.failures == 0, (
            f"{report.failures} of {report.messages_seen} messages could not be "
            f"stored; the pathologies in play were {', '.join(every)}"
        )
        assert report.messages_seen == report.messages_new + report.messages_duplicate

    @pytest.mark.parametrize(
        "pathology",
        # Named individually so a failure says which defect broke storage
        # rather than "one of twenty-six did". The parametrised ids are the
        # CLI spellings, so a failure is directly reproducible with
        # `gen-fixture --pathologies <id>`.
        [p.value for p in Pathology],
    )
    def test_each_pathology_alone_ingests_without_a_single_failure(
        self, tmp_path: Path, pathology: str
    ) -> None:
        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        mbox = self._fixture(tmp_path, pathology, [pathology])

        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            settings = Settings(
                database_url=dsn,
                blob_dir=tmp_path / f"blobs-{pathology}",
                workers=1,
                batch_size=10,
                log_level="WARNING",
                web_password_hash="",
                imap_password="",
            )
            report = ingest(settings, mbox)

        assert report.failures == 0, (
            f"'{pathology}' produced {report.failures} unstorable messages; "
            f"reproduce with: gmail-archive gen-fixture --pathologies {pathology}"
        )


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestKeptHeadersReachThePlaceTheyAreQueriedFrom:
    """`message_headers` is populated by a real ingest (#34).

    The parser tests prove `parse()` produces the values. This proves they
    survive the COPY, which is a separate claim and the one that has failed
    three times before (#41).
    """

    MESSAGES = (
        b"Subject: bulk\r\nFrom: news@amazon.com\r\n"
        b"Date: Fri, 4 Apr 2025 09:00:00 +0000\r\n"
        b"List-Unsubscribe: <https://amazon.example/u>\r\n"
        b"List-Unsubscribe: <mailto:u@amazon.example>\r\n"
        b"Precedence: bulk\r\nX-Mailer: BulkPlatform 4\r\n\r\nbuy\r\n",
        b"Subject: human\r\nFrom: p@example.com\r\n"
        b"Date: Fri, 4 Apr 2025 10:00:00 +0000\r\n"
        b"User-Agent: Mutt/2.2\r\n\r\nhello\r\n",
        # The live archive's year-2611 row, from its real headers (#27).
        b"Subject: y2611\r\nFrom: q@example.com\r\n"
        b"Date: Tue, 17 Sep 2611 16:00:10 GMT\r\n"
        b"Received: from x by y; Fri, 4 Apr 2025 01:59:09 -0700\r\n\r\nweird\r\n",
    )

    def _ingest(self, tmp_path: Path, dsn: str) -> None:
        mbox = tmp_path / "headers.mbox"
        with mbox.open("wb") as fh:
            for raw in self.MESSAGES:
                fh.write(b"From MAILER-DAEMON@archive  Thu Jan  1 00:00:00 1970\r\n")
                fh.write(raw)
                fh.write(b"\n")
        report = ingest(
            Settings(
                database_url=dsn,
                blob_dir=tmp_path / "blobs",
                workers=1,
                batch_size=10,
                log_level="WARNING",
                web_password_hash="",
                imap_password="",
            ),
            mbox,
        )
        assert report.failures == 0

    def test_headers_land_and_the_2611_date_is_replaced(self, tmp_path: Path) -> None:
        import psycopg

        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            self._ingest(tmp_path, dsn)

            with psycopg.connect(dsn) as conn:
                rows = conn.execute(
                    "select m.subject, h.name, h.seq, h.value"
                    " from message_headers h join messages m using (raw_sha256)"
                    " order by m.subject, h.name, h.seq"
                ).fetchall()
                dates: dict[str, datetime | None] = dict(
                    conn.execute(
                        "select subject, internal_date from messages"
                    ).fetchall()
                )

        by_subject: dict[str, list[tuple[str, int, str]]] = {}
        for subject, name, seq, value in rows:
            by_subject.setdefault(subject, []).append((name, seq, value))

        assert by_subject["bulk"] == [
            ("List-Unsubscribe", 0, "<https://amazon.example/u>"),
            ("List-Unsubscribe", 1, "<mailto:u@amazon.example>"),
            ("Precedence", 0, "bulk"),
            ("X-Mailer", 0, "BulkPlatform 4"),
        ]
        assert by_subject["human"] == [("User-Agent", 0, "Mutt/2.2")]
        # No allowlisted headers at all means no rows, not an empty-string row.
        assert "y2611" not in by_subject

        # The whole point of #27: this used to sort above 22 years of mail.
        assert dates["y2611"] is not None
        assert dates["y2611"].year == 2025


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestAccountAttribution:
    """A message records which account it arrived in (#39).

    Schema-level only — there is no switcher yet. It exists now because it is
    the one part of #39 that cannot be added after the #52 rebuild without
    another one: label rows are written from the per-account export they came
    from, and nothing could later recover which account a label belonged to.
    """

    def _mbox(self, tmp_path: Path, name: str, subject: str) -> Path:
        path = tmp_path / f"{name}.mbox"
        with path.open("wb") as fh:
            fh.write(b"From MAILER-DAEMON@archive  Thu Jan  1 00:00:00 1970\r\n")
            fh.write(
                f"Subject: {subject}\r\nFrom: a@example.com\r\n"
                f"Date: Fri, 4 Apr 2025 09:00:00 +0000\r\n"
                f"X-Gmail-Labels: Inbox,Starred\r\n\r\nshared body\r\n".encode()
            )
            fh.write(b"\n")
        return path

    def _settings(self, tmp_path: Path, dsn: str, tag: str) -> Settings:
        return Settings(
            database_url=dsn,
            blob_dir=tmp_path / f"blobs-{tag}",
            workers=1,
            batch_size=10,
            log_level="WARNING",
            web_password_hash="",
            imap_password="",
        )

    def test_no_account_flag_uses_the_default_account(self, tmp_path: Path) -> None:
        import psycopg

        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            ingest(
                self._settings(tmp_path, dsn, "d"),
                self._mbox(tmp_path, "d", "solo"),
            )
            with psycopg.connect(dsn) as conn:
                accounts = conn.execute("select address from accounts").fetchall()
                linked = conn.execute(
                    "select count(*) from message_accounts"
                ).fetchone()

        # A single-mailbox user should never have to learn the flag exists.
        assert accounts == [("default",)]
        assert linked == (1,)

    def test_the_same_message_in_two_accounts_is_one_row_but_two_links(
        self, tmp_path: Path
    ) -> None:
        import psycopg

        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            # Byte-identical mail, as happens when one message is addressed to
            # two of your own addresses. This is the case that made the schema
            # urgent: it used to merge silently and unrecoverably.
            first = self._mbox(tmp_path, "one", "shared")
            second = self._mbox(tmp_path, "two", "shared")
            ingest(self._settings(tmp_path, dsn, "1"), first, account="a@example.com")
            ingest(self._settings(tmp_path, dsn, "2"), second, account="b@example.com")

            with psycopg.connect(dsn) as conn:
                messages = conn.execute("select count(*) from messages").fetchone()
                links = conn.execute(
                    "select a.address from message_accounts ma"
                    " join accounts a on a.id = ma.account_id"
                    " order by a.address"
                ).fetchall()
                labels = conn.execute(
                    "select a.address, l.label from labels l"
                    " join accounts a on a.id = l.account_id"
                    " order by a.address, l.label"
                ).fetchall()

        # Content-addressed: one row, as it should be.
        assert messages == (1,)
        # But both accounts saw it, and that is now recorded.
        assert links == [("a@example.com",), ("b@example.com",)]
        # And the labels exist per account rather than collapsing — the thing
        # the old (raw_sha256, label) key made impossible to represent.
        assert labels == [
            ("a@example.com", "Inbox"),
            ("a@example.com", "Starred"),
            ("b@example.com", "Inbox"),
            ("b@example.com", "Starred"),
        ]

    def test_the_run_records_its_account(self, tmp_path: Path) -> None:
        import psycopg

        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            ingest(
                self._settings(tmp_path, dsn, "r"),
                self._mbox(tmp_path, "r", "run"),
                account="c@example.com",
            )
            with psycopg.connect(dsn) as conn:
                row = conn.execute(
                    "select a.address from ingest_runs r"
                    " join accounts a on a.id = r.account_id"
                ).fetchone()
        assert row == ("c@example.com",)


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestTwoOverlappingTakeoutExports:
    """Two exports months apart, fed in one after the other.

    The realistic way to keep this archive current: request Takeout again
    later and ingest the new file on top of the old one. The second export is
    not a delta — it contains everything the first did, plus whatever arrived
    since — so the whole thing rests on deduplication being exact.

    It is, and for a structural reason rather than a lucky one: `raw_sha256`
    is the primary key, so a byte-identical message is the same row by
    construction. There is no comparison step that could be wrong.
    """

    def _write(self, path: Path, bodies: list[bytes]) -> Path:
        with path.open("wb") as fh:
            for raw in bodies:
                fh.write(b"From MAILER-DAEMON@archive  Thu Jan  1 00:00:00 1970\r\n")
                fh.write(raw)
                fh.write(b"\n")
        return path

    def _bodies(self, start: int, count: int, label: str) -> list[bytes]:
        return [
            (
                f"Subject: message {i}\r\nFrom: s{i}@example.com\r\n"
                f"Date: Fri, 4 Apr 2025 09:00:00 +0000\r\n"
                f"Message-ID: <m{i}@example.com>\r\n"
                f"X-Gmail-Labels: {label}\r\n\r\nbody of message {i}\r\n"
            ).encode()
            for i in range(start, start + count)
        ]

    def _settings(self, tmp_path: Path, dsn: str) -> Settings:
        # One blob store, as a real archive has.
        return Settings(
            database_url=dsn,
            blob_dir=tmp_path / "blobs",
            workers=1,
            batch_size=25,
            log_level="WARNING",
            web_password_hash="",
            imap_password="",
        )

    def test_the_second_export_adds_only_what_is_new(self, tmp_path: Path) -> None:
        import psycopg

        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        # August: 40 messages. December: those same 40, byte for byte, plus 20
        # that arrived since — which is what a real second Takeout looks like.
        august = self._bodies(0, 40, "Inbox")
        december = august + self._bodies(40, 20, "Inbox")

        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            settings = self._settings(tmp_path, dsn)

            first = ingest(settings, self._write(tmp_path / "august.mbox", august))
            second = ingest(settings, self._write(tmp_path / "december.mbox", december))

            with psycopg.connect(dsn) as conn:
                messages = conn.execute("select count(*) from messages").fetchone()
                sightings = conn.execute(
                    "select count(*) from message_sightings"
                ).fetchone()
                sources = conn.execute(
                    "select count(distinct source_path) from message_sightings"
                ).fetchone()
                blobs = conn.execute("select count(*) from blobs").fetchone()

        assert first.messages_new == 40
        assert first.messages_duplicate == 0

        # The point: the December run sees all 60 but stores only the 20 new.
        assert second.messages_seen == 60
        assert second.messages_new == 20
        assert second.messages_duplicate == 40
        assert second.failures == 0

        # 60 distinct messages, one blob each.
        assert messages == (60,)
        assert blobs == (60,)

        # But 100 sightings across 2 files — nothing is silently discarded,
        # and `verify` can still reconcile either export against the archive.
        assert sightings == (100,)
        assert sources == (2,)

    def test_re_ingesting_the_same_export_changes_nothing(self, tmp_path: Path) -> None:
        # The other half of the guarantee: an accidental repeat is harmless,
        # which is what makes "just ingest it again" safe advice.
        import psycopg

        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        bodies = self._bodies(0, 30, "Inbox")
        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            settings = self._settings(tmp_path, dsn)
            path = self._write(tmp_path / "same.mbox", bodies)

            ingest(settings, path)
            again = ingest(settings, path)

            with psycopg.connect(dsn) as conn:
                messages = conn.execute("select count(*) from messages").fetchone()
                sightings = conn.execute(
                    "select count(*) from message_sightings"
                ).fetchone()

        assert again.messages_new == 0
        assert again.messages_duplicate == 30
        assert messages == (30,)
        # Same file, same offsets: the sighting rows collide and are not
        # duplicated either.
        assert sightings == (30,)

    def test_labels_accumulate_rather_than_track_the_later_export(
        self, tmp_path: Path
    ) -> None:
        """A real caveat, pinned so it is a decision rather than a surprise.

        If a message was in the inbox in August and archived by December, the
        December export no longer carries the `Inbox` label — but the label
        row from August is still there, and label writes are
        `on conflict do nothing`. So labels are the *union* across exports,
        never the latest state.

        That is defensible for an archive (it records what was true, and
        nothing is lost) but it is not what a user expects from "sync", and it
        is worth knowing before relying on `label:Inbox` to mean "currently in
        the inbox".
        """
        import psycopg

        from conftest import scratch_database
        from gmail_archive.migrate import migrate

        body = self._bodies(0, 1, "Inbox")
        archived = [
            body[0].replace(b"X-Gmail-Labels: Inbox", b"X-Gmail-Labels: Archived")
        ]

        assert DSN is not None
        with scratch_database(DSN) as dsn:
            migrate(dsn)
            settings = self._settings(tmp_path, dsn)
            ingest(settings, self._write(tmp_path / "aug.mbox", body))
            ingest(settings, self._write(tmp_path / "dec.mbox", archived))

            with psycopg.connect(dsn) as conn:
                labels = conn.execute(
                    "select label from labels order by label"
                ).fetchall()
                messages = conn.execute("select count(*) from messages").fetchone()

        # Two different label sets means two different bodies, so these are
        # two messages — the header is part of the hashed bytes.
        assert messages == (2,)
        assert labels == [("Archived",), ("Inbox",)]
