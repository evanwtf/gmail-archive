"""Query module tests.

The query module is the only place allowed to build read SQL against messages.
These tests verify the query functions work correctly against a real database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from gmail_archive.query import (
    DEFAULT_SEARCH_SORT,
    SEARCH_SORTS,
    ArchiveStats,
    LabelCount,
    MessageRow,
    get_message,
    get_message_full,
    ingest_runs,
    last_import_finished,
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
        # Implausible future dates are demoted first (#27), so the date term
        # is no longer the leading one — but it must still be there and still
        # be descending.
        clause = SEARCH_SORTS[DEFAULT_SEARCH_SORT]
        assert "internal_date desc nulls last" in clause
        assert clause.startswith("(internal_date > now()")

    def test_implausible_dates_sort_last_in_every_date_ordering(self) -> None:
        # A Date header of 2611 is a broken header, not the newest mail in
        # the archive — but newest-first put it above everything (#27).
        assert "now() + interval" in SEARCH_SORTS["date"]
        assert "now() + interval" in SEARCH_SORTS["relevance"]
        # Oldest-first is unaffected: a future date is already last there.
        assert "now() + interval" not in SEARCH_SORTS["date-asc"]

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
                "insert into labels (raw_sha256, label, account_id)"
                " values (%s, 'Inbox', (select min(id) from accounts))",
                (sha256,),
            )
            conn.execute(
                "insert into labels (raw_sha256, label, account_id)"
                " values (%s, 'Important', (select min(id) from accounts))",
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
                "insert into labels (raw_sha256, label, account_id)"
                " values (%s, 'Inbox', (select min(id) from accounts))",
                (sha256,),
            )

            result = get_message_full(conn, sha256)
            assert result is not None
            assert result.subject == "full test"
            assert "Inbox" in result.labels

            conn.execute("delete from labels where raw_sha256 = %s", (sha256,))
            conn.execute("delete from messages where raw_sha256 = %s", (sha256,))
            conn.execute("delete from blobs where sha256 = %s", (sha256,))


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestLikeMetacharactersAreLiteral:
    """`%` and `_` in an operator value match themselves (#43).

    Parameterisation stops injection; it does nothing about over-matching.
    `from:%` built `ilike '%%%'` and matched the entire archive, and
    `from:first_last` also matched `firstXlast` — silently broader than what
    was typed, which is worse than an error.
    """

    #: Addresses that differ only in the characters LIKE treats as wildcards.
    ROWS = (
        ("literal-underscore", "first_last@example.com", "a_b invoice"),
        ("wildcard-would-match", "firstXlast@example.com", "aXb invoice"),
        ("literal-percent", "sale100%@example.com", "100% off"),
        ("plain", "someone@example.com", "nothing special"),
    )

    @pytest.fixture
    def seeded(self) -> Iterator[dict[str, str]]:
        import hashlib

        import psycopg

        shas = {
            name: hashlib.sha256(f"like-{name}".encode()).hexdigest()
            for name, _, _ in self.ROWS
        }
        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            for name, addr, subject in self.ROWS:
                sha = shas[name]
                conn.execute(
                    "insert into blobs (sha256, size_bytes, kind)"
                    " values (%s, 10, 'message') on conflict do nothing",
                    (sha,),
                )
                conn.execute(
                    "insert into messages (raw_sha256, size_bytes, from_addr,"
                    " subject, search_text) values (%s, 10, %s, %s, %s)"
                    " on conflict do nothing",
                    (sha, addr, subject, subject),
                )
            conn.commit()
        try:
            yield shas
        finally:
            with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
                for sha in shas.values():
                    conn.execute("delete from messages where raw_sha256 = %s", (sha,))
                    conn.execute("delete from blobs where sha256 = %s", (sha,))
                conn.commit()

    def _from_addrs(self, query: str) -> set[str]:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            return {m.from_addr or "" for m in search(conn, query, limit=50).messages}

    def test_underscore_does_not_match_any_character(
        self, seeded: dict[str, str]
    ) -> None:
        found = self._from_addrs("from:first_last")
        assert "first_last@example.com" in found
        assert "firstXlast@example.com" not in found

    def test_a_bare_percent_does_not_match_everything(
        self, seeded: dict[str, str]
    ) -> None:
        # The worst case: this used to return the whole archive.
        found = self._from_addrs("from:%")
        assert "sale100%@example.com" in found
        assert "someone@example.com" not in found

    def test_a_literal_percent_in_an_address_matches_itself(
        self, seeded: dict[str, str]
    ) -> None:
        found = self._from_addrs("from:100%@example.com")
        assert found == {"sale100%@example.com"}

    def test_subject_underscore_is_literal_too(self, seeded: dict[str, str]) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            subjects = {
                m.subject or "" for m in search(conn, "subject:a_b", limit=50).messages
            }
        assert "a_b invoice" in subjects
        assert "aXb invoice" not in subjects

    def test_an_ordinary_value_is_unaffected(self, seeded: dict[str, str]) -> None:
        assert "someone@example.com" in self._from_addrs("from:someone")


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestImportProvenance:
    """`ingest_runs` and `last_import_finished`, which date the archive.

    The badge in the top bar is the only thing in the UI that says how stale
    the contents are, so the rule it depends on — only a cleanly finished run
    counts — is worth pinning down.
    """

    @pytest.fixture
    def runs(self) -> Iterator[dict[str, int]]:
        import psycopg

        source = "/tmp/test-import-provenance.mbox"
        ids: dict[str, int] = {}
        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            for name, status, finished in (
                ("done", "complete", "2020-01-02 03:04:05+00"),
                ("broke", "failed", "2024-06-07 08:09:10+00"),
            ):
                row = conn.execute(
                    "insert into ingest_runs (source_path, started_at,"
                    " finished_at, messages_seen, messages_new, status)"
                    " values (%s, '2020-01-01 00:00:00+00', %s, 5, 5, %s)"
                    " returning id",
                    (f"{source}.{name}", finished, status),
                ).fetchone()
                assert row is not None
                ids[name] = int(next(iter(row)))
            conn.commit()
        try:
            yield ids
        finally:
            with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
                for run_id in ids.values():
                    conn.execute("delete from ingest_runs where id = %s", (run_id,))
                conn.commit()

    def test_a_failed_run_does_not_date_the_archive(self, runs: dict[str, int]) -> None:
        """The whole point of `last_import_finished` over `max(finished_at)`.

        The failed run finished four years later than the complete one. If it
        counted, the badge would claim a freshness the archive does not have.
        """
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            when = last_import_finished(conn)
        assert when is not None
        assert when.year != 2024

    def test_runs_are_listed_newest_first(self, runs: dict[str, int]) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            listed = ingest_runs(conn)
        started = [r.started_at for r in listed]
        assert started == sorted(started, reverse=True)

    def test_a_run_carries_its_source_and_status(self, runs: dict[str, int]) -> None:
        import psycopg

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            by_id = {r.id: r for r in ingest_runs(conn)}
        broke = by_id[runs["broke"]]
        assert broke.status == "failed"
        assert broke.source_path.endswith(".broke")
        # No sightings were written against this path, so the count is None
        # rather than a misleading zero-that-might-be-a-real-zero.
        assert broke.source_sightings is None
        assert broke.newest_message is None

    def test_duration_is_none_while_a_run_is_unfinished(self) -> None:
        from datetime import UTC, datetime

        from gmail_archive.query import IngestRun

        run = IngestRun(
            id=1,
            source_path="x",
            started_at=datetime(2020, 1, 1, tzinfo=UTC),
            finished_at=None,
            checkpoint_offset=0,
            messages_seen=0,
            messages_new=0,
            failures=0,
            status="running",
            account_address=None,
            source_sightings=None,
            oldest_message=None,
            newest_message=None,
        )
        assert run.duration_seconds is None
        run.finished_at = datetime(2020, 1, 1, 0, 43, 5, tzinfo=UTC)
        assert run.duration_seconds == 2585.0
