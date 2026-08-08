"""Migration runner tests.

The discovery half runs without a database — it is pure filesystem logic and
that is where the dangerous mistakes live (a misnamed file that silently never
runs). The apply half needs Postgres and skips cleanly without a DSN, so the
default suite stays Docker-free.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from conftest import scratch_database

DSN = os.environ.get("GMAIL_ARCHIVE_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestApply:
    def test_migrate_is_idempotent(self) -> None:
        from gmail_archive.migrate import migrate, pending

        assert DSN is not None
        # Against its own throwaway database. Asserting "at least one
        # migration ran" only holds on a virgin schema, and any environment
        # that applied the schema before running the suite — CI does, so the
        # web tests have tables — made this fail.
        with scratch_database(DSN) as scratch_dsn:
            first = migrate(scratch_dsn)
            assert first, "expected at least one migration to run"
            second = migrate(scratch_dsn)
            assert second == [], "a second run must apply nothing"
            with psycopg.connect(scratch_dsn) as conn:
                assert pending(conn) == []

    def test_schema_supports_the_keyset_ordering(self) -> None:
        from gmail_archive.migrate import migrate

        assert DSN is not None
        migrate(DSN)

        # A sequential scan is the right plan on a tiny table however good the
        # index is, so the index can only be shown to work with enough rows to
        # make it the cheaper option. The rows are dated in the distant past
        # and deleted afterwards: this is a shared database, and 2000 rows
        # left behind changed what "newest first" meant for other tests.
        seed = (
            "select md5(g::text) || md5((g + 1)::text) from generate_series(1, 2000) g"
        )
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "insert into blobs (sha256, size_bytes, kind)"
                f" select s, 1, 'message' from ({seed}) as t(s)"
                " on conflict do nothing"
            )
            conn.execute(
                "insert into messages (raw_sha256, size_bytes, internal_date)"
                f" select s, 1, timestamptz '1995-01-01' from ({seed}) as t(s)"
                " on conflict do nothing"
            )
            conn.execute("analyze messages")
            try:
                # The planner must be able to use the index for the exact
                # ordering query.py emits; a mismatch is a sequential scan
                # over the whole archive.
                plan = conn.execute(
                    "explain select raw_sha256 from messages"
                    " order by internal_date desc nulls last, raw_sha256 desc"
                    " limit 50"
                ).fetchall()
                assert any("messages_keyset_idx" in str(row) for row in plan), plan
            finally:
                conn.execute(f"delete from messages where raw_sha256 in ({seed})")
                conn.execute(f"delete from blobs where sha256 in ({seed})")
                conn.commit()
