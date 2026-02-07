"""
Unit tests for wait utility functions in util_helpers.

Tests the wait_for_entity_registered, wait_for_entity_removed, and
wait_for_state_change functions (issue #381).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantConnectionError
from ha_mcp.tools.util_helpers import (
    wait_for_entity_registered,
    wait_for_entity_removed,
    wait_for_state_change,
)


class TestWaitForEntityRegistered:
    """Test wait_for_entity_registered utility."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_entity_state = AsyncMock()
        return client

    async def test_returns_true_when_entity_immediately_available(self, mock_client):
        """Entity available on first poll returns True immediately."""
        mock_client.get_entity_state.return_value = {"state": "on", "entity_id": "light.test"}
        result = await wait_for_entity_registered(mock_client, "light.test", timeout=2.0)
        assert result is True
        mock_client.get_entity_state.assert_called_with("light.test")

    async def test_returns_true_when_entity_becomes_available(self, mock_client):
        """Entity that becomes available after a few 404s returns True."""
        mock_client.get_entity_state.side_effect = [
            HomeAssistantAPIError("not found", status_code=404),
            HomeAssistantAPIError("not found", status_code=404),
            {"state": "off", "entity_id": "light.test"},
        ]
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=5.0, poll_interval=0.05
        )
        assert result is True
        assert mock_client.get_entity_state.call_count == 3

    async def test_returns_false_on_timeout(self, mock_client):
        """Returns False if entity never becomes available."""
        mock_client.get_entity_state.side_effect = HomeAssistantAPIError("not found", status_code=404)
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=0.2, poll_interval=0.05
        )
        assert result is False

    async def test_returns_false_when_state_is_falsy(self, mock_client):
        """Returns False if get_entity_state returns falsy."""
        mock_client.get_entity_state.return_value = None
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=0.2, poll_interval=0.05
        )
        assert result is False

    async def test_raises_on_connection_error(self, mock_client):
        """Connection errors propagate instead of being silently swallowed."""
        mock_client.get_entity_state.side_effect = HomeAssistantConnectionError("network down")
        with pytest.raises(HomeAssistantConnectionError):
            await wait_for_entity_registered(
                mock_client, "light.test", timeout=2.0, poll_interval=0.05
            )


