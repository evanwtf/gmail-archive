"""Web UI authentication.

The archive is published on 0.0.0.0 and holds 277k messages, so the property
that matters most is deny-by-default: a route added tomorrow must be protected
without anyone remembering to protect it.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gmail_archive.web.app import _client_id, app
from gmail_archive.web.auth import (
    SESSION_COOKIE,
    LoginThrottle,
    hash_password,
    issue_session,
    verify_password,
    verify_session,
)

PASSWORD = "correct-horse-battery"


@pytest.fixture
def secured(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client against an instance with a password configured."""
    monkeypatch.setenv("GMAIL_ARCHIVE_WEB_PASSWORD_HASH", hash_password(PASSWORD))
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def open_instance(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("GMAIL_ARCHIVE_WEB_PASSWORD_HASH", raising=False)
    return TestClient(app, follow_redirects=False)


class TestPasswordHashing:
    def test_round_trip(self) -> None:
        stored = hash_password(PASSWORD)
        assert verify_password(PASSWORD, stored)
        assert not verify_password("wrong", stored)

    def test_the_password_is_not_recoverable_from_the_hash(self) -> None:
        assert PASSWORD not in hash_password(PASSWORD)

    def test_hashes_are_salted(self) -> None:
        # Two hashes of the same password must differ, or a leaked file tells
        # an attacker which accounts share a password.
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_a_malformed_hash_refuses_every_password(self) -> None:
        # A configuration error must fail closed, not open.
        for bad in ("", "garbage", "scrypt:notanumber:8:1:x:y", "bcrypt:1:2:3:4:5"):
            assert not verify_password(PASSWORD, bad)

    def test_the_hash_contains_no_dollar_signs(self) -> None:
        # Docker Compose interpolates `$` inside .env values, so a
        # `$`-delimited hash reaches the container mangled and every login
        # fails with no indication why. Found the hard way.
        assert "$" not in hash_password(PASSWORD)


class TestSessionCookies:
    def test_a_freshly_issued_session_verifies(self) -> None:
        stored = hash_password(PASSWORD)
        assert verify_session(issue_session(stored), stored)

    def test_a_tampered_cookie_is_rejected(self) -> None:
        stored = hash_password(PASSWORD)
        cookie = issue_session(stored)
        flipped = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
        assert not verify_session(flipped, stored)

    def test_extending_the_expiry_is_rejected(self) -> None:
        # The expiry is inside the signature, so it cannot be edited.
        stored = hash_password(PASSWORD)
        version, expires, signature = issue_session(stored).split(".")
        forged = f"{version}.{int(expires) + 10_000}.{signature}"
        assert not verify_session(forged, stored)

    def test_an_expired_session_is_rejected(self) -> None:
        stored = hash_password(PASSWORD)
        cookie = issue_session(stored, now=time.time() - 60 * 60 * 24 * 365)
        assert not verify_session(cookie, stored)

    def test_changing_the_password_invalidates_sessions(self) -> None:
        # The signing key is derived from the hash, so this is free — and it
        # is what anyone changing a password expects to happen.
        old = hash_password(PASSWORD)
        cookie = issue_session(old)
        assert not verify_session(cookie, hash_password("a-new-password"))

    def test_junk_is_rejected_without_raising(self) -> None:
        stored = hash_password(PASSWORD)
        for junk in (None, "", "a.b.c", "v1.notanint.deadbeef", "....", "v9.1.2"):
            assert not verify_session(junk, stored)


class TestDenyByDefault:
    """The property that survives new routes being added."""

    def test_the_inbox_redirects_to_login(self, secured: TestClient) -> None:
        response = secured.get("/", headers={"Accept": "text/html"})
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/messages",
            "/search?q=secret",
            "/labels",
            "/stats",
            "/people",
            "/trends",
            "/version",
            "/thread/abc",
            "/messages/" + "0" * 64,
            "/messages/" + "0" * 64 + "/raw",
            "/messages/" + "0" * 64 + "/attachments/0",
            "/raw/" + "0" * 64,
        ],
    )
    def test_every_data_route_is_protected(
        self, secured: TestClient, path: str
    ) -> None:
        # Enumerated deliberately: /people, /trends and the attachment route
        # were all added in one week, and a blocklist would have leaked each.
        response = secured.get(path, headers={"Accept": "text/html"})
        assert response.status_code in (303, 401), path

    def test_healthz_stays_open(self, secured: TestClient) -> None:
        # The Docker healthcheck calls it, it touches no data, and requiring
        # auth would mean baking a credential into the image.
        assert secured.get("/healthz").status_code == 200

    def test_static_assets_stay_open(self, secured: TestClient) -> None:
        # The login page needs its stylesheet before anyone is logged in.
        assert secured.get("/static/style.css").status_code == 200

    def test_a_non_browser_gets_401_not_a_redirect(self, secured: TestClient) -> None:
        response = secured.get("/stats", headers={"Accept": "application/json"})
        assert response.status_code == 401

    def test_an_unconfigured_instance_stays_open(
        self, open_instance: TestClient
    ) -> None:
        # Refusing to serve would break a running deployment on upgrade; the
        # app warns in the log and in the header instead.
        assert open_instance.get("/healthz").status_code == 200
        assert (
            open_instance.get("/", headers={"Accept": "text/html"}).status_code != 303
        )


