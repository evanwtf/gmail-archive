"""Gmail API-backed message source.

Fetches messages from the Gmail API over HTTP. All network calls are designed
to be mocked with ``respx`` in tests — no real network in the test suite.

Quota: the Gmail API grants 250 quota units per second per user.
``messages.get`` costs 5 units, ``messages.list`` costs 1, ``history.list``
costs 2.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from gmail_archive.sources.protocol import (
    HistoryRecord,
    MessageBatch,
    RawMessage,
)

logger = logging.getLogger(__name__)

_GMAIL_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"


@dataclass
class TokenStore:
    """Holds an OAuth2 access token and knows how to refresh it.

    In production the refresh logic would call the Google OAuth2 token endpoint.
    Tests inject a fixed token and never refresh.
    """

    access_token: str = ""
    refresh_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    _expires_at: float = 0.0

    def is_expired(self) -> bool:
        """Check if the token is expired.

        A ``_expires_at`` of 0.0 means no expiry was set — the token is
        considered valid. Tests inject fixed tokens with no expiry.
        """
        if self._expires_at == 0.0:
            return False
        return time.monotonic() >= self._expires_at

    def refresh(self) -> None:
        """Refresh the access token using the refresh token.

        This is a stub — the real implementation would POST to
        ``oauth2.googleapis.com/token``. Tests override the token directly.
        """
        logger.info("Token refresh called (stub)")
        # In production: POST to https://oauth2.googleapis.com/token
        # with grant_type=refresh_token, client_id, client_secret, refresh_token.
        # For now we just log — the test suite injects tokens directly.
        if not self.refresh_token:
            logger.warning("No refresh_token configured; cannot refresh")
            return


class GmailApiSource:
    """A ``MessageSource`` backed by the Gmail API.

    Args:
        token_store: An OAuth2 token store with a valid access token.
        client: An optional ``httpx.AsyncClient``. If omitted, one is created.
            Tests pass a respx-mocked client.
        max_retries: Maximum number of retries on 429/5xx responses.
    """

    def __init__(
        self,
        token_store: TokenStore,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
    ) -> None:
        self._token_store = token_store
        self._client = client or httpx.AsyncClient()
        self._max_retries = max_retries

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make an authenticated request with retry and token refresh.

        Handles:
        - Setting the ``Authorization`` header
        - Retrying on 429 (``Retry-After``) and 5xx
        - Refreshing the token on 401 and retrying once
        """
        url = f"{_GMAIL_BASE_URL}{path}"

        for attempt in range(self._max_retries + 1):
            if self._token_store.is_expired():
                self._token_store.refresh()

            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {self._token_store.access_token}"

            response = await self._client.request(
                method, url, headers=headers, **kwargs
            )

            if response.status_code == 401 and attempt == 0:
                # Token might be stale — refresh and retry once.
                self._token_store.refresh()
                continue

            if response.status_code == 429 and attempt < self._max_retries:
                retry_after = _parse_retry_after(response)
                logger.warning("Rate limited, retrying after %s seconds", retry_after)
                await _sleep(retry_after)
                continue

            if response.status_code >= 500 and attempt < self._max_retries:
                logger.warning(
                    "Server error %s, retrying (attempt %s/%s)",
                    response.status_code,
                    attempt + 1,
                    self._max_retries,
                )
                await _sleep(1.0 * (attempt + 1))
                continue

            response.raise_for_status()
            return response

        # All retries exhausted.
        response.raise_for_status()
        return response  # unreachable, but mypy needs it

    async def list_messages(
        self,
        page_token: str | None = None,
        max_results: int = 50,
    ) -> MessageBatch:
        """List messages via ``GET /messages``.

        Gmail API: ``GET /gmail/v1/users/me/messages?maxResults=N&pageToken=P``
        """
        params: dict[str, Any] = {"maxResults": max_results}
        if page_token is not None:
            params["pageToken"] = page_token

        response = await self._request("GET", "/messages", params=params)
        data = response.json()

        raw_messages = [
            RawMessage(id=entry["id"], bytes=b"") for entry in data.get("messages", [])
        ]

        next_token = data.get("nextPageToken")
        return MessageBatch(messages=raw_messages, next_page_token=next_token)

    async def get_message(self, message_id: str) -> RawMessage:
        """Fetch a single message via ``GET /messages/{id}``.

        Uses ``format=raw`` to get the base64url-encoded RFC822 bytes.
        """
        response = await self._request(
            "GET", f"/messages/{message_id}", params={"format": "raw"}
        )
        data = response.json()

        raw_bytes = _decode_base64url(data["raw"])
        return RawMessage(id=message_id, bytes=raw_bytes)

    async def list_all(
        self,
        max_results: int = 50,
    ) -> AsyncIterator[RawMessage]:
        """Iterate over all messages, handling pagination."""
        page_token: str | None = None
        while True:
            batch = await self.list_messages(
                page_token=page_token, max_results=max_results
            )
            for msg in batch.messages:
                yield msg
            if batch.next_page_token is None:
                break
            page_token = batch.next_page_token

    async def list_history(
        self,
        start_history_id: str,
        page_token: str | None = None,
        max_results: int = 100,
    ) -> tuple[list[HistoryRecord], str | None]:
        """List history records since a given history ID.

        Returns ``(records, next_page_token)``.

        Gmail API: ``GET /gmail/v1/users/me/history?startHistoryId=S&pageToken=P``
        """
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "maxResults": max_results,
        }
        if page_token is not None:
            params["pageToken"] = page_token

        response = await self._request("GET", "/history", params=params)
        data = response.json()

        records = [_parse_history_entry(entry) for entry in data.get("history", [])]
        next_token = data.get("nextPageToken")
        return records, next_token

    async def get_profile(self) -> dict[str, Any]:
        """Fetch the user's Gmail profile.

        ``GET /gmail/v1/users/me/profile`` — returns email address, history ID, etc.
        """
        response = await self._request("GET", "/profile")
        data: dict[str, Any] = response.json()
        return data

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


