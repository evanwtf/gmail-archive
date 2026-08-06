"""Message source abstractions.

Phase 8: defines a ``MessageSource`` protocol satisfied by both the mbox reader
and a ``GmailApiSource``. The Gmail API implementation is tested against respx
mocks — no real network calls.
"""

from gmail_archive.sources.gmail_api_source import GmailApiSource
from gmail_archive.sources.mbox_source import MboxSource
from gmail_archive.sources.protocol import (
    HistoryRecord,
    MessageBatch,
    MessageSource,
    RawMessage,
)

__all__ = [
    "GmailApiSource",
    "HistoryRecord",
    "MboxSource",
    "MessageBatch",
    "MessageSource",
    "RawMessage",
]
