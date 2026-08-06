"""Tests for the Phase 8 message source abstractions.

Tests the ``MessageSource`` protocol, ``MboxSource`` adapter, and
``GmailApiSource`` with ``respx`` mocks — no real network calls.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from gmail_archive.sources.gmail_api_source import (
    GmailApiSource,
    TokenStore,
    _decode_base64url,
    _parse_history_entry,
)
from gmail_archive.sources.mbox_source import MboxSource

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


# ── Helpers ────────────────────────────────────────────────────────────


def _encode_base64url(data: bytes) -> str:
    """Encode bytes as base64url (no padding) — mirroring the Gmail API."""
    import base64

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_source(token: str = "test-token") -> GmailApiSource:
    """Create a GmailApiSource with a fixed token and a real client."""
    ts = TokenStore(access_token=token)
    client = httpx.AsyncClient()
    return GmailApiSource(token_store=ts, client=client)


# ── MboxSource tests ──────────────────────────────────────────────────


class TestMboxSource:
    """Tests for the mbox-backed message source."""

    @pytest.fixture
    def mbox_path(self) -> Path:
        return FIXTURES / "simple.mbox"

    @pytest.fixture
    def source(self, mbox_path: Path) -> MboxSource:
        return MboxSource(mbox_path)

    @pytest.mark.asyncio
    async def test_list_messages_first_page(self, source: MboxSource) -> None:
        batch = await source.list_messages(max_results=2)
        assert len(batch.messages) == 2
        assert batch.next_page_token == "2"
        assert batch.messages[0].bytes.startswith(b"From ")

    @pytest.mark.asyncio
    async def test_list_messages_pagination(self, source: MboxSource) -> None:
        all_ids: list[str] = []
        token: str | None = None
        while True:
            batch = await source.list_messages(page_token=token, max_results=1)
            all_ids.extend(m.id for m in batch.messages)
            if batch.next_page_token is None:
                break
            token = batch.next_page_token
        assert len(all_ids) == 3

    @pytest.mark.asyncio
    async def test_get_message_by_offset(self, source: MboxSource) -> None:
        msg = await source.get_message("0")
        assert msg.bytes.startswith(b"From ")
        assert msg.id == "0"

    @pytest.mark.asyncio
    async def test_get_message_nonexistent(self, source: MboxSource) -> None:
        with pytest.raises(KeyError):
            await source.get_message("99999")

    @pytest.mark.asyncio
    async def test_message_count(self, source: MboxSource) -> None:
        assert source.message_count == 3

    @pytest.mark.asyncio
    async def test_list_all(self, source: MboxSource) -> None:
        count = 0
        async for _ in source.list_all(max_results=1):
            count += 1
        assert count == 3


# ── GmailApiSource tests ──────────────────────────────────────────────


class TestGmailApiListMessages:
    """Listing messages from the Gmail API."""

    @pytest.mark.asyncio
    async def test_list_messages_first_page(self) -> None:
        source = _make_source()
        async with respx.mock:
            route = respx.get(f"{_GMAIL_BASE}/messages").respond(
                200,
                json={
                    "messages": [{"id": "msg1"}, {"id": "msg2"}],
                    "nextPageToken": "page2",
                    "resultSizeEstimate": 2,
                },
            )

            batch = await source.list_messages(max_results=2)
            assert route.called
            assert len(batch.messages) == 2
            assert batch.messages[0].id == "msg1"
            assert batch.messages[1].id == "msg2"
            assert batch.next_page_token == "page2"

        await source.aclose()

    @pytest.mark.asyncio
    async def test_list_messages_pagination(self) -> None:
        source = _make_source()
        async with respx.mock:
            route = respx.get(f"{_GMAIL_BASE}/messages")
            route.side_effect = [
                httpx.Response(
                    200,
                    json={
                        "messages": [{"id": "msg1"}],
                        "nextPageToken": "page2",
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "messages": [{"id": "msg2"}],
                    },
                ),
            ]

            page1 = await source.list_messages(max_results=1)
            assert len(page1.messages) == 1
            assert page1.next_page_token == "page2"

            page2 = await source.list_messages(page_token="page2", max_results=1)
            assert len(page2.messages) == 1
            assert page2.messages[0].id == "msg2"
            assert page2.next_page_token is None

        await source.aclose()

    @pytest.mark.asyncio
    async def test_list_messages_empty(self) -> None:
        source = _make_source()
        async with respx.mock:
            respx.get(f"{_GMAIL_BASE}/messages").respond(
                200, json={"messages": [], "resultSizeEstimate": 0}
            )

            batch = await source.list_messages()
            assert len(batch.messages) == 0
            assert batch.next_page_token is None

        await source.aclose()

    @pytest.mark.asyncio
    async def test_list_messages_passes_page_token(self) -> None:
        source = _make_source()
        async with respx.mock:
            route = respx.get(f"{_GMAIL_BASE}/messages").respond(
                200, json={"messages": [{"id": "msg1"}]}
            )

            await source.list_messages(page_token="abc123", max_results=10)
            assert route.called
            request = route.calls[0].request
            assert "pageToken=abc123" in str(request.url)
            assert "maxResults=10" in str(request.url)

        await source.aclose()


class TestGmailApiGetMessage:
    """Fetching individual messages from the Gmail API."""

    @pytest.mark.asyncio
    async def test_get_message_raw(self) -> None:
        source = _make_source()
        async with respx.mock:
            raw_bytes = b"From: test@example.com\nSubject: Hello\n\nBody text."
            encoded = _encode_base64url(raw_bytes)

            route = respx.get(f"{_GMAIL_BASE}/messages/msg1").respond(
                200,
                json={
                    "id": "msg1",
                    "raw": encoded,
                    "sizeEstimate": len(raw_bytes),
                },
            )

            msg = await source.get_message("msg1")
            assert route.called
            assert msg.id == "msg1"
            assert msg.bytes == raw_bytes

        await source.aclose()

    @pytest.mark.asyncio
    async def test_get_message_uses_format_raw(self) -> None:
        source = _make_source()
        async with respx.mock:
            route = respx.get(f"{_GMAIL_BASE}/messages/msg1").respond(
                200,
                json={
                    "id": "msg1",
                    "raw": _encode_base64url(b"test"),
                },
            )

            await source.get_message("msg1")
            assert route.called
            request = route.calls[0].request
            assert "format=raw" in str(request.url)

        await source.aclose()

    @pytest.mark.asyncio
    async def test_get_message_404(self) -> None:
        source = _make_source()
        async with respx.mock:
            respx.get(f"{_GMAIL_BASE}/messages/nonexistent").respond(404)

            with pytest.raises(httpx.HTTPStatusError):
                await source.get_message("nonexistent")

        await source.aclose()


class TestGmailApiRetryAndErrors:
    """Retry logic, rate limiting, and error handling."""

    @pytest.mark.asyncio
    async def test_429_retry_with_retry_after(self) -> None:
        """429 with Retry-After should retry after the specified delay."""
        source = _make_source()
        async with respx.mock:
            respx.get(f"{_GMAIL_BASE}/messages").respond(
                429,
                headers={"Retry-After": "0.01"},
                json={"error": {"message": "Rate limit exceeded"}},
            )
            route = respx.get(f"{_GMAIL_BASE}/messages").respond(
                200, json={"messages": [{"id": "msg1"}]}
            )

            batch = await source.list_messages()
            assert len(batch.messages) == 1
            assert route.called

        await source.aclose()

    @pytest.mark.asyncio
    async def test_429_exhausts_retries(self) -> None:
        """After max_retries 429s, the error should propagate."""
        source = _make_source()
        async with respx.mock:
            respx.get(f"{_GMAIL_BASE}/messages").respond(
                429, json={"error": {"message": "Rate limit exceeded"}}
            )

            with pytest.raises(httpx.HTTPStatusError):
                await source.list_messages()

        await source.aclose()

    @pytest.mark.asyncio
    async def test_500_retry(self) -> None:
        """5xx errors should be retried."""
        source = _make_source()
        async with respx.mock:
            respx.get(f"{_GMAIL_BASE}/messages").respond(500)
            route = respx.get(f"{_GMAIL_BASE}/messages").respond(
                200, json={"messages": [{"id": "msg1"}]}
            )

            batch = await source.list_messages()
            assert len(batch.messages) == 1
            assert route.called

        await source.aclose()

    @pytest.mark.asyncio
    async def test_500_exhausts_retries(self) -> None:
        """After max_retries 5xx, the error should propagate."""
        source = _make_source()
        async with respx.mock:
            respx.get(f"{_GMAIL_BASE}/messages").respond(500)

            with pytest.raises(httpx.HTTPStatusError):
                await source.list_messages()

        await source.aclose()


class TestGmailApiTokenRefresh:
    """Token refresh on 401 responses."""

    @pytest.mark.asyncio
    async def test_401_triggers_token_refresh(self) -> None:
        """A 401 should refresh the token and retry."""
        ts = TokenStore(access_token="stale-token")
        client = httpx.AsyncClient()
        source = GmailApiSource(token_store=ts, client=client)

        async with respx.mock:
            route = respx.get(f"{_GMAIL_BASE}/messages")
            route.side_effect = [
                httpx.Response(401),
                httpx.Response(
                    200, json={"messages": [{"id": "msg1"}]}
                ),
            ]

            def _do_refresh() -> None:
                ts.access_token = "fresh-token"

            ts.refresh = _do_refresh  # type: ignore[method-assign]

            batch = await source.list_messages()
            assert len(batch.messages) == 1
            assert ts.access_token == "fresh-token"

        await source.aclose()

    @pytest.mark.asyncio
    async def test_401_twice_still_fails(self) -> None:
        """If refresh doesn't help, the 401 should propagate."""
        ts = TokenStore(access_token="stale-token")
        client = httpx.AsyncClient()
        source = GmailApiSource(token_store=ts, client=client)

        async with respx.mock:
            respx.get(f"{_GMAIL_BASE}/messages").respond(401)

            def _do_refresh() -> None:
                ts.access_token = "still-stale"

            ts.refresh = _do_refresh  # type: ignore[method-assign]

            with pytest.raises(httpx.HTTPStatusError):
                await source.list_messages()

        await source.aclose()


