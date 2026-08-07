"""IMAP authentication.

The first tests to touch `gmail_archive.imap` at all (#16). Phase 9 was
recorded as verified on the strength of a suite that never imported this
package, and the server rejected every login including the correct one (#11).
"""

from __future__ import annotations

from argparse import Namespace

import pytest
from pysasl.creds.plain import PlainCredentials

from gmail_archive.imap.backend import Config, GmailArchiveBackend, Login


def _config(password: str = "secret") -> Config:
    """A Config without going through pymap's argument parser."""
    args = Namespace(
        database_url="",
        user="archive",
        password=password,
        host=None,
        port="1143",
        debug=False,
        cert=None,
        key=None,
        tls=False,
        passlib_cfg=None,
        proxy_protocol=None,
    )
    return Config.from_args(args)


class TestAuthentication:
    async def _login(self, password: str = "secret") -> Login:
        config = _config(password)
        login = Login(config)
        await GmailArchiveBackend._add_user(config, login)
        return login

    @pytest.mark.asyncio
    async def test_the_configured_password_is_accepted(self) -> None:
        # The regression. This failed for every credential, because the
        # hashed password was stored on an Identity that was discarded
        # immediately after being written to.
        login = await self._login("secret")
        identity = await login.authenticate(PlainCredentials("archive", "secret"))
        assert identity.name == "archive"

    @pytest.mark.asyncio
    async def test_a_wrong_password_is_rejected(self) -> None:
        from pymap.exceptions import InvalidAuth

        login = await self._login("secret")
        with pytest.raises(InvalidAuth):
            await login.authenticate(PlainCredentials("archive", "wrong"))

    @pytest.mark.asyncio
    async def test_an_empty_password_is_rejected(self) -> None:
        from pymap.exceptions import InvalidAuth

        login = await self._login("secret")
        with pytest.raises(InvalidAuth):
            await login.authenticate(PlainCredentials("archive", ""))

    @pytest.mark.asyncio
    async def test_an_unknown_user_is_rejected(self) -> None:
        # Even with the right password: the archive has exactly one user.
        from pymap.exceptions import InvalidAuth

        login = await self._login("secret")
        with pytest.raises(InvalidAuth):
            await login.authenticate(PlainCredentials("mallory", "secret"))

    @pytest.mark.asyncio
    async def test_metadata_outlives_the_identity_it_was_set_on(self) -> None:
        # `user_identity` returns a new object every call, so the metadata has
        # to live on the Login for any of this to work.
        login = await self._login("secret")
        first = login.user_identity
        second = login.user_identity
        assert first is not second
        assert await second.get() is not None


class TestServerArguments:
    """The CLI must produce the argument namespace pymap actually expects.

    `gmail-archive imap` hand-built a Namespace and had never once started:
    it died in `Config.from_args` with AttributeError on `cert`, because
    pymap's IMAPService contributes that to the *top-level* parser rather
    than to the backend subparser. Reconstructing pymap's own parser means
    every field exists by construction.
    """

    def _namespace(self) -> object:
        from argparse import ArgumentParser

        from pymap.service import services

        parser = ArgumentParser(prog="gmail-archive imap")
        parser.add_argument("--debug", action="store_true")
        parser.add_argument("--pid-file")
        parser.add_argument("--logging-cfg")
        subparsers = parser.add_subparsers(dest="backend", required=True)
        subparser = GmailArchiveBackend.add_subparser("gmail-archive", subparsers)
        subparser.set_defaults(backend_type=GmailArchiveBackend)
        for service_type in services.values():
            service_type.add_arguments(parser)
        parser.set_defaults(
            skip_services=[], passlib_cfg=None, set_uid=None, set_gid=None
        )
        return parser.parse_args(
            [
                "--host",
                "127.0.0.1",
                "--port",
                "1143",
                "--no-tls",
                "gmail-archive",
                "--database-url",
                "",
                "--user",
                "archive",
                "--password",
                "secret",
            ]
        )

    def test_config_accepts_the_namespace(self) -> None:
        # The regression: this raised AttributeError before reaching a socket.
        config = Config.from_args(self._namespace())  # type: ignore[arg-type]
        assert config.imap_user == "archive"

    @pytest.mark.parametrize(
        "field",
        ["cert", "key", "tls", "set_uid", "set_gid", "passlib_cfg", "host", "port"],
    )
    def test_every_field_pymap_reads_is_present(self, field: str) -> None:
        # pymap.main.run() and Config.from_args between them read all of
        # these; a missing one is an AttributeError at startup, not a type
        # error at import, so only an actual construction catches it.
        assert hasattr(self._namespace(), field), field


class TestMessageContent:
    """FETCH reads the body from the blob store on demand.

    The class docstring always claimed lazy loading from the blob store; the
    code only ever used bytes handed to the constructor, and nothing handed it
    any. Every FETCH returned `RFC822.SIZE 0` and an empty body.
    """

    RAW = b"Subject: hello\r\nFrom: a@example.com\r\n\r\nbody text\r\n"

    def _message(self, tmp_path: object) -> object:
        from datetime import UTC, datetime
        from pathlib import Path

        from gmail_archive.imap.message import Message
        from gmail_archive.storage import BlobStore

        store = BlobStore(Path(str(tmp_path)))
        sha = store.put(self.RAW).sha256
        return Message(
            1,
            datetime.now(UTC),
            frozenset(),
            raw_sha256=sha,
            store=store,
        )

    @pytest.mark.asyncio
    async def test_content_is_read_from_the_blob_store(self, tmp_path: object) -> None:
        from pymap.parsing.specials import FetchRequirement

        msg = self._message(tmp_path)
        loaded = await msg.load_content(FetchRequirement.CONTENT)  # type: ignore[attr-defined]
        assert loaded.get_header(b"subject")  # non-empty

    @pytest.mark.asyncio
    async def test_a_missing_blob_does_not_kill_the_fetch(
        self, tmp_path: object
    ) -> None:
        # `verify` reports missing blobs as a real condition on a damaged
        # archive; an empty body beats aborting the client's whole FETCH.
        from datetime import UTC, datetime
        from pathlib import Path

        from pymap.parsing.specials import FetchRequirement

        from gmail_archive.imap.message import Message
        from gmail_archive.storage import BlobStore

        msg = Message(
            1,
            datetime.now(UTC),
            frozenset(),
            raw_sha256="0" * 64,
            store=BlobStore(Path(str(tmp_path))),
        )
        loaded = await msg.load_content(FetchRequirement.CONTENT)
        assert loaded is not None

    def test_copy_carries_the_store_and_hash(self, tmp_path: object) -> None:
        # pymap copies messages around; losing the loader would silently
        # reintroduce the empty-body bug.
        from gmail_archive.imap.message import Message

        original = self._message(tmp_path)
        duplicate = Message.copy(original)  # type: ignore[arg-type]
        assert duplicate._raw_sha256 == original._raw_sha256  # type: ignore[attr-defined]
        assert duplicate._store is original._store  # type: ignore[attr-defined]
