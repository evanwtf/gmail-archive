"""Message and LoadedMessage implementations for the gmail-archive IMAP backend.

Messages are read from the Postgres archive and the content-addressed blob store.
The raw RFC822 bytes are parsed by pymap's MIME parser on demand for FETCH
responses, with envelope and bodystructure cached in the database after backfill.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime

from pymap.message import BaseLoadedMessage, BaseMessage
from pymap.mime import MessageContent
from pymap.parsing.specials import FetchRequirement, Flag, ObjectId

from gmail_archive.storage import BlobStore

logger = logging.getLogger(__name__)

__all__ = ["LoadedMessage", "Message"]


class Message(BaseMessage):
    """A message in the gmail-archive IMAP backend.

    Unlike the dict backend, content is loaded lazily from the blob store
    rather than held in memory. The ``raw_bytes`` are fetched on demand when
    ``load_content()`` is called.
    """

    __slots__ = ["_raw_bytes", "_raw_sha256", "_recent", "_store"]

    def __init__(
        self,
        uid: int,
        internal_date: datetime,
        permanent_flags: Iterable[Flag],
        *,
        expunged: bool = False,
        email_id: ObjectId | None = None,
        thread_id: ObjectId | None = None,
        recent: bool = False,
        raw_bytes: bytes | None = None,
        raw_sha256: str | None = None,
        store: BlobStore | None = None,
    ) -> None:
        super().__init__(
            uid,
            internal_date,
            permanent_flags,
            expunged=expunged,
            email_id=email_id,
            thread_id=thread_id,
        )
        self._raw_bytes = raw_bytes
        self._raw_sha256 = raw_sha256
        self._store = store
        self._recent = recent

    @classmethod
    def copy(
        cls,
        msg: Message,
        *,
        uid: int | None = None,
        recent: bool = False,
        expunged: bool = False,
    ) -> Message:
        if uid is None:
            uid = msg.uid
        return cls(
            uid,
            msg.internal_date,
            msg.permanent_flags,
            expunged=expunged,
            email_id=msg.email_id,
            thread_id=msg.thread_id,
            recent=recent,
            raw_bytes=msg._raw_bytes,
            raw_sha256=msg._raw_sha256,
            store=msg._store,
        )

    @property
    def recent(self) -> bool:
        return self._recent

    @recent.setter
    def recent(self, recent: bool) -> None:
        self._recent = recent

    async def load_content(self, requirement: FetchRequirement) -> LoadedMessage:
        """Parse the message, reading it from the blob store if needed.

        This is the lazy loading the class docstring has always described and
        never did: `_raw_bytes` was only ever set if a caller passed it in,
        and nothing did, so every FETCH returned `RFC822.SIZE 0` and an empty
        body. Holding 277k messages in memory is not an option, so the bytes
        are read per FETCH — the blob store is a content-addressed file read,
        which is about as cheap as that gets.
        """
        raw = self._raw_bytes
        if raw is None and self._store is not None and self._raw_sha256:
            try:
                raw = self._store.get(self._raw_sha256)
            except OSError:
                # A missing blob is a real possibility on a damaged archive
                # (`verify` reports them). An empty body beats killing the
                # client's whole FETCH.
                logger.warning("blob missing for %s", self._raw_sha256)
                raw = None
        content = MessageContent.parse(raw) if raw is not None else None
        return LoadedMessage(self, requirement, content)


class LoadedMessage(BaseLoadedMessage):
    """Loaded message content for the gmail-archive IMAP backend.

    Delegates to pymap's ``BaseLoadedMessage`` which handles all the MIME
    parsing for FETCH BODY, BODYSTRUCTURE, ENVELOPE, etc.
    """

    pass