class TestGmailApiHistory:
    """History list for incremental sync."""

    @pytest.mark.asyncio
    async def test_list_history(self) -> None:
        source = _make_source()
        async with respx.mock:
            respx.get(f"{_GMAIL_BASE}/history").respond(
                200,
                json={
                    "history": [
                        {
                            "id": "1001",
                            "messagesAdded": [
                                {"message": {"id": "new1"}},
                                {"message": {"id": "new2"}},
                            ],
                            "messagesDeleted": [
                                {"message": {"id": "old1"}}
                            ],
                            "labelsAdded": [
                                {
                                    "message": {"id": "lab1"},
                                    "labelIds": ["IMPORTANT", "STARRED"],
                                }
                            ],
                            "labelsRemoved": [
                                {
                                    "message": {"id": "lab2"},
                                    "labelIds": ["SPAM"],
                                }
                            ],
                        }
                    ],
                    "nextPageToken": "next1001",
                },
            )

            records, next_token = await source.list_history(
                start_history_id="1000", max_results=10
            )
            assert len(records) == 1
            record = records[0]
            assert record.history_id == "1001"
            assert record.messages_added == ["new1", "new2"]
            assert record.messages_deleted == ["old1"]
            assert record.labels_added == [("lab1", ["IMPORTANT", "STARRED"])]
            assert record.labels_removed == [("lab2", ["SPAM"])]
            assert next_token == "next1001"

        await source.aclose()

    @pytest.mark.asyncio
    async def test_list_history_empty(self) -> None:
        source = _make_source()
        async with respx.mock:
            respx.get(f"{_GMAIL_BASE}/history").respond(
                200, json={"history": []}
            )

            records, next_token = await source.list_history(
                start_history_id="9999"
            )
            assert len(records) == 0
            assert next_token is None

        await source.aclose()

    @pytest.mark.asyncio
    async def test_list_history_pagination(self) -> None:
        source = _make_source()
        async with respx.mock:
            route = respx.get(f"{_GMAIL_BASE}/history")
            route.side_effect = [
                httpx.Response(
                    200,
                    json={
                        "history": [
                            {
                                "id": "2001",
                                "messagesAdded": [{"message": {"id": "m1"}}],
                            }
                        ],
                        "nextPageToken": "p2",
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "history": [
                            {
                                "id": "2002",
                                "messagesAdded": [{"message": {"id": "m2"}}],
                            }
                        ],
                    },
                ),
            ]

            page1, token1 = await source.list_history(
                start_history_id="2000", max_results=1
            )
            assert len(page1) == 1
            assert token1 == "p2"

            page2, token2 = await source.list_history(
                start_history_id="2000", page_token="p2", max_results=1
            )
            assert len(page2) == 1
            assert page2[0].history_id == "2002"
            assert token2 is None

        await source.aclose()


