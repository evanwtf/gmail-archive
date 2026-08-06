"""Message source protocol and data types.

Defines the ``MessageSource`` protocol that both the mbox reader and the Gmail
API source satisfy. The ingest pipeline can consume any ``MessageSource``
without knowing which backend provides the bytes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RawMessage:
    """A single raw RFC822 message from any source."""

    id: str
    """Source-specific message identifier (Gmail message ID or offset-based)."""

    bytes: bytes
    """Raw RFC822 message bytes."""


@dataclass(frozen=True)
class MessageBatch:
    """A page of messages from a source listing."""

    messages: list[RawMessage]
    """Messages in this batch."""

    next_page_token: str | None
    """Opaque token for the next page, or ``None`` if this is the last page."""


@dataclass(frozen=True)
class HistoryRecord:
    """A single history entry from the Gmail API history list."""

    history_id: str
    """The history ID for this record."""

    messages_added: list[str]
    """Message IDs that were added in this history record."""

    messages_deleted: list[str]
    """Message IDs that were deleted in this history record."""

    labels_added: list[tuple[str, list[str]]]
    """(message_id, label_ids) pairs for labels added."""

    labels_removed: list[tuple[str, list[str]]]
    """(message_id, label_ids) pairs for labels removed."""


class MessageSource(Protocol):
    """Protocol for message sources.

    Satisfied by both ``MboxSource`` (reads a local mbox file) and
    ``GmailApiSource`` (fetches from the Gmail API over HTTP).
    """

    async def list_messages(
        self,
        page_token: str | None = None,
        max_results: int = 50,
    ) -> MessageBatch:
        """List messages, with optional pagination.

        Args:
            page_token: Opaque token from a previous page, or ``None`` for the
                first page.
            max_results: Maximum number of messages to return in this batch.

        Returns:
            A ``MessageBatch`` with messages and the next page token.
        """
        ...

    async def get_message(self, message_id: str) -> RawMessage:
        """Fetch a single message by its source-specific identifier.

        Args:
            message_id: The source-specific message identifier.

        Returns:
            The raw RFC822 message bytes wrapped in a ``RawMessage``.
        """
        ...

    async def list_all(self, max_results: int = 50) -> AsyncIterator[RawMessage]:
        """Convenience: iterate over all messages, handling pagination.

        Args:
            max_results: Page size for each underlying ``list_messages`` call.

        Yields:
            ``RawMessage`` instances one at a time.
        """
        page_token: str | None = None
        while True:
            batch = await self.list_messages(
                page_token=page_token, max_results=max_results
            )
            for msg in batch.messages:
                yield msg
            if batch.next_page_token is None:
                break
            page_token = batch.next_page_token
