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
