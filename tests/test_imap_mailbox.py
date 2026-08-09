"""IMAP mailbox tests.

The IMAP layer has had no tests beyond authentication (#16), and #45 is what
that costs: `snapshot()` materialised the entire folder to produce three
integers, and nothing noticed because it *worked*. Every earlier IMAP bug was
"it does not work at all"; this one was only expensive.
"""

from __future__ import annotations

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
