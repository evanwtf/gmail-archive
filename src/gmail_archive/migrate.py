"""Numbered `.sql` migrations, applied by a runner small enough to read.

No Alembic. The schema is a few hand-written tables that will change rarely
after Phase 4, and an ORM-shaped migration tool would be a large dependency
whose autogeneration this project would never use. What is actually needed is:
apply files in order, exactly once, and record which ran.

Each migration runs **inside a transaction together with its `schema_migrations`
row**, so a failure half way through leaves neither the DDL nor the claim that
it was applied. Postgres has transactional DDL, which is what makes that
possible; this would not be safe on MySQL.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"

_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

_BOOTSTRAP = """
create table if not exists schema_migrations (
    version     integer     primary key,
    name        text        not null,
    applied_at  timestamptz not null default now()
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Every migration on disk, in version order.

    A filename that does not match `NNNN_name.sql` is an error rather than a
    file to skip: a typo'd migration that silently never runs is a schema that
    silently differs between machines.
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"no migrations directory at {directory}")

    found: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        match = _FILENAME.match(path.name)
        if match is None:
            raise ValueError(
                f"{path.name!r} is not a valid migration name; expected NNNN_name.sql"
            )
        found.append(Migration(int(match.group(1)), match.group(2), path))

    versions = [m.version for m in found]
    duplicates = {v for v in versions if versions.count(v) > 1}
    if duplicates:
        raise ValueError(f"duplicate migration version(s): {sorted(duplicates)}")
    return found


def applied_versions(conn: psycopg.Connection[object]) -> set[int]:
    conn.execute(_BOOTSTRAP)
    rows = conn.execute("select version from schema_migrations").fetchall()
    return {int(row[0]) for row in rows}  # type: ignore[index]


def pending(
    conn: psycopg.Connection[object], directory: Path = MIGRATIONS_DIR
) -> list[Migration]:
    done = applied_versions(conn)
    return [m for m in discover(directory) if m.version not in done]


def migrate(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Apply every pending migration. Returns the ones that ran."""
    ran: list[Migration] = []
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        # Log what was found and where. In the container the migrations are
        # baked into the image, not read from the working tree, so adding a
        # file and running `docker compose run --rm web migrate` silently does
        # nothing until the image is rebuilt — and reports {"applied": 0},
        # which reads like success. Naming the directory and the filenames
        # turns that into something visible.
        discovered = discover(directory)
        logger.info(
            "migrations directory %s contains %d file(s): %s",
            directory,
            len(discovered),
            ", ".join(f"{m.version:04d}_{m.name}" for m in discovered) or "none",
        )
        todo = pending(conn, directory)
        if not todo:
            logger.info("schema is up to date")
            return ran

        for migration in todo:
            logger.info("applying %04d_%s", migration.version, migration.name)
            # One transaction per migration, covering both the DDL and the row
            # recording it.
            with conn.transaction():
                conn.execute(migration.sql)
                conn.execute(
                    "insert into schema_migrations (version, name) values (%s, %s)",
                    (migration.version, migration.name),
                )
            ran.append(migration)

    logger.info("applied %d migration(s)", len(ran))
    return ran
