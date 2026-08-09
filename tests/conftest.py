"""Shared test setup.

`scripts/` holds standalone hook entry points rather than an installed package,
so it is put on the path here instead of with a sys.path edit inside each test
module.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


#: Headroom a full run needs under `tmp_path`. The integration tests each build
#: their own blob store, and `--pathologies` runs one per defect, so the suite
#: writes far more than a unit-test suite has any right to.
_TMPDIR_MIN_FREE_BYTES = 8 * 1024**3


def _keep_temp_files_off_a_ram_disk() -> None:
    """Point `tmp_path` at real disk when the default is too small.

    `/tmp` is tmpfs on the reference machine — 7.5 GB, in RAM. pytest's
    `tmp_path` lives under `tempfile.gettempdir()`, so a full suite run fills
    it, and this is not hypothetical: it exhausted tmpfs and every test that
    touched a file started failing with `OSError: [Errno 122]`. It also took
    out the surrounding tooling, which had its own scratch space on the same
    filesystem, and the disk it was really competing for had 158 GB free.

    Called at import time, before pytest builds its temp factory, because the
    factory resolves `TMPDIR` once and keeps it.

    Nothing happens if `TMPDIR` is already set or `--basetemp` was passed —
    an explicit choice wins, and CI makes one.
    """
    if os.environ.get("TMPDIR") or "--basetemp" in sys.argv:
        return
    current = Path(tempfile.gettempdir())
    try:
        if shutil.disk_usage(current).free >= _TMPDIR_MIN_FREE_BYTES:
            return
    except OSError:
        return
    repo_scratch = Path(__file__).resolve().parent.parent / ".tmp"
    for candidate in (Path("/var/tmp"), repo_scratch):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(candidate).free < _TMPDIR_MIN_FREE_BYTES:
                continue
        except OSError:
            continue
        os.environ["TMPDIR"] = str(candidate)
        tempfile.tempdir = None  # Drop the cached value from any earlier call.
        return


_keep_temp_files_off_a_ram_disk()


#: A three-message mbox, generated rather than committed.
#:
#: This used to live at `tests/fixtures/simple.mbox` — untracked, because
#: `.gitignore` excludes `*.mbox` and the pre-commit guard rejects them, both
#: deliberately. So four tests depended on a file that existed only on the
#: machine that first wrote it: green locally, broken for anyone cloning, and
#: invisible until CI ran on a clean checkout.
#:
#: Generating it keeps the "no .mbox in git, ever" rule intact and makes the
#: tests self-contained. Addresses are RFC 2606 reserved domains.
SIMPLE_MBOX = b"""From test@example.com Mon Jan 01 00:00:00 2024
Subject: First message
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Date: Mon, 1 Jan 2024 00:00:00 +0000
Message-ID: <msg1@example.com>

This is the first message.
From test@example.com Mon Jan 02 00:00:00 2024
Subject: Second message
From: Bob <bob@example.com>
To: Alice <alice@example.com>
Date: Tue, 2 Jan 2024 00:00:00 +0000
Message-ID: <msg2@example.com>

This is the second message.
From test@example.com Mon Jan 03 00:00:00 2024
Subject: Third message
From: Charlie <charlie@example.com>
To: Alice <alice@example.com>
Date: Wed, 3 Jan 2024 00:00:00 +0000
Message-ID: <msg3@example.com>

This is the third message.
"""


@pytest.fixture
def simple_mbox(tmp_path: Path) -> Path:
    """Path to a freshly written three-message mbox."""
    path = tmp_path / "simple.mbox"
    path.write_bytes(SIMPLE_MBOX)
    return path


@contextmanager
def scratch_database(dsn: str) -> Iterator[str]:
    """A short-lived empty database, dropped on the way out.

    Some tests cannot share the suite's database: the migration runner needs a
    virgin schema, and the export round trip exports *everything* it can see,
    so rows another test left behind change what it is asserting about.
    """
    name = f"gmail_archive_scratch_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(dsn, autocommit=True)
    try:
        admin.execute(f'create database "{name}"')
    finally:
        admin.close()

    parts = urlsplit(dsn)
    try:
        yield urlunsplit(parts._replace(path=f"/{name}"))
    finally:
        admin = psycopg.connect(dsn, autocommit=True)
        try:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity"
                " where datname = %s",
                (name,),
            )
            admin.execute(f'drop database if exists "{name}"')
        finally:
            admin.close()
