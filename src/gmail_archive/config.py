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


def _bool_env(name: str) -> bool:
    """Truthy environment flag. Anything but 1/true/yes/on is false."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    blob_dir: Path
    workers: int
    batch_size: int
    log_level: str
    imap_password: str
    #: scrypt hash of the web UI password, from `gmail-archive set-password`.
    #: Empty means the UI is unauthenticated — which the app warns about
    #: loudly, because compose publishes it on 0.0.0.0.
    web_password_hash: str
    #: Whether an `X-Forwarded-For` header may be believed when identifying a
    #: client. Off by default: a forwarded header is trivially forged by
    #: anyone talking to the app directly, so believing it without a proxy in
    #: front turns the login throttle into decoration. See #47.
    trust_proxy: bool = False

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
            imap_password=os.environ.get("GMAIL_ARCHIVE_IMAP_PASSWORD", ""),
            web_password_hash=os.environ.get("GMAIL_ARCHIVE_WEB_PASSWORD_HASH", ""),
            trust_proxy=_bool_env("GMAIL_ARCHIVE_TRUST_PROXY"),
        )
