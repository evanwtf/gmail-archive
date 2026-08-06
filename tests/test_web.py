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