class TestGmailApiProfile:
    """Profile endpoint."""

    @pytest.mark.asyncio
    async def test_get_profile(self) -> None:
        source = _make_source()
        async with respx.mock:
            respx.get(f"{_GMAIL_BASE}/profile").respond(
                200,
                json={
                    "emailAddress": "user@gmail.com",
                    "messagesTotal": 1000,
                    "threadsTotal": 500,
                    "historyId": "12345",
                },
            )

            profile = await source.get_profile()
            assert profile["emailAddress"] == "user@gmail.com"
            assert profile["historyId"] == "12345"
            assert profile["messagesTotal"] == 1000

        await source.aclose()


# ── Protocol tests ────────────────────────────────────────────────────


class TestMessageSourceProtocol:
    """Structural tests for the MessageSource protocol."""

    def test_mbox_source_satisfies_protocol(self) -> None:
        """MboxSource should be a valid MessageSource."""
        source = MboxSource(FIXTURES / "simple.mbox")
        assert hasattr(source, "list_messages")
        assert hasattr(source, "get_message")
        assert hasattr(source, "list_all")

    def test_gmail_api_source_satisfies_protocol(self) -> None:
        """GmailApiSource should be a valid MessageSource."""
        ts = TokenStore(access_token="x")
        client = httpx.AsyncClient()
        source = GmailApiSource(token_store=ts, client=client)
        assert hasattr(source, "list_messages")
        assert hasattr(source, "get_message")
        assert hasattr(source, "list_all")


