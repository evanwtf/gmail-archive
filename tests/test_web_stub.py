"""The Phase 1 surface the container healthcheck and deploys depend on."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gmail_archive.web.app import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_healthz_does_not_require_a_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The container HEALTHCHECK calls this. If it touched Postgres, a database
    # restart would mark the app unhealthy and put it in a restart loop.
    monkeypatch.delenv("GMAIL_ARCHIVE_DATABASE_URL", raising=False)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_unconfigured_without_a_dsn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GMAIL_ARCHIVE_DATABASE_URL", raising=False)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "unconfigured"


def test_version_reports_build_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GMAIL_ARCHIVE_COMMIT", "abc1234")
    body = client.get("/version").json()
    assert body["commit"] == "abc1234"
    assert body["python"].startswith("3.")


def test_version_falls_back_when_build_args_are_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Build args do not cross Dockerfile stage boundaries; if that wiring
    # breaks, this must degrade rather than fail to start.
    monkeypatch.delenv("GMAIL_ARCHIVE_COMMIT", raising=False)
    assert client.get("/version").json()["commit"] == "unknown"
