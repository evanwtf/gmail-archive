"""Runtime configuration, entirely from the environment.

Nothing here may default to a host-specific path, core count, or memory size:
the deployment target is undecided and every value below has to survive a move
between machines as an .env edit plus a data copy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    return int(raw)


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    blob_dir: Path
    workers: int
    batch_size: int
    log_level: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("GMAIL_ARCHIVE_DATABASE_URL", ""),
            blob_dir=Path(os.environ.get("GMAIL_ARCHIVE_BLOB_DIR", "/blobs")),
            # os.cpu_count() rather than a pinned number: the candidate hosts
            # range from a 2c/4t i3 to a 12-core Ultra 5.
            workers=_int_env("GMAIL_ARCHIVE_WORKERS", os.cpu_count() or 1),
            batch_size=_int_env("GMAIL_ARCHIVE_BATCH_SIZE", 1000),
            log_level=os.environ.get("GMAIL_ARCHIVE_LOG_LEVEL", "INFO"),
        )