class TestLoginFlow:
    def test_the_correct_password_sets_a_session(self, secured: TestClient) -> None:
        response = secured.post("/login", data={"password": PASSWORD, "next": "/"})
        assert response.status_code == 303
        assert SESSION_COOKIE in response.cookies

    def test_a_session_then_reaches_protected_pages(self, secured: TestClient) -> None:
        secured.post("/login", data={"password": PASSWORD, "next": "/"})
        # The claim is "past the auth gate", so the assertion is "not bounced
        # to login". It used to assert 503 — the database being unreachable —
        # which made it fail whenever the database happened to be up, i.e.
        # exactly in CI's integration step.
        response = secured.get("/", headers={"Accept": "text/html"})
        assert response.status_code != 303

    def test_the_wrong_password_is_rejected(self, secured: TestClient) -> None:
        response = secured.post("/login", data={"password": "nope", "next": "/"})
        assert response.status_code == 401
        assert SESSION_COOKIE not in response.cookies

    def test_logout_clears_the_session(self, secured: TestClient) -> None:
        secured.post("/login", data={"password": PASSWORD, "next": "/"})
        secured.post("/logout")
        assert secured.get("/", headers={"Accept": "text/html"}).status_code == 303

    def test_logout_refuses_a_get(self, secured: TestClient) -> None:
        """A state-changing GET is the precondition for CSRF (#48).

        `<img src=".../logout">` on any page you happened to visit used to
        sign you out. It is only a nuisance — nothing can be read and the fix
        is to log in again — but this was the app's one state-changing GET,
        and removing it makes "every GET here is safe" a property rather than
        a coincidence.
        """
        secured.post("/login", data={"password": PASSWORD, "next": "/"})
        assert secured.get("/logout").status_code == 405
        # And the session survives the attempt. Asserted as "not bounced to
        # login" rather than a specific code, because whether the page then
        # renders or reports the database down is not what this is about.
        assert secured.get("/", headers={"Accept": "text/html"}).status_code != 303

    @pytest.mark.parametrize(
        "target",
        [
            "//evil.example",
            "https://evil.example",
            "http://evil.example/x",
            # A browser normalises `\` to `/` in the authority position, so
            # this is `//evil.example`. The first version of the guard
            # accepted it.
            "/\\evil.example",
            # And dot segments are removed, so this collapses to
            # `//evil.example` — same hole, different spelling.
            "/..//evil.example",
            "///evil.example",
            "evil.example",
        ],
    )
    def test_open_redirect_is_refused(self, secured: TestClient, target: str) -> None:
        # `//evil.example` is an absolute URL to a browser, so checking for a
        # leading slash alone is not enough.
        response = secured.post("/login", data={"password": PASSWORD, "next": target})
        location = response.headers["location"]
        # The property is same-origin, not literally "/": a normalised target
        # may legitimately resolve to a local path.
        assert location.startswith("/"), location
        assert not location.startswith("//"), location
        assert "evil.example" not in location.split("?")[0].lstrip("/") or (
            location.startswith("/evil.example")
        ), location

    def test_a_relative_next_is_preserved(self, secured: TestClient) -> None:
        response = secured.post(
            "/login", data={"password": PASSWORD, "next": "/search?q=x"}
        )
        assert response.headers["location"] == "/search?q=x"


