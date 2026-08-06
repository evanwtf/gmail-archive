"""Archive integrity verification.

Reconciles the database against the content-addressed blob store and the source
mbox sightings. Designed to be run periodically or after a crash to detect
corruption, orphaned blobs, and missing data.

The `--deep` flag re-hashes every blob on disk against its sha256 filename,
which is the payoff of making the content hash the primary key: no stored
checksum to compare against, because the name *is* the checksum.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from gmail_archive.storage import BlobStore

logger = logging.getLogger(__name__)


@dataclass
class VerifyReport:
    """Result of a verification run."""

    messages_in_db: int
    sightings_in_db: int
    blobs_in_db: int
    blobs_on_disk: int
    orphaned_blobs: list[str]
    missing_blobs: list[str]
    deep_checked: int
    deep_corrupt: list[str]
    sighting_mismatch: int
    messages_without_sightings: int


def verify(
    conn: psycopg.Connection[object],
    store: BlobStore,
    *,
    deep: bool = False,
) -> VerifyReport:
    """Verify archive integrity.

    Args:
        conn: Database connection.
        store: Blob store instance.
        deep: If True, re-hash every blob on disk.

    Returns:
        A VerifyReport summarising the findings.
    """
    # ── Counts ────────────────────────────────────────────────────────────
    messages_in_db = int(
        conn.execute("select count(*) from messages").fetchone()[0]  # type: ignore[index]
    )
    sightings_in_db = int(
        conn.execute("select count(*) from message_sightings").fetchone()[0]  # type: ignore[index]
    )
    blobs_in_db = int(
        conn.execute("select count(*) from blobs").fetchone()[0]  # type: ignore[index]
    )

    blobs_on_disk = store.iter_blobs()
    blobs_on_disk_set = set(blobs_on_disk)

    # ── Orphaned blobs (on disk, not in database) ───────────────────────
    db_sha256s: set[str] = {
        str(r[0])  # type: ignore[index]
        for r in conn.execute("select sha256 from blobs").fetchall()
    }
    orphaned = sorted(blobs_on_disk_set - db_sha256s)

    # ── Missing blobs (in database, not on disk) ────────────────────────
    missing = sorted(db_sha256s - blobs_on_disk_set)

    # ── Sighting reconciliation ─────────────────────────────────────────
    sighting_mismatch = abs(messages_in_db - sightings_in_db)

    # Messages with no sighting at all (shouldn't happen in normal operation).
    raw = conn.execute(
        "select count(*) from messages m"
        " where not exists (select 1 from message_sightings s"
        "  where s.raw_sha256 = m.raw_sha256)"
    ).fetchone()
    messages_without_sightings = int(raw[0]) if raw else 0  # type: ignore[index]

    # ── Deep check ──────────────────────────────────────────────────────
    deep_checked = 0
    deep_corrupt: list[str] = []
    if deep:
        for sha256 in blobs_on_disk:
            deep_checked += 1
            if not store.verify(sha256):
                deep_corrupt.append(sha256)

    return VerifyReport(
        messages_in_db=messages_in_db,
        sightings_in_db=sightings_in_db,
        blobs_in_db=blobs_in_db,
        blobs_on_disk=len(blobs_on_disk),
        orphaned_blobs=orphaned,
        missing_blobs=missing,
        deep_checked=deep_checked,
        deep_corrupt=deep_corrupt,
        sighting_mismatch=sighting_mismatch,
        messages_without_sightings=messages_without_sightings,
    )
