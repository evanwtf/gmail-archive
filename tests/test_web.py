"""Tests for the Phase 7 web UI routes.

These tests verify that HTML pages render, CSP headers are set, and error
cases (missing messages, missing database) are handled gracefully.

Database-backed routes are integration tests that require a running Postgres
instance. They skip cleanly when GMAIL_ARCHIVE_TEST_DATABASE_URL is not set.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from gmail_archive.storage import BlobStore
from gmail_archive.web.app import RAW_VIEW_MAX_BYTES, app

DSN = os.environ.get("GMAIL_ARCHIVE_TEST_DATABASE_URL")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ── CSP and security headers ──────────────────────────────────────


class TestSecurityHeaders:
    """Every HTML response must carry CSP and nosniff headers."""

    def test_csp_header_on_index(self, client: TestClient) -> None:
        # The index route needs a database, so we expect a 503, but the
        # CSP middleware should still fire.
        response = client.get("/", headers={"Accept": "text/html"})
        assert "Content-Security-Policy" in response.headers
        assert "X-Content-Type-Options" in response.headers

    def test_csp_header_on_healthz(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert "Content-Security-Policy" in response.headers
        assert "X-Content-Type-Options" in response.headers

    def test_csp_blocks_remote_scripts(self, client: TestClient) -> None:
        csp = client.get("/healthz").headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "img-src 'self' data:" in csp
        assert "frame-ancestors 'none'" in csp

    def test_csp_names_no_external_origin(self) -> None:
        # The assertion above is a substring match, and passed for two months
        # while the policy read "script-src 'self' https://unpkg.com/...".
        # This one tests the property that was actually intended.
        from gmail_archive.web.app import app as _app

        client = TestClient(_app)
        csp = client.get("/healthz").headers["Content-Security-Policy"]
        assert "http://" not in csp
        assert "https://" not in csp

    def test_no_template_loads_a_remote_resource(self) -> None:
        # An archive that must work offline cannot depend on a CDN. This is a
        # property of the templates, so check them rather than one response.
        from pathlib import Path

        import gmail_archive.web as web_pkg

        templates_dir = Path(web_pkg.__file__).parent / "templates"
        offenders = [
            path.name
            for path in templates_dir.glob("*.html")
            if "http://" in path.read_text() or "https://" in path.read_text()
        ]
        assert offenders == [], f"templates fetch remote resources: {offenders}"

    def test_nosniff_header(self, client: TestClient) -> None:
        assert client.get("/healthz").headers["X-Content-Type-Options"] == "nosniff"


# ── HTML routes (without database — expect errors) ────────────────


class TestHtmlRoutesNoDb:
    """Verify HTML routes degrade gracefully without a database."""

    def test_index_returns_503_without_db(self, client: TestClient) -> None:
        response = client.get("/", headers={"Accept": "text/html"})
        assert response.status_code == 503

    def test_messages_returns_503_without_db(self, client: TestClient) -> None:
        response = client.get("/messages", headers={"Accept": "text/html"})
        assert response.status_code == 503

    def test_message_detail_returns_503_without_db(self, client: TestClient) -> None:
        response = client.get("/messages/" + "0" * 64, headers={"Accept": "text/html"})
        assert response.status_code == 503

    def test_search_returns_503_without_db(self, client: TestClient) -> None:
        response = client.get("/search", headers={"Accept": "text/html"})
        assert response.status_code == 503

    def test_labels_returns_503_without_db(self, client: TestClient) -> None:
        response = client.get("/labels", headers={"Accept": "text/html"})
        assert response.status_code == 503

    def test_unknown_sort_falls_back_instead_of_erroring(
        self, client: TestClient
    ) -> None:
        # 503 means it got past sort validation and into the database call.
        # A 500 would mean a hand-edited query string can crash the route.
        response = client.get(
            "/search?q=hello&sort=bogus", headers={"Accept": "text/html"}
        )
        assert response.status_code == 503

    def test_raw_returns_404_without_db(self, client: TestClient) -> None:
        # Raw download only touches the blob store, not the database.
        response = client.get("/raw/0" * 64)
        assert response.status_code == 404

    def test_raw_view_404s_for_a_missing_blob(self, client: TestClient) -> None:
        response = client.get("/messages/" + "0" * 64 + "/raw")
        assert response.status_code == 404

    def test_raw_view_404s_for_a_malformed_hash(self, client: TestClient) -> None:
        # Not a 500: path_for() raises ValueError on a wrong-length string,
        # so the route has to reject it before the blob store sees it.
        for bad in ("nope", "0" * 63, "0" * 65, "G" * 64, "0" * 63 + "Z"):
            response = client.get(f"/messages/{bad}/raw")
            assert response.status_code == 404, bad


class TestRawSourceView:
    """The raw view reads the blob store only, so no database is needed."""

    @pytest.fixture
    def stored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Callable[[bytes], str]:
        monkeypatch.setenv("GMAIL_ARCHIVE_BLOB_DIR", str(tmp_path))

        def put(data: bytes) -> str:
            return BlobStore(tmp_path).put(data).sha256

        return put

    def test_renders_the_raw_bytes(
        self, client: TestClient, stored: Callable[[bytes], str]
    ) -> None:
        sha = stored(b"Subject: hello\r\n\r\nbody text here\r\n")
        response = client.get(f"/messages/{sha}/raw")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert b"Subject: hello" in response.content
        assert b"body text here" in response.content

    def test_html_in_the_message_is_escaped_not_rendered(
        self, client: TestClient, stored: Callable[[bytes], str]
    ) -> None:
        # The point of this page: show the source, never become it.
        sha = stored(
            b"Content-Type: text/html\r\n\r\n"
            b"<script>alert(1)</script><img src=x onerror=alert(2)>\r\n"
        )
        response = client.get(f"/messages/{sha}/raw")
        assert response.status_code == 200

        # Scope the check to the rendered message, not the whole page: the
        # base template has script tags of its own, and asserting over the
        # full body would pass or fail for reasons unrelated to the message.
        body = response.content
        start = body.index(b'<pre class="raw-source">')
        rendered = body[start : body.index(b"</pre>", start)]

        # What matters is that no tag survives as markup. The attribute text
        # itself may appear -- inside "&lt;img src=x onerror=alert(2)&gt;" it
        # is inert, because the angle brackets that would open a tag are gone.
        assert b"<script" not in rendered
        assert b"<img" not in rendered
        assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
        assert b"&lt;img src=x onerror=alert(2)&gt;" in rendered

    def test_invalid_utf8_does_not_error(
        self, client: TestClient, stored: Callable[[bytes], str]
    ) -> None:
        # Twenty years of real mail is not all valid UTF-8.
        sha = stored(b"Subject: latin1\r\n\r\n\xe9\xff\xfe caf\xe9\r\n")
        response = client.get(f"/messages/{sha}/raw")
        assert response.status_code == 200
        assert b"latin1" in response.content

    def test_oversized_message_is_truncated_with_a_notice(
        self, client: TestClient, stored: Callable[[bytes], str]
    ) -> None:
        sha = stored(b"Subject: big\r\n\r\n" + b"A" * (RAW_VIEW_MAX_BYTES + 5000))
        response = client.get(f"/messages/{sha}/raw")
        assert response.status_code == 200
        assert b"Download for the whole message" in response.content
        # The cap is on what is rendered, not just on what is claimed.
        assert response.content.count(b"A") < RAW_VIEW_MAX_BYTES + 5000

    def test_small_message_carries_no_truncation_notice(
        self, client: TestClient, stored: Callable[[bytes], str]
    ) -> None:
        sha = stored(b"Subject: small\r\n\r\nshort\r\n")
        response = client.get(f"/messages/{sha}/raw")
        assert b"Download for the whole message" not in response.content

    def test_view_is_inline_while_download_stays_an_attachment(
        self, client: TestClient, stored: Callable[[bytes], str]
    ) -> None:
        sha = stored(b"Subject: both\r\n\r\nbody\r\n")

        view = client.get(f"/messages/{sha}/raw")
        assert "attachment" not in view.headers.get("content-disposition", "")

        download = client.get(f"/raw/{sha}")
        assert download.headers["content-disposition"].startswith("attachment")


# ── HTML routes (with database — integration tests) ───────────────


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestHtmlRoutesWithDb:
    """Verify HTML routes render correctly with a real database."""

    def test_index_returns_html(self, client: TestClient) -> None:
        response = client.get("/", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert b"Archive Dashboard" in response.content

    def test_messages_page_returns_html(self, client: TestClient) -> None:
        response = client.get("/messages", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        # The page should render even with no messages.
        assert b"Messages" in response.content

    def test_messages_with_keyset_pagination(self, client: TestClient) -> None:
        response = client.get(
            "/messages?after_date=2020-01-01&after_sha=0" * 64,
            headers={"Accept": "text/html"},
        )
        assert response.status_code == 200

    def test_search_page_renders(self, client: TestClient) -> None:
        response = client.get("/search", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert b"Search" in response.content

    def test_search_with_query(self, client: TestClient) -> None:
        response = client.get("/search?q=hello", headers={"Accept": "text/html"})
        assert response.status_code == 200

    def test_search_offers_sort_controls(self, client: TestClient) -> None:
        response = client.get("/search?q=hello", headers={"Accept": "text/html"})
        assert b"Newest first" in response.content
        assert b"Oldest first" in response.content

    def test_search_sort_by_date_is_accepted(self, client: TestClient) -> None:
        response = client.get(
            "/search?q=hello&sort=date", headers={"Accept": "text/html"}
        )
        assert response.status_code == 200

    def test_labels_page_renders(self, client: TestClient) -> None:
        response = client.get("/labels", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert b"Labels" in response.content

    def test_message_detail_404(self, client: TestClient) -> None:
        response = client.get("/messages/" + "0" * 64, headers={"Accept": "text/html"})
        assert response.status_code == 404

    def test_thread_view_404(self, client: TestClient) -> None:
        response = client.get("/thread/nonexistent", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert b"No messages" in response.content or b"Thread" in response.content

    def test_raw_message_404(self, client: TestClient) -> None:
        response = client.get("/raw/" + "0" * 64)
        assert response.status_code == 404


# ── Raw message download ──────────────────────────────────────────


class TestRawDownload:
    """Raw message download must use Content-Disposition: attachment."""

    def test_raw_returns_attachment_headers(self, client: TestClient) -> None:
        response = client.get("/raw/" + "0" * 64)
        assert response.status_code == 404  # blob doesn't exist

    def test_raw_content_disposition_is_attachment(self, client: TestClient) -> None:
        response = client.get("/raw/" + "0" * 64)
        # Even for 404, the middleware should set nosniff.
        assert "X-Content-Type-Options" in response.headers


class TestDayPicker:
    """`?on=YYYY-MM-DD` restricts the mailbox to one calendar day."""

    def test_bad_date_is_ignored_not_an_error(self, client: TestClient) -> None:
        # 503 means it got past date parsing and into the database call; a 500
        # would mean a hand-edited query string can crash the mailbox.
        for bad in ("not-a-date", "2026-13-45", "", "2026-02-30", "0000-00-00"):
            response = client.get(f"/?on={bad}", headers={"Accept": "text/html"})
            assert response.status_code == 503, bad

    def test_valid_date_is_accepted(self, client: TestClient) -> None:
        response = client.get("/?on=2020-03-04", headers={"Accept": "text/html"})
        assert response.status_code == 503  # no database in the unit suite


class TestDayPickerScope:
    """The "Only show Inbox" checkbox decides the scope of a day jump.

    An unchecked checkbox submits nothing, so the form sends `picker=1` to
    distinguish "unchecked" from "this request did not come from the picker".
    These assert the resolution itself, without a database.
    """

    def _resolve(self, **params: object) -> str | None:
        """Run the route's scope logic and report the label it settled on."""
        from unittest.mock import patch

        seen: dict[str, object] = {}

        def capture(conn: object, **kwargs: object) -> list[object]:
            seen.update(kwargs)
            raise psycopg.OperationalError("stop here")

        with (
            patch("gmail_archive.web.app.list_messages_keyset", capture),
            patch("gmail_archive.web.app._get_conn"),
        ):
            TestClient(app).get("/", params=params)
        return seen.get("label")  # type: ignore[return-value]

    def test_checked_scopes_to_the_inbox(self) -> None:
        assert self._resolve(picker=1, inbox_only=1, on="2020-03-04") == "Inbox"

    def test_unchecked_searches_all_mail(self) -> None:
        # No inbox_only at all, which is what a browser sends when unchecked.
        assert self._resolve(picker=1, on="2020-03-04") is None

    def test_unchecked_overrides_the_mailbox_you_were_in(self) -> None:
        # "Only show Inbox", unchecked, must not leave you inside Starred.
        assert self._resolve(picker=1, label="Starred", on="2020-03-04") is None

    def test_checked_overrides_the_mailbox_you_were_in(self) -> None:
        assert self._resolve(picker=1, inbox_only=1, label="Starred") == "Inbox"

    def test_without_the_picker_marker_the_label_is_untouched(self) -> None:
        # A plain link is not a picker submission and must keep its label.
        assert self._resolve(label="Starred") == "Starred"

    def test_default_front_door_is_still_the_inbox(self) -> None:
        assert self._resolve() == "Inbox"


