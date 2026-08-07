"""MailboxData and MailboxSet implementations for the gmail-archive IMAP backend.

Mailboxes are mapped from Gmail labels. Messages are read from the Postgres
archive. The backend is read-only: APPEND, COPY, MOVE, DELETE, and flag updates
are not supported.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, Iterable
from datetime import UTC, datetime
from typing import Any

from pymap.backend.mailbox import MailboxDataInterface, MailboxSetInterface
from pymap.concurrent import Event, ReadWriteLock
from pymap.context import subsystem
from pymap.exceptions import MailboxReadOnly, NotAllowedError
from pymap.flags import FlagOp
from pymap.interfaces.message import CachedMessage
from pymap.listtree import ListTree
from pymap.mailbox import MailboxSnapshot
from pymap.parsing.message import AppendMessage
from pymap.parsing.specials import ObjectId
from pymap.parsing.specials.flag import Flag, Recent, Seen
from pymap.selected import SelectedMailbox, SelectedSet

from .message import Message

logger = logging.getLogger(__name__)

# System flags we support (read-only, so we just report them).
_PERMANENT_FLAGS: frozenset[Flag] = frozenset(
    {Seen, Flag("\\Answered"), Flag("\\Flagged"), Flag("\\Draft"), Flag("\\Deleted")}
)
_SESSION_FLAGS: frozenset[Flag] = frozenset({Recent})


class MailboxData(MailboxDataInterface[Message]):
    """A read-only mailbox backed by a Gmail label.

    Messages are loaded from the database on demand. UIDs are assigned per
    folder and stored in ``imap_uids``.
    """

    def __init__(
        self,
        folder_id: int,
        name: str,
        uid_validity: int,
        conn_factory: Any,
    ) -> None:
        self._mailbox_id = ObjectId.random_mailbox_id()
        self._folder_id = folder_id
        self._name = name
        self._uid_validity = uid_validity
        self._conn_factory = conn_factory
        self._readonly = True
        self._updated = subsystem.get().new_event()
        self._messages_lock = subsystem.get().new_rwlock()
        self._selected_set = SelectedSet()
        # Track the max UID so we can report next_uid in snapshots.
        self._max_uid = 0

    @property
    def mailbox_id(self) -> ObjectId:
        return self._mailbox_id

    @property
    def readonly(self) -> bool:
        return self._readonly

    @property
    def uid_validity(self) -> int:
        return self._uid_validity

    @property
    def messages_lock(self) -> ReadWriteLock:
        return self._messages_lock

    @property
    def selected_set(self) -> SelectedSet:
        return self._selected_set

    async def update_selected(
        self, selected: SelectedMailbox, *, wait_on: Event | None = None
    ) -> SelectedMailbox:
        if wait_on is not None:
            either_event = wait_on.or_event(self._updated)
            await either_event.wait()
        mod_sequence = selected.mod_sequence
        selected.mod_sequence = 0  # No mod sequences in read-only backend
        if mod_sequence is None:
            # First select: load all messages
            all_messages = await self._load_all_messages()
            selected.add_updates(all_messages, [])
        # For subsequent checks, there are no updates (read-only)
        return selected

    async def _load_all_messages(self) -> list[Message]:
        """Load all messages for this folder from the database."""
        messages: list[Message] = []
        pool = await self._conn_factory()
        async with pool.connection() as conn:
            rows = await conn.execute(
                """
                SELECT u.uid, m.raw_sha256, m.internal_date, m.subject, m.from_addr
                FROM imap_uids u
                JOIN messages m ON m.raw_sha256 = u.raw_sha256
                WHERE u.folder_id = %s
                ORDER BY u.uid
                """,
                (self._folder_id,),
            )
            async for row in rows:
                uid = row[0]
                internal_date = row[1] or datetime.now(UTC)
                msg = Message(
                    uid,
                    internal_date,
                    _PERMANENT_FLAGS,
                    email_id=ObjectId(row[2][:16].encode()),
                    thread_id=ObjectId(b"0" * 16),
                )
                messages.append(msg)
                if uid > self._max_uid:
                    self._max_uid = uid
        return messages

    async def append(
        self, append_msg: AppendMessage, *, recent: bool = False
    ) -> Message:
        raise MailboxReadOnly(self._name)

    async def copy(
        self, uid: int, destination: MailboxData, *, recent: bool = False
    ) -> int | None:
        raise MailboxReadOnly(self._name)

    async def move(
        self, uid: int, destination: MailboxData, *, recent: bool = False
    ) -> int | None:
        raise MailboxReadOnly(self._name)

    async def get(self, uid: int, cached_msg: CachedMessage) -> Message:
        if uid < 1 or uid > self._max_uid:
            raise IndexError(uid)
        async with self._messages_lock.read_lock():
            if isinstance(cached_msg, Message):
                return Message.copy(cached_msg, expunged=False)
            raise TypeError(cached_msg)

    async def update(
        self,
        uid: int,
        cached_msg: CachedMessage,
        flag_set: frozenset[Flag],
        mode: FlagOp,
    ) -> Message:
        raise MailboxReadOnly(self._name)

    async def delete(self, uids: Iterable[int]) -> None:
        raise MailboxReadOnly(self._name)

    async def claim_recent(self, selected: SelectedMailbox) -> None:
        # No recent messages in a read-only backend
        pass

    async def cleanup(self) -> None:
        pass

    async def messages(self) -> AsyncIterable[Message]:
        async with self._messages_lock.read_lock():
            for msg in await self._load_all_messages():
                yield msg

    async def snapshot(self) -> MailboxSnapshot:
        exists = 0
        recent = 0
        unseen = 0
        first_unseen: int | None = None
        async for msg in self.messages():
            exists += 1
            if msg.recent:
                recent += 1
            if Seen not in msg.permanent_flags:
                unseen += 1
                if first_unseen is None:
                    first_unseen = exists
        return MailboxSnapshot(
            self.mailbox_id,
            self.readonly,
            self.uid_validity,
            _PERMANENT_FLAGS,
            _SESSION_FLAGS,
            exists,
            recent,
            unseen,
            first_unseen,
            self._max_uid + 1,
        )


class MailboxSet(MailboxSetInterface[MailboxData]):
    """Manages the set of mailboxes (Gmail labels) available to the user.

    Mailboxes are synced from the ``labels`` table on every list operation,
    so new labels appear without a restart.
    """

    def __init__(self, conn_factory: Any) -> None:
        super().__init__()
        self._conn_factory = conn_factory
        self._delimiter = "/"

    @property
    def delimiter(self) -> str:
        return self._delimiter

    async def _sync_folders(self) -> dict[str, tuple[int, int]]:
        """Sync folders from the database labels table.

        Returns a dict of ``{name: (folder_id, uid_validity)}``.
        """
        pool = await self._conn_factory()
        async with pool.connection() as conn:
            # Ensure INBOX exists
            await conn.execute(
                """
                INSERT INTO imap_folders (name, uid_validity)
                SELECT 'INBOX', 1
                WHERE NOT EXISTS (SELECT 1 FROM imap_folders WHERE name = 'INBOX')
                """
            )
            # Ensure a folder exists for every distinct label
            await conn.execute(
                """
                INSERT INTO imap_folders (name, uid_validity)
                SELECT DISTINCT l.label, 1
                FROM labels l
                WHERE NOT EXISTS (SELECT 1 FROM imap_folders f WHERE f.name = l.label)
                """
            )
            rows = await conn.execute(
                "SELECT id, name, uid_validity FROM imap_folders ORDER BY name"
            )
            return {row[1]: (row[0], row[2]) async for row in rows}

    async def set_subscribed(self, name: str, subscribed: bool) -> None:
        # All folders are always subscribed
        pass

    async def list_subscribed(self) -> ListTree:
        return await self.list_mailboxes()

    async def list_mailboxes(self) -> ListTree:
        folders = await self._sync_folders()
        return ListTree(self.delimiter).update(
            "INBOX", *(n for n in folders if n != "INBOX")
        )

    async def get_mailbox(self, name: str) -> MailboxData:
        folders = await self._sync_folders()
        if name.upper() == "INBOX":
            name = "INBOX"
        if name not in folders:
            raise KeyError(name)
        folder_id, uid_validity = folders[name]
        return MailboxData(folder_id, name, uid_validity, self._conn_factory)

    async def add_mailbox(self, name: str) -> ObjectId:
        raise NotAllowedError("Cannot create mailboxes in a read-only archive")

    async def delete_mailbox(self, name: str) -> None:
        raise NotAllowedError("Cannot delete mailboxes in a read-only archive")

    async def rename_mailbox(self, before: str, after: str) -> None:
        raise NotAllowedError("Cannot rename mailboxes in a read-only archive")
