"""`imap-backfill` tests, centred on its recovery path.

The command runs two phases against one database: it computes envelope and
bodystructure for every message, then assigns IMAP UIDs per folder. The second
phase is the one that can abort mid-run (#13), and the first phase commits as
it goes — so the interesting state is "every envelope present, UIDs missing",
which is precisely what a re-run has to be able to finish.

Against a throwaway database rather than the shared one: the command has no
notion of scope. It backfills every message and every folder it can see, so
rows another test left behind would change what it does.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from conftest import scratch_database
from gmail_archive.cli import main

DSN = os.environ.get("GMAIL_ARCHIVE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL"),
]

_ROWS = 4
_LABEL = "Receipts"


def _seed(dsn: str, *, with_envelopes: bool) -> list[str]:
    """`_ROWS` labelled messages, optionally already carrying envelopes.

    `with_envelopes=True` reproduces the post-abort state: the envelope phase
    finished and committed, the UID phase did not run.
    """
    shas = [hashlib.sha256(f"backfill-{i}".encode()).hexdigest() for i in range(_ROWS)]
    envelope = '{"subject": "s"}' if with_envelopes else None
    with psycopg.connect(dsn) as conn:
        account = conn.execute("select min(id) from accounts").fetchone()
        assert account is not None
        account_id = next(iter(account))
        for i, sha in enumerate(shas):
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind)"
                " values (%s, 10, 'message') on conflict do nothing",
                (sha,),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, subject,"
                " search_text, envelope, bodystructure)"
                " values (%s, 10, %s, %s, %s::jsonb, %s::jsonb)",
                (sha, f"m{i}", f"m{i}", envelope, envelope),
            )
            conn.execute(
                "insert into labels (raw_sha256, label, account_id)"
                " values (%s, %s, %s)",
                (sha, _LABEL, account_id),
            )
        conn.commit()
    return shas


def _uid_count(dsn: str, folder: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "select count(*) from imap_uids u join imap_folders f"
            " on f.id = u.folder_id where f.name = %s",
            (folder,),
        ).fetchone()
    assert row is not None
    return int(next(iter(row)))


@pytest.fixture
def scratch() -> Iterator[str]:
    from gmail_archive.migrate import migrate

    assert DSN is not None
    with scratch_database(DSN) as dsn:
        migrate(dsn)
        yield dsn


def _backfill(dsn: str, blob_dir: Path) -> str:
    """Run the command against `dsn` and return its output.

    `blob_dir` is real but empty. The envelope phase then logs a skip per
    message rather than raising, which is what lets these tests exercise the
    UID phase without standing up a blob store — the phase under test reads
    only the database.
    """
    result = CliRunner().invoke(
        main,
        ["imap-backfill"],
        env={
            "GMAIL_ARCHIVE_DATABASE_URL": dsn,
            "GMAIL_ARCHIVE_BLOB_DIR": str(blob_dir),
        },
    )
    assert result.exit_code == 0, result.output
    return result.output


class TestRecoveryFromAnAbortedRun:
    """The bug: an early return skipped UID assignment entirely.

    `#13` aborts *during* UID assignment, after every envelope is committed.
    The re-run meant to finish the job found no envelope work left, printed
    "All messages already have envelope and bodystructure", and returned —
    leaving the projection half built, reporting success, and giving no
    indication of which folders were short.
    """

    def test_uids_are_assigned_when_every_envelope_already_exists(
        self, scratch: str, tmp_path: Path
    ) -> None:
        _seed(scratch, with_envelopes=True)
        assert _uid_count(scratch, _LABEL) == 0

        output = _backfill(scratch, tmp_path)

        # The skip notice is still correct and still printed — it just is not
        # a reason to stop.
        assert "already have envelope" in output
        assert _uid_count(scratch, _LABEL) == _ROWS

    def test_a_second_run_changes_nothing(self, scratch: str, tmp_path: Path) -> None:
        """UID assignment must be idempotent, not merely re-entrant.

        Clients cache UIDs hard and read a changed one as data loss, so a
        re-run that renumbered would be worse than one that did nothing.
        """
        _seed(scratch, with_envelopes=False)
        _backfill(scratch, tmp_path)

        with psycopg.connect(scratch) as conn:
            before = conn.execute(
                "select raw_sha256, uid from imap_uids order by folder_id, uid"
            ).fetchall()

        _backfill(scratch, tmp_path)

        with psycopg.connect(scratch) as conn:
            after = conn.execute(
                "select raw_sha256, uid from imap_uids order by folder_id, uid"
            ).fetchall()
        assert before == after

    def test_the_summary_reports_what_it_did(
        self, scratch: str, tmp_path: Path
    ) -> None:
        _seed(scratch, with_envelopes=True)
        first = _backfill(scratch, tmp_path)
        # INBOX gets everything as well as the label folder, so every message
        # is assigned twice — once per folder.
        assert f"Assigned {_ROWS * 2} UIDs across 2 of 2 folders." in first

        second = _backfill(scratch, tmp_path)
        assert "Assigned 0 UIDs across 0 of 2 folders." in second
        # A no-op must not print a line per folder; that buried the one folder
        # that actually moved.
        assert f"Folder '{_LABEL}'" not in second
