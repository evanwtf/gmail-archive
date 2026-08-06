"""Query module tests.

The query module is the only place allowed to build read SQL against messages.
These tests verify the query functions work correctly against a real database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gmail_archive.query import (
    ArchiveStats,
    SearchResult,
    get_message,
    list_messages,
    search,
    stats,
)

DSN = os.environ.get("GMAIL_ARCHIVE_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestStats:
    def test_stats_on_empty_database(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:
            result = stats(conn)
            assert isinstance(result, ArchiveStats)
            assert result.total_messages == 0
            assert result.total_blobs == 0
            assert result.total_bytes == 0

    def test_stats_after_inserting_data(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:
            # Insert a blob and message.
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

            # Clean up
            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestSearch:
    def test_empty_query_returns_no_results(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:
            result = search(conn, "")
            assert result.total == 0
            assert result.messages == []

    def test_search_finds_matching_messages(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:
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
            assert any("hello" in m.subject or "" for m in result.messages)

            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestListMessages:
    def test_list_on_empty_database(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:
            result = list_messages(conn)
            assert result == []

    def test_list_returns_newest_first(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:
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
            # Newest first.
            assert result[0].subject == "newer"

            conn.execute("delete from messages where raw_sha256 in (%s, %s)", (sha1, sha2))
            conn.execute("delete from blobs where sha256 in (%s, %s)", (sha1, sha2))


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestGetMessage:
    def test_get_nonexistent_message(self) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:
            result = get_message(conn, "0" * 64)
            assert result is None

    def test_get_existing_message(self) -> None:
        import hashlib

        import psycopg

        with psycopg.connect(DSN) as conn:
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