class TestThrottle:
    def test_allows_attempts_below_the_threshold(self) -> None:
        throttle = LoginThrottle(threshold=3, lockout_seconds=30)
        for _ in range(2):
            throttle.record_failure("1.2.3.4")
        assert throttle.locked_for("1.2.3.4") == 0.0

    def test_locks_out_after_the_threshold(self) -> None:
        throttle = LoginThrottle(threshold=3, lockout_seconds=30)
        for _ in range(3):
            throttle.record_failure("1.2.3.4")
        assert throttle.locked_for("1.2.3.4") > 0

    def test_the_lockout_expires(self) -> None:
        throttle = LoginThrottle(threshold=1, lockout_seconds=10)
        throttle.record_failure("1.2.3.4", now=1000.0)
        assert throttle.locked_for("1.2.3.4", now=1005.0) > 0
        assert throttle.locked_for("1.2.3.4", now=1011.0) == 0.0

    def test_a_success_clears_the_record(self) -> None:
        throttle = LoginThrottle(threshold=1, lockout_seconds=30)
        throttle.record_failure("1.2.3.4")
        throttle.record_success("1.2.3.4")
        assert throttle.locked_for("1.2.3.4") == 0.0

    def test_clients_are_throttled_independently(self) -> None:
        throttle = LoginThrottle(threshold=1, lockout_seconds=30)
        throttle.record_failure("1.2.3.4")
        assert throttle.locked_for("5.6.7.8") == 0.0


class TestThrottlePruning:
    """The failure map does not grow forever (#47)."""

    def test_entries_older_than_the_lockout_are_dropped(self) -> None:
        throttle = LoginThrottle(threshold=5, lockout_seconds=30.0)
        for i in range(100):
            throttle.record_failure(f"10.0.0.{i}", now=1000.0)
        assert len(throttle._failures) == 100

        # One failure well after the window: everything stale goes with it.
        throttle.record_failure("10.0.1.1", now=1000.0 + 31.0)
        assert list(throttle._failures) == ["10.0.1.1"]

    def test_pruning_does_not_release_a_client_still_locked_out(self) -> None:
        # The dangerous way to get this wrong: prune so eagerly that an
        # attacker mid-lockout is forgiven.
        throttle = LoginThrottle(threshold=2, lockout_seconds=30.0)
        throttle.record_failure("10.0.0.1", now=1000.0)
        throttle.record_failure("10.0.0.1", now=1000.0)
        assert throttle.locked_for("10.0.0.1", now=1005.0) > 0

        throttle.record_failure("10.0.0.2", now=1005.0)
        assert throttle.locked_for("10.0.0.1", now=1005.0) > 0


class TestClientIdentification:
    """Who gets throttled, behind a proxy and without one (#47)."""

    def _request(self, headers: dict[str, str], host: str = "127.0.0.1") -> Any:
        from starlette.datastructures import Headers

        class _Client:
            def __init__(self, h: str) -> None:
                self.host = h

        class _Request:
            def __init__(self) -> None:
                self.client = _Client(host)
                self.headers = Headers(headers)

        return _Request()

    def test_forwarded_header_is_ignored_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without a proxy the header is attacker-controlled. Believing it
        # would let one client mint a fresh identity per attempt and never be
        # throttled — strictly worse than the shared bucket it would fix.
        monkeypatch.delenv("GMAIL_ARCHIVE_TRUST_PROXY", raising=False)
        request = self._request({"x-forwarded-for": "1.2.3.4"})
        assert _client_id(request) == "127.0.0.1"

    def test_forwarded_header_is_used_when_trusted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GMAIL_ARCHIVE_TRUST_PROXY", "1")
        request = self._request({"x-forwarded-for": "1.2.3.4"})
        assert _client_id(request) == "1.2.3.4"

    def test_the_rightmost_hop_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A client can write anything into the left of the list; only the
        # entry the trusted proxy appended is worth anything.
        monkeypatch.setenv("GMAIL_ARCHIVE_TRUST_PROXY", "1")
        request = self._request({"x-forwarded-for": "9.9.9.9, 8.8.8.8, 1.2.3.4"})
        assert _client_id(request) == "1.2.3.4"

    def test_a_trusted_proxy_with_no_header_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GMAIL_ARCHIVE_TRUST_PROXY", "1")
        assert _client_id(self._request({})) == "127.0.0.1"

    def test_an_empty_header_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GMAIL_ARCHIVE_TRUST_PROXY", "1")
        assert _client_id(self._request({"x-forwarded-for": " , "})) == "127.0.0.1"
