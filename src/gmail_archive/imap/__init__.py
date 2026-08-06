"""Read-only IMAP server backend for gmail-archive.

Maps Gmail labels to IMAP folders, serves messages from the Postgres archive
and the content-addressed blob store. Built on pymap.
"""

from __future__ import annotations

from .backend import GmailArchiveBackend

__all__ = ["GmailArchiveBackend"]
