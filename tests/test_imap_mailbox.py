"""IMAP mailbox tests.

The IMAP layer has had no tests beyond authentication (#16), and #45 is what
that costs: `snapshot()` materialised the entire folder to produce three
integers, and nothing noticed because it *worked*. Every earlier IMAP bug was
"it does not work at all"; this one was only expensive.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from gmail_archive.imap.mailbox import MailboxData

DSN = os.environ.get("GMAIL_ARCHIVE_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL"),
]

#: Enough rows to be a folder, few enough to be fast. The bug was about
#: proportionality, not volume — it is equally wrong at five rows.
_ROWS = 5


@pytest.fixture
def seeded_folder() -> Iterator[int]:
    """A folder with `_ROWS` messages in it, torn down afterwards."""
    import psycopg

    shas = [
        hashlib.sha256(f"imap-mailbox-{i}".encode()).hexdigest() for i in range(_ROWS)
    ]
    with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
        row = conn.execute(
            "insert into imap_folders (name, uid_validity) values (%s, 1)"
            " on conflict (name) do update set uid_validity = 1 returning id",
            ("TestSnapshotFolder",),
        ).fetchone()
        assert row is not None
        folder_id = int(next(iter(row)))
        for i, sha in enumerate(shas, start=1):
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind)"
                " values (%s, 10, 'message') on conflict do nothing",
                (sha,),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, subject, search_text)"
                " values (%s, 10, %s, %s) on conflict do nothing",
                (sha, f"m{i}", f"m{i}"),
            )
            conn.execute(
                "insert into imap_uids (folder_id, raw_sha256, uid)"
                " values (%s, %s, %s) on conflict do nothing",
                (folder_id, sha, i),
            )
        conn.commit()
    try:
        yield folder_id
    finally:
        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            conn.execute("delete from imap_uids where folder_id = %s", (folder_id,))
            for sha in shas:
                conn.execute("delete from messages where raw_sha256 = %s", (sha,))
                conn.execute("delete from blobs where sha256 = %s", (sha,))
            conn.execute("delete from imap_folders where id = %s", (folder_id,))
            conn.commit()


@pytest.fixture
async def pool() -> AsyncIterator[Any]:
    p = AsyncConnectionPool(DSN or "", min_size=1, max_size=2, open=False)
    await p.open(wait=True)
    try:
        yield p
    finally:
        await p.close()


def _mailbox(folder_id: int, pool: Any) -> MailboxData:
    async def factory() -> Any:
        return pool

    return MailboxData(folder_id, "TestSnapshotFolder", 1, factory)


class TestSnapshotDoesNotMaterialiseTheFolder:
    async def test_the_counts_are_right(self, seeded_folder: int, pool: Any) -> None:
        snapshot = await _mailbox(seeded_folder, pool).snapshot()
        assert snapshot.exists == _ROWS
        # Nothing in a read-only archive is RECENT, and every message carries
        # the same permanent flags — which included Seen — so these were
        # always constant. The scan was computing a constant the hard way.
        assert snapshot.recent == 0
        assert snapshot.unseen == 0
        assert snapshot.first_unseen is None

    async def test_uidnext_is_right_without_a_prior_full_load(
        self, seeded_folder: int, pool: Any
    ) -> None:
        # A latent bug the rewrite fixed: `_max_uid` was only populated by
        # `_load_all_messages()`, so UIDNEXT was 1 on any instance that had
        # not run one.
        snapshot = await _mailbox(seeded_folder, pool).snapshot()
        assert snapshot.next_uid == _ROWS + 1

    async def test_it_never_loads_the_messages(
        self, seeded_folder: int, pool: Any
    ) -> None:
        """The regression guard, and the whole point of #45.

        Asserting on the counts alone would pass just as happily against the
        old implementation. What must not come back is the *scan*: on this
        archive INBOX is every message, so a SELECT built ~554,000 `Message`
        objects — the folder twice — to produce three integers.
        """
        mailbox = _mailbox(seeded_folder, pool)

        async def explode() -> list[Any]:
            raise AssertionError(
                "snapshot() loaded the folder; it must answer from SQL (#45)"
            )

        mailbox._load_all_messages = explode  # type: ignore[method-assign]
        snapshot = await mailbox.snapshot()
        assert snapshot.exists == _ROWS

    async def test_an_empty_folder_is_not_an_error(self, pool: Any) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            row = conn.execute(
                "insert into imap_folders (name, uid_validity) values (%s, 1)"
                " on conflict (name) do update set uid_validity = 1 returning id",
                ("TestEmptyFolder",),
            ).fetchone()
            assert row is not None
            folder_id = int(next(iter(row)))
            conn.commit()
        try:
            snapshot = await _mailbox(folder_id, pool).snapshot()
            assert snapshot.exists == 0
            # coalesce, not max(NULL) + 1.
            assert snapshot.next_uid == 1
        finally:
            with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
                conn.execute("delete from imap_folders where id = %s", (folder_id,))
                conn.commit()


class TestReadOnlyEnforcement:
    """Every mutating operation refuses (#16).

    Cheap to write, and worth pinning precisely because it is cheap: the
    read-only guarantee is the archive's central promise, and it is currently
    enforced by five separate `raise` statements that nothing checks. One
    accidental implementation would remove it silently.
    """

    async def test_mailbox_mutations_raise(self, seeded_folder: int, pool: Any) -> None:
        from pymap.exceptions import MailboxReadOnly

        mailbox = _mailbox(seeded_folder, pool)
        with pytest.raises(MailboxReadOnly):
            await mailbox.append(None)  # type: ignore[arg-type]
        with pytest.raises(MailboxReadOnly):
            await mailbox.copy(1, mailbox)
        with pytest.raises(MailboxReadOnly):
            await mailbox.move(1, mailbox)
        with pytest.raises(MailboxReadOnly):
            await mailbox.update(1, None, frozenset(), None)  # type: ignore[arg-type]

    async def test_delete_raises(self, seeded_folder: int, pool: Any) -> None:
        from pymap.exceptions import MailboxReadOnly

        with pytest.raises(MailboxReadOnly):
            await _mailbox(seeded_folder, pool).delete([1, 2, 3])

    async def test_the_mailbox_reports_itself_readonly(
        self, seeded_folder: int, pool: Any
    ) -> None:
        assert _mailbox(seeded_folder, pool).readonly is True

    async def test_mailbox_set_mutations_raise(self, pool: Any) -> None:
        from pymap.exceptions import NotAllowedError

        from gmail_archive.imap.mailbox import MailboxSet

        async def factory() -> Any:
            return pool

        mailbox_set = MailboxSet(factory)
        with pytest.raises(NotAllowedError):
            await mailbox_set.add_mailbox("Nope")
        with pytest.raises(NotAllowedError):
            await mailbox_set.delete_mailbox("Nope")
        with pytest.raises(NotAllowedError):
            await mailbox_set.rename_mailbox("A", "B")


class TestFolderSync:
    """Folders track the `labels` table (#16)."""

    async def test_inbox_always_exists_and_sync_is_idempotent(
        self, seeded_folder: int, pool: Any
    ) -> None:
        from gmail_archive.imap.mailbox import MailboxSet

        async def factory() -> Any:
            return pool

        mailbox_set = MailboxSet(factory)
        first = await mailbox_set._sync_folders()
        assert "INBOX" in first

        # Re-running must not create duplicates — it runs on every LIST, so a
        # non-idempotent sync would grow the table on every client refresh.
        second = await mailbox_set._sync_folders()
        assert first == second

    async def test_get_mailbox_is_case_insensitive_for_inbox(self, pool: Any) -> None:
        # RFC 3501: INBOX is case-insensitive. Clients spell it every way.
        from gmail_archive.imap.mailbox import MailboxSet

        async def factory() -> Any:
            return pool

        mailbox_set = MailboxSet(factory)
        assert (await mailbox_set.get_mailbox("inbox"))._name == "INBOX"
        assert (await mailbox_set.get_mailbox("InBoX"))._name == "INBOX"

    async def test_get_mailbox_returns_the_same_instance(self, pool: Any) -> None:
        """One `MailboxData` per folder, for the life of the session.

        `get_mailbox()` used to build a new one per call, which threw away the
        UID ceiling and selected-set state a SELECT had just established —
        before the FETCH that followed could use them.
        """
        from gmail_archive.imap.mailbox import MailboxSet

        async def factory() -> Any:
            return pool

        mailbox_set = MailboxSet(factory)
        assert await mailbox_set.get_mailbox("INBOX") is await mailbox_set.get_mailbox(
            "INBOX"
        )

    async def test_an_unknown_mailbox_raises_keyerror(self, pool: Any) -> None:
        from gmail_archive.imap.mailbox import MailboxSet

        async def factory() -> Any:
            return pool

        with pytest.raises(KeyError):
            await MailboxSet(factory).get_mailbox("NoSuchFolderAnywhere")


class TestLoadAllMessages:
    """The UID list a SELECT is built from (#16)."""

    async def test_uids_ascend_and_max_uid_is_tracked(
        self, seeded_folder: int, pool: Any
    ) -> None:
        mailbox = _mailbox(seeded_folder, pool)
        messages = await mailbox._load_all_messages()
        uids = [m.uid for m in messages]
        assert uids == sorted(uids)
        assert len(uids) == _ROWS
        assert mailbox._max_uid == _ROWS

    async def test_a_null_internal_date_falls_back(
        self, seeded_folder: int, pool: Any
    ) -> None:
        # ~2.7% of this archive has no Date header. IMAP requires an
        # INTERNALDATE, so the message must still appear rather than be
        # dropped or crash the SELECT.
        messages = await _mailbox(seeded_folder, pool)._load_all_messages()
        assert all(m.internal_date is not None for m in messages)


class TestMessageContentLoading:
    """FETCH reads the body from the blob store, lazily (#16)."""

    async def test_a_missing_blob_does_not_kill_the_fetch(self, tmp_path: Any) -> None:
        """`verify` reports missing blobs, so a damaged archive is real.

        An empty body beats killing the client's whole FETCH — losing one
        message is recoverable, losing the session is not.
        """
        from pymap.parsing.specials.fetchattr import FetchRequirement
        from pymap.parsing.specials.objectid import ObjectId

        from gmail_archive.imap.message import Message
        from gmail_archive.storage import BlobStore

        absent = "0" * 64
        msg = Message(
            1,
            __import__("datetime").datetime.now(__import__("datetime").UTC),
            frozenset(),
            email_id=ObjectId(absent[:16].encode()),
            thread_id=ObjectId(b"0" * 16),
            raw_sha256=absent,
            store=BlobStore(tmp_path),
        )
        loaded = await msg.load_content(FetchRequirement.CONTENT)
        assert loaded is not None


@pytest.mark.slow
class TestEndToEndOverASocket:
    """A real client, a real socket, a real FETCH (#16).

    "One test of this shape is worth all the unit tests above" — and the
    history backs that up. Seven IMAP bugs shipped in sequence, each hidden
    behind the previous, and every one of them would have failed this: auth on
    a throwaway Identity, a CLI namespace missing pymap's defaults, the pool
    factory used as an async context manager, UIDs looked up by position, NUL
    in envelope JSON, off-by-one column indices, and `Message` never loading
    blob content at all.

    Marked `slow` — it starts a server process — so it is out of the default
    run and in the explicit one.
    """

    PASSWORD = "e2e-test-password"
    RAW = (
        b"From: sender@example.com\r\nTo: rcpt@example.com\r\n"
        b"Subject: end to end\r\nDate: Fri, 4 Apr 2025 09:00:00 +0000\r\n"
        b"Message-ID: <e2e@example.com>\r\n\r\nthe body bytes\r\n"
    )

    @pytest.fixture
    def served(self, tmp_path: Any) -> Iterator[tuple[int, str, bytes]]:
        """Seed one message, start the server, yield (port, folder, raw)."""
        import socket as _socket
        import subprocess
        import time

        import psycopg

        from gmail_archive.storage import BlobStore

        blob_dir = tmp_path / "blobs"
        blob_dir.mkdir()
        sha = BlobStore(blob_dir).put(self.RAW).sha256
        folder = "E2EFolder"

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind)"
                " values (%s, %s, 'message') on conflict do nothing",
                (sha, len(self.RAW)),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, subject,"
                " from_addr, internal_date, search_text)"
                " values (%s, %s, 'end to end', 'sender@example.com',"
                " '2025-04-04T09:00:00+00', 'end to end')"
                " on conflict do nothing",
                (sha, len(self.RAW)),
            )
            row = conn.execute(
                "insert into imap_folders (name, uid_validity) values (%s, 1)"
                " on conflict (name) do update set uid_validity = 1 returning id",
                (folder,),
            ).fetchone()
            assert row is not None
            folder_id = int(next(iter(row)))
            conn.execute(
                "insert into imap_uids (folder_id, raw_sha256, uid)"
                " values (%s, %s, 1) on conflict do nothing",
                (folder_id, sha),
            )
            conn.commit()

        # Port 0 then close: a fixed port collides with the real server, which
        # on this machine is running on 1143 while the suite runs.
        probe = _socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        env = {
            **os.environ,
            "GMAIL_ARCHIVE_DATABASE_URL": DSN or "",
            "GMAIL_ARCHIVE_BLOB_DIR": str(blob_dir),
            "GMAIL_ARCHIVE_IMAP_PASSWORD": self.PASSWORD,
        }
        proc = subprocess.Popen(
            [
                "uv",
                "run",
                "gmail-archive",
                "imap",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                pytest.fail(f"IMAP server exited early:\n{out[-2000:]}")
            try:
                with _socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            proc.kill()
            pytest.fail(f"IMAP server never bound port {port}")

        try:
            yield port, folder, self.RAW
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
                conn.execute("delete from imap_uids where folder_id = %s", (folder_id,))
                conn.execute("delete from imap_folders where id = %s", (folder_id,))
                conn.execute("delete from messages where raw_sha256 = %s", (sha,))
                conn.execute("delete from blobs where sha256 = %s", (sha,))
                conn.commit()

    def test_login_list_select_fetch(self, served: tuple[int, str, bytes]) -> None:
        import imaplib

        port, folder, raw = served
        client = imaplib.IMAP4("127.0.0.1", port)
        try:
            assert client.login("archive", self.PASSWORD)[0] == "OK"

            status, folders = client.list()
            assert status == "OK"
            assert any(folder.encode() in line for line in folders if line)

            status, counts = client.select(folder, readonly=True)
            assert status == "OK"
            exists = counts[0]
            assert exists is not None
            assert int(exists) == 1

            status, data = client.fetch("1", "(RFC822)")
            assert status == "OK"
            fetched = next(
                part[1] for part in data if isinstance(part, tuple) and part[1]
            )
            # The whole point: the bytes a client receives are the bytes in the
            # blob store. `load_content` returning nothing is what #16's
            # seventh bug was, and it looked exactly like success.
            assert fetched.replace(b"\r\n", b"\n") == raw.replace(b"\r\n", b"\n")
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    def test_the_wrong_password_is_refused(
        self, served: tuple[int, str, bytes]
    ) -> None:
        import imaplib

        port, _, _ = served
        client = imaplib.IMAP4("127.0.0.1", port)
        try:
            with pytest.raises(imaplib.IMAP4.error):
                client.login("archive", "not-the-password")
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    def test_append_is_refused_over_the_wire(
        self, served: tuple[int, str, bytes]
    ) -> None:
        # Read-only is the archive's central promise; this is the only test
        # that checks it the way a client would actually discover it.
        import imaplib
        import time

        port, folder, _ = served
        client = imaplib.IMAP4("127.0.0.1", port)
        try:
            client.login("archive", self.PASSWORD)
            # `imaplib` returns ("NO", ...) for a refusal rather than raising;
            # it only raises on BAD. Asserting on the status is both correct
            # and stronger, because it pins the tagged response a real client
            # shows the user.
            status, detail = client.append(
                folder, "", time.localtime(), b"Subject: nope\r\n\r\nbody\r\n"
            )
            assert status == "NO"
            assert b"read-only" in detail[0].lower()
        finally:
            with contextlib.suppress(Exception):
                client.logout()
