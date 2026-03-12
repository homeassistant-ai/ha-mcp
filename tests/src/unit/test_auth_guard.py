"""Unit tests for AuthenticationError, AuthenticationGuard, and WebSocketManager auth integration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.websocket_client import (
    _AUTH_BASE_BACKOFF_SECONDS,
    _AUTH_MAX_BACKOFF_SECONDS,
    _AUTH_MAX_CONSECUTIVE_FAILURES,
    AuthenticationError,
    AuthenticationGuard,
    HomeAssistantWebSocketClient,
    WebSocketManager,
)


class TestAuthenticationGuard:
    """Tests for the AuthenticationGuard circuit-breaker."""

    def test_initial_state_allows_attempts(self):
        guard = AuthenticationGuard()
        key = "test-key"
        assert not guard.is_circuit_open(key)
        assert guard.consecutive_failures(key) == 0
        assert guard.get_backoff_seconds(key) == 0.0

    def test_record_failure_increments_count(self):
        guard = AuthenticationGuard()
        key = "test-key"
        guard.record_failure(key)
        assert guard.consecutive_failures(key) == 1
        guard.record_failure(key)
        assert guard.consecutive_failures(key) == 2

    def test_circuit_opens_after_max_failures(self):
        guard = AuthenticationGuard(max_failures=3)
        key = "test-key"
        for _ in range(3):
            guard.record_failure(key)
        assert guard.is_circuit_open(key)

    def test_circuit_stays_closed_below_max(self):
        guard = AuthenticationGuard(max_failures=5)
        key = "test-key"
        for _ in range(4):
            guard.record_failure(key)
        assert not guard.is_circuit_open(key)

    def test_record_success_resets_failures(self):
        guard = AuthenticationGuard(max_failures=5)
        key = "test-key"
        for _ in range(4):
            guard.record_failure(key)
        guard.record_success(key)
        assert guard.consecutive_failures(key) == 0
        assert not guard.is_circuit_open(key)

    def test_exponential_backoff(self):
        guard = AuthenticationGuard(base_backoff=10.0, max_backoff=100.0)
        key = "test-key"
        # 1st failure → 10 * 2^0 = 10
        guard.record_failure(key)
        assert guard.get_backoff_seconds(key) == 10.0
        # 2nd failure → 10 * 2^1 = 20
        guard.record_failure(key)
        assert guard.get_backoff_seconds(key) == 20.0
        # 3rd failure → 10 * 2^2 = 40
        guard.record_failure(key)
        assert guard.get_backoff_seconds(key) == 40.0
        # 4th failure → 10 * 2^3 = 80
        guard.record_failure(key)
        assert guard.get_backoff_seconds(key) == 80.0
        # 5th failure → 10 * 2^4 = 160 → capped at 100
        guard.record_failure(key)
        assert guard.get_backoff_seconds(key) == 100.0

    def test_backoff_caps_at_max(self):
        guard = AuthenticationGuard(base_backoff=30.0, max_backoff=300.0)
        key = "test-key"
        for _ in range(20):
            guard.record_failure(key)
        assert guard.get_backoff_seconds(key) == 300.0

    def test_reset_specific_key(self):
        guard = AuthenticationGuard()
        guard.record_failure("key-a")
        guard.record_failure("key-b")
        guard.reset("key-a")
        assert guard.consecutive_failures("key-a") == 0
        assert guard.consecutive_failures("key-b") == 1

    def test_reset_all(self):
        guard = AuthenticationGuard()
        guard.record_failure("key-a")
        guard.record_failure("key-b")
        guard.reset()
        assert guard.consecutive_failures("key-a") == 0
        assert guard.consecutive_failures("key-b") == 0

    def test_has_any_tripped_breaker(self):
        guard = AuthenticationGuard(max_failures=2)
        key = "test-key"
        assert not guard.has_any_tripped_breaker
        guard.record_failure(key)
        assert not guard.has_any_tripped_breaker
        guard.record_failure(key)
        assert guard.has_any_tripped_breaker

    def test_independent_tracking_per_key(self):
        guard = AuthenticationGuard(max_failures=3)
        guard.record_failure("key-a")
        guard.record_failure("key-a")
        guard.record_failure("key-b")
        assert guard.consecutive_failures("key-a") == 2
        assert guard.consecutive_failures("key-b") == 1
        assert not guard.is_circuit_open("key-a")
        assert not guard.is_circuit_open("key-b")

    def test_default_constants(self):
        """Verify the module-level defaults are sensible."""
        assert _AUTH_MAX_CONSECUTIVE_FAILURES == 5
        assert _AUTH_BASE_BACKOFF_SECONDS == 30.0
        assert _AUTH_MAX_BACKOFF_SECONDS == 300.0


class TestAuthenticationErrorInConnect:
    """Tests that connect() raises AuthenticationError on auth_invalid."""

    @pytest.mark.asyncio
    async def test_connect_raises_auth_error_on_invalid_token(self):
        """connect() should raise AuthenticationError when HA returns auth_invalid."""
        client = HomeAssistantWebSocketClient(
            url="http://ha.local:8123", token="bad-token"
        )

        mock_ws = AsyncMock()
        # Simulate HA WebSocket auth flow
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))

        with patch("ha_mcp.client.websocket_client.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws

            # Simulate: auth_required → send auth → auth_invalid
            async def fake_wait(message_type: str, timeout: float = 5):
                if message_type == "auth_required":
                    return {"type": "auth_required"}
                if message_type == "auth_ok":
                    return None  # not received
                if message_type == "auth_invalid":
                    return {"type": "auth_invalid", "message": "Invalid access token"}
                return None

            client._wait_for_auth_message = AsyncMock(side_effect=fake_wait)
            client._send_auth = AsyncMock()

            with pytest.raises(AuthenticationError, match="Invalid token"):
                await client.connect()


class TestWebSocketManagerAuthGuard:
    """Tests for WebSocketManager integration with AuthenticationGuard."""

    def setup_method(self):
        """Reset the singleton for each test."""
        WebSocketManager._instance = None

    @pytest.mark.asyncio
    async def test_manager_blocks_after_circuit_opens(self):
        """get_client() should raise AuthenticationError when circuit is open."""
        manager = WebSocketManager()

        # Manually trip the circuit breaker
        key = manager._client_key("http://ha.local:8123", "bad-token")
        for _ in range(_AUTH_MAX_CONSECUTIVE_FAILURES):
            manager.auth_guard.record_failure(key)

        with patch.object(manager, "_ensure_lock"):
            manager._lock = asyncio.Lock()
            manager._lock_loop = asyncio.get_event_loop()
            manager._current_loop = asyncio.get_event_loop()

            with patch(
                "ha_mcp.client.websocket_client.get_global_settings"
            ) as mock_settings:
                mock_settings.return_value = MagicMock(
                    homeassistant_url="http://ha.local:8123",
                    homeassistant_token="bad-token",
                )

                with pytest.raises(AuthenticationError, match="circuit breaker"):
                    await manager.get_client()

    @pytest.mark.asyncio
    async def test_manager_records_auth_failure(self):
        """get_client() should record failure when connect raises AuthenticationError."""
        manager = WebSocketManager()

        mock_client = MagicMock()
        mock_client.is_connected = False
        mock_client.connect = AsyncMock(side_effect=AuthenticationError("bad token"))

        manager._client_factory = lambda url, token: mock_client

        with patch.object(manager, "_ensure_lock"):
            manager._lock = asyncio.Lock()
            manager._lock_loop = asyncio.get_event_loop()
            manager._current_loop = asyncio.get_event_loop()

            with patch(
                "ha_mcp.client.websocket_client.get_global_settings"
            ) as mock_settings:
                mock_settings.return_value = MagicMock(
                    homeassistant_url="http://ha.local:8123",
                    homeassistant_token="bad-token",
                )

                key = manager._client_key("http://ha.local:8123", "bad-token")
                assert manager.auth_guard.consecutive_failures(key) == 0

                with pytest.raises(AuthenticationError):
                    await manager.get_client()

                assert manager.auth_guard.consecutive_failures(key) == 1

    @pytest.mark.asyncio
    async def test_manager_resets_on_success(self):
        """get_client() should reset auth guard on successful connection."""
        manager = WebSocketManager()

        mock_client = MagicMock()
        mock_client.is_connected = False
        mock_client.connect = AsyncMock(return_value=True)

        manager._client_factory = lambda url, token: mock_client

        with patch.object(manager, "_ensure_lock"):
            manager._lock = asyncio.Lock()
            manager._lock_loop = asyncio.get_event_loop()
            manager._current_loop = asyncio.get_event_loop()

            with patch(
                "ha_mcp.client.websocket_client.get_global_settings"
            ) as mock_settings:
                mock_settings.return_value = MagicMock(
                    homeassistant_url="http://ha.local:8123",
                    homeassistant_token="good-token",
                )

                key = manager._client_key("http://ha.local:8123", "good-token")
                # Simulate prior failures
                manager.auth_guard.record_failure(key)
                manager.auth_guard.record_failure(key)
                assert manager.auth_guard.consecutive_failures(key) == 2

                await manager.get_client()

                assert manager.auth_guard.consecutive_failures(key) == 0
