"""Mbox-backed message source.

Adapts the existing byte-level mbox splitter to the ``MessageSource`` protocol.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from gmail_archive.mbox import read_message, scan
from gmail_archive.sources.protocol import MessageBatch, RawMessage

logger = logging.getLogger(__name__)


class MboxSource:
    """A ``MessageSource`` backed by a local mbox file.

    Messages are identified by their byte offset in the file, encoded as a
    decimal string. Pagination is offset-based (the page token is the index
    into the offsets list).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offsets: list[tuple[int, int]] | None = None

    def _lazy_scan(self) -> list[tuple[int, int]]:
        if self._offsets is None:
            result = scan(self._path)
            self._offsets = result.offsets
        return self._offsets

    async def list_messages(
        self,
        page_token: str | None = None,
        max_results: int = 50,
    ) -> MessageBatch:
        offsets = self._lazy_scan()
        start = int(page_token) if page_token is not None else 0
        chunk = offsets[start : start + max_results]

        messages = [
            RawMessage(
                id=str(offset),
                bytes=read_message(self._path, offset, length),
            )
            for offset, length in chunk
        ]

        next_start = start + len(chunk)
        next_page_token = str(next_start) if next_start < len(offsets) else None

        return MessageBatch(messages=messages, next_page_token=next_page_token)

    async def get_message(self, message_id: str) -> RawMessage:
        offset = int(message_id)
        offsets = self._lazy_scan()
        # Find the (offset, length) pair whose offset matches.
        for o, length in offsets:
            if o == offset:
                raw = read_message(self._path, o, length)
                return RawMessage(id=message_id, bytes=raw)
        raise KeyError(f"Message at offset {offset} not found")

    async def list_all(
        self,
        max_results: int = 50,
    ) -> AsyncIterator[RawMessage]:
        """Iterate over all messages, handling pagination."""
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

    @property
    def path(self) -> Path:
        return self._path

    @property
    def message_count(self) -> int:
        return len(self._lazy_scan())
