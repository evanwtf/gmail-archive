"""Message and LoadedMessage implementations for the gmail-archive IMAP backend.

Messages are read from the Postgres archive and the content-addressed blob store.
The raw RFC822 bytes are parsed by pymap's MIME parser on demand for FETCH
responses, with envelope and bodystructure cached in the database after backfill.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pymap.message import BaseLoadedMessage, BaseMessage
from pymap.mime import MessageContent
from pymap.parsing.specials import FetchRequirement, Flag, ObjectId

__all__ = ["LoadedMessage", "Message"]


class Message(BaseMessage):
    """A message in the gmail-archive IMAP backend.

    Unlike the dict backend, content is loaded lazily from the blob store
    rather than held in memory. The ``raw_bytes`` are fetched on demand when
    ``load_content()`` is called.
    """

    __slots__ = ["_raw_bytes", "_recent"]

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
        )

    @property
    def recent(self) -> bool:
        return self._recent

    @recent.setter
    def recent(self, recent: bool) -> None:
        self._recent = recent

    async def load_content(self, requirement: FetchRequirement) -> LoadedMessage:
        content: MessageContent | None = None
        if self._raw_bytes is not None:
            content = MessageContent.parse(self._raw_bytes)
        return LoadedMessage(self, requirement, content)


class LoadedMessage(BaseLoadedMessage):
    """Loaded message content for the gmail-archive IMAP backend.

    Delegates to pymap's ``BaseLoadedMessage`` which handles all the MIME
    parsing for FETCH BODY, BODYSTRUCTURE, ENVELOPE, etc.
    """

    pass