# ── Helper tests ──────────────────────────────────────────────────────


class TestBase64Decode:
    """Base64url decoding for Gmail API raw fields."""

    def test_standard_message(self) -> None:
        raw = b"Subject: Test\n\nHello, world!"
        encoded = _encode_base64url(raw)
        decoded = _decode_base64url(encoded)
        assert decoded == raw

    def test_empty_bytes(self) -> None:
        assert _decode_base64url("") == b""

    def test_padding_variants(self) -> None:
        """Gmail API omits padding; our decoder should handle it."""
        raw = b"a"
        encoded = _encode_base64url(raw).rstrip("=")
        decoded = _decode_base64url(encoded)
        assert decoded == raw


class TestParseHistoryEntry:
    """Parsing individual history entries."""

    def test_full_entry(self) -> None:
        entry = {
            "id": "5001",
            "messagesAdded": [
                {"message": {"id": "m1"}},
                {"message": {"id": "m2"}},
            ],
            "messagesDeleted": [{"message": {"id": "m3"}}],
            "labelsAdded": [
                {
                    "message": {"id": "m4"},
                    "labelIds": ["STARRED"],
                }
            ],
            "labelsRemoved": [
                {
                    "message": {"id": "m5"},
                    "labelIds": ["UNREAD"],
                }
            ],
        }
        record = _parse_history_entry(entry)
        assert record.history_id == "5001"
        assert record.messages_added == ["m1", "m2"]
        assert record.messages_deleted == ["m3"]
        assert record.labels_added == [("m4", ["STARRED"])]
        assert record.labels_removed == [("m5", ["UNREAD"])]

    def test_minimal_entry(self) -> None:
        """An entry with only an id should parse cleanly."""
        entry = {"id": "6001"}
        record = _parse_history_entry(entry)
        assert record.history_id == "6001"
        assert record.messages_added == []
        assert record.messages_deleted == []
        assert record.labels_added == []
        assert record.labels_removed == []
