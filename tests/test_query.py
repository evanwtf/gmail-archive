"""Query module tests.

The query module is the only place allowed to build read SQL against messages.
These tests verify the query functions work correctly against a real database.
"""

from __future__ import annotations

import os

import pytest

from gmail_archive.query import (
    DEFAULT_SEARCH_SORT,
    SEARCH_SORTS,
    ArchiveStats,
    LabelCount,
    MessageRow,
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


class TestMessageRowLabels:
    """The list UI is driven by Gmail's own labels, carried through Takeout."""

    def _row(self, *labels: str) -> MessageRow:
        return MessageRow(
            raw_sha256="0" * 64,
            subject="s",
            from_addr="a@example.com",
            to_addrs=[],
            internal_date=None,
            thread_id=None,
            labels=list(labels),
        )

    def test_gmail_state_flags(self) -> None:
        row = self._row("Unread", "Starred", "Important")
        assert (row.is_unread, row.is_starred, row.is_important) == (True, True, True)

    def test_absent_labels_are_false_not_missing(self) -> None:
        row = self._row()
        assert (row.is_unread, row.is_starred, row.is_important) == (
            False,
            False,
            False,
        )

    def test_user_labels_exclude_system_and_category(self) -> None:
        row = self._row(
            "Inbox", "Unread", "Category Promotions", "Amazon", "Bank Alerts"
        )
        assert row.user_labels == ["Amazon", "Bank Alerts"]

    def test_a_row_with_only_system_labels_shows_no_chips(self) -> None:
        assert self._row("Inbox", "Important", "Opened").user_labels == []


class TestSearchSorts:
    """No database needed: sort selection is resolved before any SQL runs."""

    def test_default_sort_is_a_known_key(self) -> None:
        assert DEFAULT_SEARCH_SORT in SEARCH_SORTS

    def test_default_sort_is_newest_first(self) -> None:
        # Deliberate: the common query here is a sender or a domain, where
        # every hit is equally "relevant" and ts_rank orders arbitrarily.
        assert DEFAULT_SEARCH_SORT == "date"
        assert SEARCH_SORTS[DEFAULT_SEARCH_SORT].startswith("internal_date desc")

    def test_default_sort_does_not_rank(self) -> None:
        # ts_rank in the default ordering would mean paying for the rank
        # expression on every search that never displays it.
        assert "ts_rank" not in SEARCH_SORTS[DEFAULT_SEARCH_SORT]

    def test_expected_sorts_are_offered(self) -> None:
        assert set(SEARCH_SORTS) == {"relevance", "date", "date-asc"}

    def test_unknown_sort_raises_before_touching_the_connection(self) -> None:
        # conn=None proves the guard runs first: reaching SQL would AttributeError.
        with pytest.raises(ValueError, match="unknown sort"):
            search(None, "anything", sort="; drop table messages --")  # type: ignore[arg-type]

    def test_no_sort_clause_interpolates_caller_input(self) -> None:
        # The caller's string selects a key; only these fixed clauses reach SQL.
        for clause in SEARCH_SORTS.values():
            assert "%(q)s" in clause or "%" not in clause

    def test_every_sort_breaks_ties_on_raw_sha256(self) -> None:
        # Without a total order, two requests can disagree about page boundaries
        # and OFFSET pagination silently skips or repeats rows.
        for name, clause in SEARCH_SORTS.items():
            assert "raw_sha256" in clause, name

    def test_undated_messages_sort_last_in_every_ordering(self) -> None:
        for name, clause in SEARCH_SORTS.items():
            assert "nulls last" in clause, name


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

    def test_sort_by_date_orders_newest_first(self) -> None:
        import hashlib
        from datetime import UTC, datetime

        import psycopg

        # Three matching messages, inserted in an order that is neither the
        # date order nor its reverse, so a passing result cannot be luck.
        rows = [
            (
                hashlib.sha256(b"sortable-b").hexdigest(),
                datetime(2015, 6, 1, tzinfo=UTC),
            ),
            (
                hashlib.sha256(b"sortable-c").hexdigest(),
                datetime(2021, 6, 1, tzinfo=UTC),
            ),
            (
                hashlib.sha256(b"sortable-a").hexdigest(),
                datetime(2009, 6, 1, tzinfo=UTC),
            ),
        ]
        undated = hashlib.sha256(b"sortable-undated").hexdigest()

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            for sha, when in rows:
                conn.execute(
                    "insert into blobs (sha256, size_bytes, kind) "
                    "values (%s, 9, 'message') on conflict do nothing",
                    (sha,),
                )
                conn.execute(
                    "insert into messages (raw_sha256, size_bytes, subject,"
                    " search_text, internal_date)"
                    " values (%s, 9, 'zorkmid', 'zorkmid', %s)"
                    " on conflict do nothing",
                    (sha, when),
                )
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind) "
                "values (%s, 9, 'message') on conflict do nothing",
                (undated,),
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, subject,"
                " search_text, internal_date)"
                " values (%s, 9, 'zorkmid', 'zorkmid', null)"
                " on conflict do nothing",
                (undated,),
            )

            try:
                newest = search(conn, "zorkmid", sort="date")
                dates = [m.internal_date for m in newest.messages]
                assert dates == sorted(
                    (d for d in dates if d is not None), reverse=True
                ) + [d for d in dates if d is None]
                assert dates[0] == datetime(2021, 6, 1, tzinfo=UTC)
                # An undated message must not lead a newest-first list.
                assert dates[-1] is None

                oldest = search(conn, "zorkmid", sort="date-asc")
                asc = [m.internal_date for m in oldest.messages]
                assert asc[0] == datetime(2009, 6, 1, tzinfo=UTC)
                # ...nor an oldest-first one: unknown is not old.
                assert asc[-1] is None
            finally:
                for sha in [r[0] for r in rows] + [undated]:
                    conn.execute("delete from messages where raw_sha256 = %s", (sha,))
                    conn.execute("delete from blobs where sha256 = %s", (sha,))


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
            conn.execute("delete from blobs where sha256 in (%s, %s)", (sha1, sha2))


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
