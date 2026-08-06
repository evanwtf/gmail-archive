"""Backend, Login, Identity, Session, and Config for the gmail-archive IMAP server.

Uses pymap's plugin system to register as a ``pymap.backend`` backend.
"""

from __future__ import annotations

import logging
from argparse import ArgumentParser, Namespace
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

from psycopg_pool import AsyncConnectionPool
from pymap.backend.session import BaseSession
from pymap.config import BackendCapability, IMAPConfig
from pymap.exceptions import AuthorizationFailure, InvalidAuth, UserNotFound
from pymap.health import HealthStatus
from pymap.interfaces.backend import BackendInterface
from pymap.interfaces.login import IdentityInterface, LoginInterface
from pymap.token import AllTokens
from pymap.user import Passwords, UserMetadata
from pysasl.creds.server import ServerCredentials

from .mailbox import MailboxSet
from .message import Message

logger = logging.getLogger(__name__)


class GmailArchiveBackend(BackendInterface):
    """Read-only IMAP backend serving messages from the gmail-archive database."""

    def __init__(self, login: Login, config: Config) -> None:
        super().__init__()
        self._login = login
        self._config = config
        self._status = HealthStatus()

    @property
    def login(self) -> Login:
        return self._login

    @property
    def config(self) -> Config:
        return self._config

    @property
    def status(self) -> HealthStatus:
        return self._status

    @classmethod
    def add_subparser(cls, name: str, subparsers: Any) -> ArgumentParser:
        parser: ArgumentParser = subparsers.add_parser(
            name,
            help="read-only IMAP server backed by the gmail-archive Postgres database",
        )
        parser.add_argument(
            "--database-url",
            default="",
            metavar="DSN",
            help="Postgres connection string (default: $GMAIL_ARCHIVE_DATABASE_URL)",
        )
        parser.add_argument(
            "--user",
            default="archive",
            metavar="USER",
            help="IMAP login username (default: archive)",
        )
        parser.add_argument(
            "--password",
            default="",
            metavar="PASS",
            help="IMAP login password (default: $GMAIL_ARCHIVE_IMAP_PASSWORD)",
        )
        return parser

    @classmethod
    async def init(
        cls, args: Namespace, **overrides: Any
    ) -> tuple[GmailArchiveBackend, Config]:
        config = Config.from_args(args, **overrides)
        login = Login(config)
        await cls._add_user(config, login)
        return cls(login, config), config

    @classmethod
    async def _add_user(cls, config: Config, login: Login) -> None:
        hashed_password = await Passwords(config).hash_password(config.imap_password)
        user = UserMetadata(config, config.imap_user, password=hashed_password)
        await login.user_identity.set(user)

    async def start(self, stack: Any) -> None:
        pass


class Config(IMAPConfig):
    """Configuration for the gmail-archive IMAP backend."""

    def __init__(
        self,
        args: Namespace,
        *,
        database_url: str,
        imap_user: str,
        imap_password: str,
        admin_key: bytes | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(args, admin_key=admin_key, **extra)
        self._database_url = database_url
        self._imap_user = imap_user
        self._imap_password = imap_password
        self._pool: AsyncConnectionPool | None = None

    @property
    def backend_capability(self) -> BackendCapability:
        return BackendCapability(idle=True, object_id=True, multi_append=False)

    @property
    def database_url(self) -> str:
        return self._database_url

    @property
    def imap_user(self) -> str:
        return self._imap_user

    @property
    def imap_password(self) -> str:
        return self._imap_password

    async def get_pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            self._pool = AsyncConnectionPool(self._database_url, min_size=1, max_size=4)
        return self._pool

    @classmethod
    def parse_args(cls, args: Namespace) -> Mapping[str, Any]:
        import os

        database_url = args.database_url or os.environ.get(
            "GMAIL_ARCHIVE_DATABASE_URL", ""
        )
        imap_password = args.password or os.environ.get(
            "GMAIL_ARCHIVE_IMAP_PASSWORD", ""
        )
        return {
            **super().parse_args(args),
            "database_url": database_url,
            "imap_user": args.user,
            "imap_password": imap_password,
        }


class Session(BaseSession[Message]):
    """Session implementation for the gmail-archive IMAP backend."""

    def __init__(self, owner: str, config: Config, mailbox_set: MailboxSet) -> None:
        super().__init__(owner)
        self._config = config
        self._mailbox_set = mailbox_set

    @property
    def config(self) -> Config:
        return self._config

    @property
    def mailbox_set(self) -> MailboxSet:
        return self._mailbox_set


class Login(LoginInterface):
    """Login implementation for the gmail-archive IMAP backend.

    Supports a single configured user with a password.
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config: Final = config
        self._passwords = Passwords(config)
        self._tokens = AllTokens(config)
        self._user_metadata: UserMetadata | None = None

    @property
    def tokens(self) -> AllTokens:
        return self._tokens

    @property
    def user_identity(self) -> Identity:
        return Identity(self.config.imap_user, self, None, frozenset())

    async def authenticate(self, credentials: ServerCredentials) -> Identity:
        authcid = credentials.authcid
        roles: set[str] = set()
        identity = Identity(authcid, self, None, frozenset(roles))
        try:
            user = await identity.get()
        except UserNotFound:
            user = UserMetadata(self.config, authcid)
        if not await self._passwords.check_password(user, credentials):
            raise InvalidAuth()
        roles |= user.roles
        return identity

    async def authorize(
        self, authenticated: IdentityInterface, authzid: str
    ) -> Identity:
        authcid = authenticated.name
        roles = authenticated.roles
        if authcid != authzid and "admin" not in roles:
            raise AuthorizationFailure()
        return Identity(authzid, self, None, roles)


class Identity(IdentityInterface):
    """Identity implementation for the gmail-archive IMAP backend."""

    def __init__(
        self, name: str, login: Login, token_id: str | None, roles: frozenset[str]
    ) -> None:
        super().__init__()
        self.login: Final = login
        self.config: Final = login.config
        self._name = name
        self._roles = roles
        self._token_id = token_id
        self._user_metadata: UserMetadata | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def roles(self) -> frozenset[str]:
        return self._roles

    async def new_token(self, *, expiration: Any = None) -> str | None:
        return None

    @asynccontextmanager
    async def new_session(self) -> AsyncIterator[Session]:
        mailbox_set = MailboxSet(self.config.get_pool)
        yield Session(self._name, self.config, mailbox_set)

    async def get(self) -> UserMetadata:
        if self._user_metadata is None:
            raise UserNotFound(self._name)
        return self._user_metadata

    async def set(self, user: UserMetadata) -> int:
        self._user_metadata = user
        return UserMetadata.new_entity_tag()

    async def delete(self) -> None:
        self._user_metadata = None
