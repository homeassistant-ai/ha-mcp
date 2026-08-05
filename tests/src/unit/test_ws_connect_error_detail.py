"""Unit tests for surfacing *why* a WebSocket connection failed.

Regression coverage for the opaque-error problem behind issue #1464: a failed
``connect()`` used to bubble up only as "Failed to connect to Home Assistant
WebSocket" with the real reason confined to a log line. These tests pin the
reason being captured on the client and surfaced at both call sites (the
pooled ``WebSocketManager.get_client`` path and the ``get_connected_ws_client``
helper path).
"""

import json
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeWS:
    """Minimal async-iterable websocket stand-in for connect() tests."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self):
        pass

    async def send(self, *_args, **_kwargs):
        pass


class TestConnectCapturesReason:
    """``connect()`` records the failure reason on ``last_connect_error``."""

    def test_last_connect_error_is_none_before_any_connect(self):
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(url="http://supervisor/core", token="t")
        assert client.last_connect_error is None

    @pytest.mark.asyncio
    async def test_connect_failure_captures_exception_text(self):
        """A transport-level failure is captured verbatim (type + message)."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(url="http://supervisor/core", token="t")

        async def fake_connect(*_args, **_kwargs):
            raise OSError("Connection refused")

        with patch(
            "ha_mcp.client.websocket_client.websockets.connect",
            side_effect=fake_connect,
        ):
            assert await client.connect() is False

        assert client.last_connect_error is not None
        assert "OSError" in client.last_connect_error
        assert "Connection refused" in client.last_connect_error

    @pytest.mark.asyncio
    async def test_missing_auth_required_is_captured(self):
        """The 'Did not receive auth_required' handshake failure is captured.

        This is the failure mode the issue bot hypothesised for Supervisor
        proxy mode — the reason must reach the caller, not just the log.
        """
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(url="http://supervisor/core", token="t")

        async def fake_connect(*_args, **_kwargs):
            return _FakeWS()

        # No auth message ever arrives -> the auth_required wait returns None.
        client._wait_for_auth_message = AsyncMock(return_value=None)  # type: ignore[method-assign]

        with patch(
            "ha_mcp.client.websocket_client.websockets.connect",
            side_effect=fake_connect,
        ):
            assert await client.connect() is False

        assert client.last_connect_error is not None
        assert "Did not receive auth_required" in client.last_connect_error

    @pytest.mark.asyncio
    async def test_reason_is_cleared_on_a_fresh_attempt(self):
        """A new connect() attempt resets the previous reason before running."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(url="http://supervisor/core", token="t")
        client._last_connect_error = "stale: from a previous attempt"

        async def fake_connect(*_args, **_kwargs):
            raise OSError("brand new failure")

        with patch(
            "ha_mcp.client.websocket_client.websockets.connect",
            side_effect=fake_connect,
        ):
            assert await client.connect() is False

        assert "stale" not in (client.last_connect_error or "")
        assert "brand new failure" in (client.last_connect_error or "")

    @pytest.mark.asyncio
    async def test_ssl_error_is_captured(self):
        """A TLS verification failure is captured like any other reason.

        Self-signed / hostname-mismatch certs are a common real-world cause,
        so the surfaced reason must name the SSL error, not the opaque string.
        """
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="https://ha.example.com:8123", token="t"
        )

        async def fake_connect(*_args, **_kwargs):
            raise ssl.SSLCertVerificationError("self-signed certificate")

        with patch(
            "ha_mcp.client.websocket_client.websockets.connect",
            side_effect=fake_connect,
        ):
            assert await client.connect() is False

        assert client.last_connect_error is not None
        assert "SSLCertVerificationError" in client.last_connect_error
        assert "self-signed" in client.last_connect_error

    @pytest.mark.asyncio
    async def test_token_is_not_leaked_into_reason(self):
        """The captured reason must never contain the access token.

        The reason is built only from the exception text; the token travels
        in the Authorization header / auth payload and is never echoed into a
        connect exception. This pins that invariant against a future change
        that captures more (repr(e), e.args, the URL, or request headers).
        """
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        token = "super-secret-token-DO-NOT-LEAK"
        client = HomeAssistantWebSocketClient(url="http://supervisor/core", token=token)

        async def fake_connect(*_args, **_kwargs):
            raise OSError("[Errno 111] Connection refused")

        with patch(
            "ha_mcp.client.websocket_client.websockets.connect",
            side_effect=fake_connect,
        ):
            assert await client.connect() is False

        assert client.last_connect_error is not None
        assert token not in client.last_connect_error


class TestManagerSurfacesReason:
    """``WebSocketManager.get_client`` appends the reason to its raised error."""

    @pytest.mark.asyncio
    async def test_get_client_includes_reason_in_message(self):
        from ha_mcp.client.websocket_client import (
            HomeAssistantWebSocketClient,
            WebSocketManager,
        )

        mock_client = MagicMock()
        mock_client.is_connected = False
        mock_client.connect = AsyncMock(return_value=False)
        mock_client.disconnect = AsyncMock()
        mock_client.last_connect_error = "OSError: [Errno 111] Connection refused"

        manager = WebSocketManager()
        # Isolate from any pooled state left by other tests.
        manager._clients.clear()
        manager._last_used.clear()
        manager._current_loop = None
        manager.configure(client_factory=lambda url, token: mock_client)
        try:
            with pytest.raises(Exception) as exc_info:
                await manager.get_client(url="http://supervisor/core", token="t")
            msg = str(exc_info.value)
            assert "Failed to connect to Home Assistant WebSocket" in msg
            assert "Connection refused" in msg
        finally:
            manager.configure(client_factory=HomeAssistantWebSocketClient)
            manager._clients.clear()
            manager._last_used.clear()
            manager._current_loop = None

    @pytest.mark.asyncio
    async def test_get_client_omits_suffix_when_reason_absent(self):
        """A client whose connect() captured no reason yields the bare message.

        No trailing colon, no repr — the ``isinstance`` guard turns a ``None``
        reason into an empty suffix.
        """
        from ha_mcp.client.websocket_client import (
            HomeAssistantWebSocketClient,
            WebSocketManager,
        )

        mock_client = MagicMock()
        mock_client.is_connected = False
        mock_client.connect = AsyncMock(return_value=False)
        mock_client.disconnect = AsyncMock()
        mock_client.last_connect_error = None

        manager = WebSocketManager()
        manager._clients.clear()
        manager._last_used.clear()
        manager._current_loop = None
        manager.configure(client_factory=lambda url, token: mock_client)
        try:
            with pytest.raises(Exception) as exc_info:
                await manager.get_client(url="http://supervisor/core", token="t")
            assert (
                str(exc_info.value) == "Failed to connect to Home Assistant WebSocket"
            )
        finally:
            manager.configure(client_factory=HomeAssistantWebSocketClient)
            manager._clients.clear()
            manager._last_used.clear()
            manager._current_loop = None


class TestHelperSurfacesReason:
    """``get_connected_ws_client`` puts the reason in the error ``details``."""

    @pytest.mark.asyncio
    async def test_helper_uses_reason_as_details(self):
        from ha_mcp.tools import helpers

        fake = MagicMock()
        fake.connect = AsyncMock(return_value=False)
        fake.last_connect_error = (
            "HomeAssistantConnectionError: Did not receive auth_required message"
        )

        with patch.object(helpers, "HomeAssistantWebSocketClient", return_value=fake):
            ws_client, error = await helpers.get_connected_ws_client(
                "http://supervisor/core", "t"
            )

        assert ws_client is None
        assert error is not None
        # Robust to the exact key the reason lands under in the error dict.
        assert "Did not receive auth_required" in json.dumps(error)

    @pytest.mark.asyncio
    async def test_helper_falls_back_when_reason_absent(self):
        """No captured reason -> the generic default lands in details, no mock repr."""
        from ha_mcp.tools import helpers

        fake = MagicMock()  # last_connect_error left as an auto-mock
        fake.connect = AsyncMock(return_value=False)

        with patch.object(helpers, "HomeAssistantWebSocketClient", return_value=fake):
            ws_client, error = await helpers.get_connected_ws_client(
                "http://supervisor/core", "t"
            )

        assert ws_client is None
        assert error is not None
        blob = json.dumps(error)
        assert "WebSocket connection could not be established" in blob
        assert "MagicMock" not in blob