# ── Helpers ─────────────────────────────────────────────────────────────


def _parse_retry_after(response: httpx.Response) -> float:
    """Extract the ``Retry-After`` header value as a float."""
    raw = response.headers.get("Retry-After", "1")
    try:
        return float(raw)
    except ValueError:
        return 1.0


async def _sleep(seconds: float) -> None:
    """Async sleep — overridable in tests via ``asyncio`` event loop."""
    import asyncio

    await asyncio.sleep(seconds)


def _decode_base64url(data: str) -> bytes:
    """Decode a Gmail API ``raw`` field (base64url, no padding)."""
    # Add padding if needed.
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data)


def _parse_history_entry(entry: dict[str, Any]) -> HistoryRecord:
    """Parse a single history entry from the Gmail API response."""
    history_id = str(entry["id"])

    messages_added: list[str] = []
    messages_deleted: list[str] = []
    labels_added: list[tuple[str, list[str]]] = []
    labels_removed: list[tuple[str, list[str]]] = []

    for msg in entry.get("messagesAdded", []):
        msg_id = msg["message"]["id"]
        messages_added.append(msg_id)

    for msg in entry.get("messagesDeleted", []):
        msg_id = msg["message"]["id"]
        messages_deleted.append(msg_id)

    for msg in entry.get("labelsAdded", []):
        msg_id = msg["message"]["id"]
        label_ids = msg.get("labelIds", [])
        labels_added.append((msg_id, label_ids))

    for msg in entry.get("labelsRemoved", []):
        msg_id = msg["message"]["id"]
        label_ids = msg.get("labelIds", [])
        labels_removed.append((msg_id, label_ids))

    return HistoryRecord(
        history_id=history_id,
        messages_added=messages_added,
        messages_deleted=messages_deleted,
        labels_added=labels_added,
        labels_removed=labels_removed,
    )
