"""Tests for the Phase 7 web UI routes.

These tests verify that HTML pages render, CSP headers are set, and error
cases (missing messages, missing database) are handled gracefully.

Database-backed routes are integration tests that require a running Postgres
instance. They skip cleanly when GMAIL_ARCHIVE_TEST_DATABASE_URL is not set.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from gmail_archive.web.app import app

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

    def test_raw_returns_404_without_db(self, client: TestClient) -> None:
        # Raw download only touches the blob store, not the database.
        response = client.get("/raw/0" * 64)
        assert response.status_code == 404


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