class TestWaitForEntityRemoved:
    """Test wait_for_entity_removed utility."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_entity_state = AsyncMock()
        return client

    async def test_returns_true_when_entity_immediately_gone(self, mock_client):
        """Entity gone on first poll (404) returns True."""
        mock_client.get_entity_state.side_effect = HomeAssistantAPIError("not found", status_code=404)
        result = await wait_for_entity_removed(mock_client, "light.test", timeout=2.0)
        assert result is True

    async def test_returns_true_when_entity_returns_none(self, mock_client):
        """Entity returning None/falsy is treated as removed."""
        mock_client.get_entity_state.return_value = None
        result = await wait_for_entity_removed(mock_client, "light.test", timeout=2.0)
        assert result is True

    async def test_returns_true_when_entity_eventually_removed(self, mock_client):
        """Entity that exists then gets removed (404) returns True."""
        mock_client.get_entity_state.side_effect = [
            {"state": "on"},
            {"state": "on"},
            HomeAssistantAPIError("not found", status_code=404),
        ]
        result = await wait_for_entity_removed(
            mock_client, "light.test", timeout=5.0, poll_interval=0.05
        )
        assert result is True

    async def test_returns_false_on_timeout(self, mock_client):
        """Returns False if entity never gets removed."""
        mock_client.get_entity_state.return_value = {"state": "on"}
        result = await wait_for_entity_removed(
            mock_client, "light.test", timeout=0.2, poll_interval=0.05
        )
        assert result is False

    async def test_raises_on_connection_error(self, mock_client):
        """Connection errors propagate instead of falsely reporting deletion."""
        mock_client.get_entity_state.side_effect = HomeAssistantConnectionError("network down")
        with pytest.raises(HomeAssistantConnectionError):
            await wait_for_entity_removed(
                mock_client, "light.test", timeout=2.0, poll_interval=0.05
            )


class TestWaitForStateChange:
    """Test wait_for_state_change utility."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_entity_state = AsyncMock()
        return client

    async def test_detects_expected_state_change(self, mock_client):
        """Detects when entity reaches the expected state."""
        mock_client.get_entity_state.side_effect = [
            # Initial state fetch
            {"state": "off", "entity_id": "light.test"},
            # Polls during wait
            {"state": "off", "entity_id": "light.test"},
            {"state": "on", "entity_id": "light.test"},
        ]
        result = await wait_for_state_change(
            mock_client, "light.test", expected_state="on",
            timeout=5.0, poll_interval=0.05,
        )
        assert result is not None
        assert result["state"] == "on"

    async def test_detects_any_state_change(self, mock_client):
        """Detects any state change when no expected_state is given."""
        mock_client.get_entity_state.side_effect = [
            # Initial state fetch
            {"state": "off", "entity_id": "light.test"},
            # Polls during wait
            {"state": "off", "entity_id": "light.test"},
            {"state": "on", "entity_id": "light.test"},
        ]
        result = await wait_for_state_change(
            mock_client, "light.test",
            timeout=5.0, poll_interval=0.05,
        )
        assert result is not None
        assert result["state"] == "on"

    async def test_returns_none_on_timeout(self, mock_client):
        """Returns None if state doesn't change within timeout."""
        mock_client.get_entity_state.return_value = {"state": "off", "entity_id": "light.test"}
        result = await wait_for_state_change(
            mock_client, "light.test", expected_state="on",
            timeout=0.2, poll_interval=0.05,
        )
        assert result is None

    async def test_uses_provided_initial_state(self, mock_client):
        """Uses provided initial_state instead of fetching."""
        mock_client.get_entity_state.return_value = {"state": "on", "entity_id": "light.test"}
        result = await wait_for_state_change(
            mock_client, "light.test", initial_state="off",
            timeout=2.0, poll_interval=0.05,
        )
        # Should detect change since initial_state=off but current=on
        assert result is not None
        assert result["state"] == "on"

    async def test_expected_state_immediately_met(self, mock_client):
        """Returns immediately if entity already at expected state."""
        mock_client.get_entity_state.return_value = {"state": "on", "entity_id": "light.test"}
        result = await wait_for_state_change(
            mock_client, "light.test", expected_state="on",
            timeout=2.0, poll_interval=0.05,
        )
        assert result is not None
        assert result["state"] == "on"

    async def test_initial_fetch_fails_then_detects_change(self, mock_client):
        """When initial fetch fails (API error), uses first successful poll as baseline and detects subsequent change."""
        mock_client.get_entity_state.side_effect = [
            # Initial state fetch fails (in the pre-loop section)
            HomeAssistantAPIError("not found", status_code=404),
            # First poll succeeds - becomes baseline (off)
            {"state": "off", "entity_id": "light.test"},
            # Second poll - state changed
            {"state": "on", "entity_id": "light.test"},
        ]
        result = await wait_for_state_change(
            mock_client, "light.test",
            expected_state=None,  # No specific expected state
            timeout=5.0, poll_interval=0.05,
        )
        assert result is not None
        assert result["state"] == "on"

    async def test_handles_api_errors_gracefully(self, mock_client):
        """API errors in polling loop are tolerated (entity may not exist yet)."""
        mock_client.get_entity_state.side_effect = [
            # Initial fetch OK
            {"state": "off", "entity_id": "light.test"},
            # Transient API error during polling
            HomeAssistantAPIError("server error", status_code=500),
            # Then state changes
            {"state": "on", "entity_id": "light.test"},
        ]
        result = await wait_for_state_change(
            mock_client, "light.test", expected_state="on",
            timeout=5.0, poll_interval=0.05,
        )
        assert result is not None
        assert result["state"] == "on"

    async def test_raises_on_connection_error_in_initial_fetch(self, mock_client):
        """Connection errors during initial state fetch propagate."""
        mock_client.get_entity_state.side_effect = HomeAssistantConnectionError("network down")
        with pytest.raises(HomeAssistantConnectionError):
            await wait_for_state_change(
                mock_client, "light.test", expected_state="on",
                timeout=2.0, poll_interval=0.05,
            )

    async def test_raises_on_connection_error_in_polling(self, mock_client):
        """Connection errors during polling propagate."""
        mock_client.get_entity_state.side_effect = [
            # Initial fetch OK
            {"state": "off", "entity_id": "light.test"},
            # Connection error during polling
            HomeAssistantConnectionError("network down"),
        ]
        with pytest.raises(HomeAssistantConnectionError):
            await wait_for_state_change(
                mock_client, "light.test", expected_state="on",
                timeout=5.0, poll_interval=0.05,
            )
