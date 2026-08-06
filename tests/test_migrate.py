"""Migration runner tests.

The discovery half runs without a database — it is pure filesystem logic and
that is where the dangerous mistakes live (a misnamed file that silently never
runs). The apply half needs Postgres and skips cleanly without a DSN, so the
default suite stays Docker-free.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gmail_archive.migrate import discover

DSN = os.environ.get("GMAIL_ARCHIVE_TEST_DATABASE_URL")


class TestDiscovery:
    def test_finds_the_real_migrations(self) -> None:
        found = discover()
        assert found, "no migrations discovered"
        assert found[0].version == 1
        assert found[0].name == "initial"

    def test_returned_in_version_order(self, tmp_path: Path) -> None:
        for name in ("0003_c.sql", "0001_a.sql", "0002_b.sql"):
            (tmp_path / name).write_text("select 1;")
        assert [m.version for m in discover(tmp_path)] == [1, 2, 3]

    def test_a_misnamed_file_is_an_error_not_a_skip(self, tmp_path: Path) -> None:
        # Skipping it silently would mean a schema that differs between machines
        # with nothing to show for it.
        (tmp_path / "0001_ok.sql").write_text("select 1;")
        (tmp_path / "add_index.sql").write_text("select 1;")
        with pytest.raises(ValueError, match="not a valid migration name"):
            discover(tmp_path)

    def test_duplicate_versions_are_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "0001_a.sql").write_text("select 1;")
        (tmp_path / "0001_b.sql").write_text("select 1;")
        with pytest.raises(ValueError, match="duplicate migration version"):
            discover(tmp_path)

    def test_dotfiles_are_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "0001_a.sql").write_text("select 1;")
        (tmp_path / ".DS_Store").write_text("junk")
        assert len(discover(tmp_path)) == 1

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            discover(tmp_path / "nope")

    def test_initial_migration_is_readable_sql(self) -> None:
        sql = discover()[0].sql
        assert "create table if not exists messages" in sql
        # The three decisions most likely to be "cleaned up" by someone who does
        # not know why they are there.
        assert "nulls last" in sql
        assert "to_tsvector('english'" in sql
        assert "references blobs (sha256)" in sql


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestApply:
    def test_migrate_is_idempotent(self) -> None:
        import psycopg

        from gmail_archive.migrate import migrate, pending

        assert DSN is not None
        first = migrate(DSN)
        assert first, "expected at least one migration to run"
        second = migrate(DSN)
        assert second == [], "a second run must apply nothing"
        with psycopg.connect(DSN) as conn:
            assert pending(conn) == []

    def test_schema_supports_the_keyset_ordering(self) -> None:
        import psycopg

        from gmail_archive.migrate import migrate

        assert DSN is not None
        migrate(DSN)
        with psycopg.connect(DSN) as conn:
            # The query planner must be able to use the index for the exact
            # ordering query.py will emit; a mismatch here is a sequential scan
            # over the whole archive.
            plan = conn.execute(
                "explain select raw_sha256 from messages "
                "order by internal_date desc nulls last, raw_sha256 desc limit 50"
            ).fetchall()
            assert any("messages_keyset_idx" in str(row) for row in plan), plan
