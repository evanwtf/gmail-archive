"""Query module tests.

The query module is the only place allowed to build read SQL against messages.
These tests verify the query functions work correctly against a real database.
"""

from __future__ import annotations

import os

import pytest

from gmail_archive.query import (
    ArchiveStats,
    LabelCount,
    get_message,
    get_message_full,
    list_labels,
    list_messages,
    list_messages_keyset,
    search,
    stats,
)

DSN = os.environ.get("GMAIL_ARCHIVE_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestStats:
    def test_stats_returns_archive_stats(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            result = stats(conn)
            assert isinstance(result, ArchiveStats)
            # The database may have data from other tests or the ingest fixture.
            assert result.total_messages >= 0
            assert result.total_blobs >= 0
            assert result.total_bytes >= 0

    def test_stats_after_inserting_data(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            sha256 = hashlib.sha256(b"test data").hexdigest()
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind) "
                "values (%s, 9, 'message') on conflict do nothing",
                (sha256,),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, subject) "
                "values (%s, 9, 'test') on conflict do nothing",
                (sha256,),
            )

            result = stats(conn)
            assert result.total_messages >= 1
            assert result.total_blobs >= 1

            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestSearch:
    def test_empty_query_returns_no_results(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            result = search(conn, "")
            assert result.total == 0
            assert result.messages == []

    def test_search_finds_matching_messages(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            sha256 = hashlib.sha256(b"hello world").hexdigest()
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind) "
                "values (%s, 11, 'message') on conflict do nothing",
                (sha256,),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, subject, "
                "search_text) values (%s, 11, 'hello', 'hello world') "
                "on conflict do nothing",
                (sha256,),
            )

            result = search(conn, "hello")
            assert result.total >= 1
            assert any("hello" in (m.subject or "") for m in result.messages)

            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestListMessages:
    def test_list_returns_messages(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            result = list_messages(conn)
            assert isinstance(result, list)

    def test_list_returns_newest_first(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            sha1 = hashlib.sha256(b"older").hexdigest()
            sha2 = hashlib.sha256(b"newer").hexdigest()
            for sha, subj, date in [
                (sha1, "older", "2020-01-01"),
                (sha2, "newer", "2024-01-01"),
            ]:
                conn.execute(
                    "insert into blobs (sha256, size_bytes, kind) "
                    "values (%s, 5, 'message') on conflict do nothing",
                    (sha,),
                )
                conn.execute(
                    "insert into messages (raw_sha256, size_bytes, subject, "
                    "internal_date) values (%s, 5, %s, %s::timestamptz) "
                    "on conflict do nothing",
                    (sha, subj, date),
                )

            result = list_messages(conn, limit=10)
            assert len(result) >= 2
            assert result[0].subject == "newer"

            conn.execute(
                "delete from messages where raw_sha256 in (%s, %s)", (sha1, sha2)
            )
            conn.execute("delete from blobs where sha256 in (%s, %s)", (sha1, sha2))


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestGetMessage:
    def test_get_nonexistent_message(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            result = get_message(conn, "0" * 64)
            assert result is None

    def test_get_existing_message(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            sha256 = hashlib.sha256(b"get test").hexdigest()
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind) "
                "values (%s, 8, 'message') on conflict do nothing",
                (sha256,),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, subject, "
                "from_addr) values (%s, 8, 'get test', 'a@e.com') "
                "on conflict do nothing",
                (sha256,),
            )

            result = get_message(conn, sha256)
            assert result is not None
            assert result.subject == "get test"
            assert result.from_addr == "a@e.com"

            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))


# ── Phase 6: new query functions ─────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestListLabels:
    def test_list_labels_returns_label_counts(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            result = list_labels(conn)
            assert isinstance(result, list)
            if result:
                assert isinstance(result[0], LabelCount)
                assert result[0].message_count > 0

    def test_list_labels_with_data(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            sha256 = hashlib.sha256(b"label test").hexdigest()
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind) "
                "values (%s, 9, 'message') on conflict do nothing",
                (sha256,),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes) "
                "values (%s, 9) on conflict do nothing",
                (sha256,),
            )
            conn.execute(
                "insert into labels (raw_sha256, label) values (%s, 'Inbox')",
                (sha256,),
            )
            conn.execute(
                "insert into labels (raw_sha256, label) values (%s, 'Important')",
                (sha256,),
            )

            result = list_labels(conn)
            assert len(result) >= 2
            labels = {lb.label: lb.message_count for lb in result}
            assert labels.get("Inbox", 0) >= 1
            assert labels.get("Important", 0) >= 1

            conn.execute("delete from labels where raw_sha256 = %s", (sha256,))
            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestListMessagesKeyset:
    def test_keyset_first_page(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            result = list_messages_keyset(conn, limit=10)
            assert isinstance(result, list)
            assert len(result) <= 10

    def test_keyset_pagination(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            sha1 = hashlib.sha256(b"keyset older").hexdigest()
            sha2 = hashlib.sha256(b"keyset newer").hexdigest()
            for sha, subj, date in [
                (sha1, "older", "2020-01-01"),
                (sha2, "newer", "2024-01-01"),
            ]:
                conn.execute(
                    "insert into blobs (sha256, size_bytes, kind) "
                    "values (%s, 12, 'message') on conflict do nothing",
                    (sha,),
                )
                conn.execute(
                    "insert into messages (raw_sha256, size_bytes, subject, "
                    "internal_date) values (%s, 12, %s, %s::timestamptz) "
                    "on conflict do nothing",
                    (sha, subj, date),
                )

            page1 = list_messages_keyset(conn, limit=10)
            assert len(page1) >= 2
            assert page1[0].subject == "newer"

            page2 = list_messages_keyset(
                conn,
                after_date=page1[0].internal_date,
                after_sha=page1[0].raw_sha256,
                limit=10,
            )
            assert all(m.raw_sha256 != page1[0].raw_sha256 for m in page2)

            conn.execute(
                "delete from messages where raw_sha256 in (%s, %s)",
                (sha1, sha2),
            )
            conn.execute(
                "delete from blobs where sha256 in (%s, %s)", (sha1, sha2)
            )


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestGetMessageFull:
    def test_get_full_nonexistent(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            result = get_message_full(conn, "0" * 64)
            assert result is None

    def test_get_full_with_labels(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            sha256 = hashlib.sha256(b"full test").hexdigest()
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind) "
                "values (%s, 9, 'message') on conflict do nothing",
                (sha256,),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, subject, "
                "from_addr) values (%s, 9, 'full test', 'a@e.com') "
                "on conflict do nothing",
                (sha256,),
            )
            conn.execute(
                "insert into labels (raw_sha256, label) values (%s, 'Inbox')",
                (sha256,),
            )

            result = get_message_full(conn, sha256)
            assert result is not None
            assert result.subject == "full test"
            assert "Inbox" in result.labels

            conn.execute("delete from labels where raw_sha256 = %s", (sha256,))
            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))