class TestAssetVersioning:
    """Static URLs carry a fingerprint so a browser cannot pair new HTML with
    a cached stylesheet — which renders as a layout bug, not a stale file."""

    def test_rendered_pages_link_the_versioned_stylesheet(
        self, client: TestClient
    ) -> None:
        from gmail_archive.web.app import ASSET_VERSION

        # The 503 page extends base.html, so it exercises the real <head>
        # without needing a database.
        body = client.get("/", headers={"Accept": "text/html"}).text
        assert f"/static/style.css?v={ASSET_VERSION}" in body
        assert f"/static/htmx.min.js?v={ASSET_VERSION}" in body
        assert '/static/style.css"' not in body  # never the unversioned URL

    def test_versioned_static_is_cached_hard(self, client: TestClient) -> None:
        from gmail_archive.web.app import ASSET_VERSION

        response = client.get(f"/static/style.css?v={ASSET_VERSION}")
        assert response.status_code == 200
        assert "immutable" in response.headers["cache-control"]

    def test_unversioned_static_must_revalidate(self, client: TestClient) -> None:
        # Never let a browser serve this from cache without asking, or the
        # next CSS change is invisible to anyone who loaded the old one.
        response = client.get("/static/style.css")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"

    def test_version_changes_when_the_stylesheet_does(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gmail_archive.web.app as app_module

        first = app_module._asset_version()
        monkeypatch.setattr(app_module, "HERE", tmp_path)
        (tmp_path / "static").mkdir()
        (tmp_path / "static" / "style.css").write_text("body{}")
        assert app_module._asset_version() != first


class TestAttachmentDownload:
    """Attachments are re-extracted from the raw message on demand."""

    RAW = (
        b"From: a@example.com\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="report.pdf"\r\n\r\nPDFBYTES\r\n'
        b"--B\r\nContent-Type: text/html\r\n"
        b'Content-Disposition: attachment; filename="../../etc/passwd"\r\n\r\nEVIL\r\n'
        b"--B--\r\n"
    )

    @pytest.fixture
    def sha(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        monkeypatch.setenv("GMAIL_ARCHIVE_BLOB_DIR", str(tmp_path))
        return BlobStore(tmp_path).put(self.RAW).sha256

    def test_serves_the_attachment_bytes(self, client: TestClient, sha: str) -> None:
        response = client.get(f"/messages/{sha}/attachments/0")
        assert response.status_code == 200
        assert response.content == b"PDFBYTES"

    def test_served_as_an_attachment_never_inline(
        self, client: TestClient, sha: str
    ) -> None:
        response = client.get(f"/messages/{sha}/attachments/0")
        assert response.headers["content-disposition"].startswith("attachment")
        # Declared type is ignored: twenty years of mail contains plenty of
        # Content-Type headers that are wrong, wishful, or hostile.
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_path_traversal_in_the_filename_is_neutralised(
        self, client: TestClient, sha: str
    ) -> None:
        # The archive stores the filename exactly as declared, including this.
        response = client.get(f"/messages/{sha}/attachments/1")
        assert response.status_code == 200
        name = response.headers["content-disposition"].split("filename=")[1].strip('"')

        # What makes traversal possible is a path separator, not the dots. With
        # every separator gone the name is a single, inert path segment, so
        # "_.._etc_passwd" is a fine thing to save to disk.
        assert "/" not in name
        assert "\\" not in name
        assert name not in ("..", ".")
        # And it cannot break out of the quoted header value either.
        assert '"' not in name

    def test_html_attachment_is_not_served_as_html(
        self, client: TestClient, sha: str
    ) -> None:
        # A text/html attachment served inline would run in this origin.
        response = client.get(f"/messages/{sha}/attachments/1")
        assert "text/html" not in response.headers["content-type"]

    def test_out_of_range_index_is_404(self, client: TestClient, sha: str) -> None:
        assert client.get(f"/messages/{sha}/attachments/9").status_code == 404
        assert client.get(f"/messages/{sha}/attachments/-1").status_code == 404

    def test_malformed_hash_is_404(self, client: TestClient) -> None:
        assert client.get("/messages/nope/attachments/0").status_code == 404


class TestRawDownloadValidation:
    """`/raw/{sha}` must 404 on a malformed hash, not 500 (#19)."""

    def test_malformed_hashes_are_404(self, client: TestClient) -> None:
        # path_for() raises ValueError on a wrong-length string, which the
        # route's FileNotFoundError handler does not catch.
        for bad in ("nope", "0" * 63, "0" * 65, "G" * 64, "0" * 63 + "Z", "%2e%2e"):
            assert client.get(f"/raw/{bad}").status_code == 404, bad

    def test_a_well_formed_but_absent_hash_is_still_404(
        self, client: TestClient
    ) -> None:
        assert client.get("/raw/" + "0" * 64).status_code == 404
