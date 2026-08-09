"""Single-user authentication for the web UI.

The archive is one person's mail, so this is a password and a signed cookie —
no user table, no registration, no password reset. What it has to get right is
narrow: store the password so a leaked `.env` is not a leaked password, prove a
session without server-side state, and fail closed.

Nothing here needs a dependency. `hashlib.scrypt` is a memory-hard KDF in the
standard library, and `hmac` signs the cookie; adding passlib or itsdangerous
would buy nothing this does not already have.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time

logger = logging.getLogger(__name__)

#: scrypt parameters. n=2**15 costs roughly 100ms and 32MB per verification on
#: the hardware this runs on — slow enough to make an offline attack on a
#: leaked hash expensive, fast enough that a login does not feel broken.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16

#: Bumped if the cookie's meaning ever changes, so old cookies stop verifying
#: rather than being reinterpreted.
_COOKIE_VERSION = "v1"

SESSION_COOKIE = "gmail_archive_session"

#: Long, deliberately. This is a personal archive on a home network; being
#: logged out weekly is friction with no security benefit, because the threat
#: is a device on the LAN, not a stolen laptop.
SESSION_MAX_AGE = 60 * 60 * 24 * 30


def hash_password(password: str) -> str:
    """Hash a password for storage in `.env`.

    Returns a self-describing string — the parameters travel with the hash, so
    they can be raised later without invalidating existing hashes.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_N * _SCRYPT_R * 200,
    )
    # Colon-separated, not the `$` of the crypt/passlib convention. Docker
    # Compose interpolates `$` inside .env values, so a `$`-delimited hash
    # arrives at the container mangled — and the failure is silent: the app
    # sees a malformed hash, refuses every login, and nothing says why.
    return ":".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(derived).decode(),
        )
    )


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash. Never raises."""
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split(":")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        # A malformed hash is a configuration error, and the safe reading of
        # "I cannot check this password" is "no".
        logger.warning("stored password hash is malformed; refusing all logins")
        return False

    derived = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=n * r * 200,
    )
    return hmac.compare_digest(derived, expected)


def _signing_key(password_hash: str) -> bytes:
    """Derive the cookie signing key from the stored password hash.

    Deriving it rather than configuring a second secret means there is one
    thing to set, and changing the password invalidates every existing session
    for free — which is what anyone changing a password expects to happen.
    """
    return hmac.new(
        b"gmail-archive.session." + _COOKIE_VERSION.encode(),
        password_hash.encode(),
        hashlib.sha256,
    ).digest()


def issue_session(password_hash: str, *, now: float | None = None) -> str:
    """Mint a signed session cookie value."""
    expires = int((now or time.time()) + SESSION_MAX_AGE)
    payload = f"{_COOKIE_VERSION}.{expires}"
    signature = hmac.new(
        _signing_key(password_hash), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_session(
    cookie: str | None, password_hash: str, *, now: float | None = None
) -> bool:
    """Check a session cookie. Never raises; anything unexpected is a no."""
    if not cookie or not password_hash:
        return False
    try:
        version, expires_s, signature = cookie.split(".")
        expires = int(expires_s)
    except (ValueError, AttributeError):
        return False

    if version != _COOKIE_VERSION:
        return False

    payload = f"{version}.{expires}"
    expected = hmac.new(
        _signing_key(password_hash), payload.encode(), hashlib.sha256
    ).hexdigest()
    # Constant-time: the comparison is against a value the caller controls.
    if not hmac.compare_digest(expected, signature):
        return False

    return (now or time.time()) < expires


class LoginThrottle:
    """Per-client delay after repeated failures.

    In memory and per process, which is the right size for a single-user
    archive: it exists to make online guessing pointless, not to survive a
    restart. pymap does the same for IMAP.
    """

    def __init__(self, *, threshold: int = 5, lockout_seconds: float = 30.0) -> None:
        self._threshold = threshold
        self._lockout = lockout_seconds
        self._failures: dict[str, tuple[int, float]] = {}

    def _prune(self, now: float) -> None:
        """Drop entries that can no longer lock anyone out.

        An entry older than the lockout window has no effect on any decision,
        so keeping it is pure growth (#47). Without this the map gains one
        entry per distinct client forever and drops one only on a successful
        login — bounded and harmless on a LAN, unbounded anywhere else.

        On write rather than on a timer: the map only grows on a failure, so
        that is the only moment it can need pruning.
        """
        cutoff = now - self._lockout
        stale = [key for key, (_, last) in self._failures.items() if last < cutoff]
        for key in stale:
            del self._failures[key]

    def locked_for(self, client: str, *, now: float | None = None) -> float:
        """Seconds remaining before this client may try again; 0 if allowed."""
        count, last = self._failures.get(client, (0, 0.0))
        if count < self._threshold:
            return 0.0
        elapsed = (now or time.time()) - last
        return max(0.0, self._lockout - elapsed)

    def record_failure(self, client: str, *, now: float | None = None) -> None:
        moment = now or time.time()
        self._prune(moment)
        count, _ = self._failures.get(client, (0, 0.0))
        self._failures[client] = (count + 1, moment)

    def record_success(self, client: str) -> None:
        self._failures.pop(client, None)
