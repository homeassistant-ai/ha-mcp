"""
Unit tests for wait utility functions in util_helpers.

Tests the wait_for_entity_registered, wait_for_entity_removed, and
wait_for_state_change functions (issue #381).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

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
        """Entity that becomes available after a few polls returns True."""
        mock_client.get_entity_state.side_effect = [
            Exception("not found"),
            Exception("not found"),
            {"state": "off", "entity_id": "light.test"},
        ]
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=5.0, poll_interval=0.05
        )
        assert result is True
        assert mock_client.get_entity_state.call_count == 3

    async def test_returns_false_on_timeout(self, mock_client):
        """Returns False if entity never becomes available."""
        mock_client.get_entity_state.side_effect = Exception("not found")
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


class TestWaitForEntityRemoved:
    """Test wait_for_entity_removed utility."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_entity_state = AsyncMock()
        return client

    async def test_returns_true_when_entity_immediately_gone(self, mock_client):
        """Entity gone on first poll (exception) returns True."""
        mock_client.get_entity_state.side_effect = Exception("404 not found")
        result = await wait_for_entity_removed(mock_client, "light.test", timeout=2.0)
        assert result is True

    async def test_returns_true_when_entity_returns_none(self, mock_client):
        """Entity returning None/falsy is treated as removed."""
        mock_client.get_entity_state.return_value = None
        result = await wait_for_entity_removed(mock_client, "light.test", timeout=2.0)
        assert result is True

    async def test_returns_true_when_entity_eventually_removed(self, mock_client):
        """Entity that exists then gets removed returns True."""
        mock_client.get_entity_state.side_effect = [
            {"state": "on"},
            {"state": "on"},
            Exception("404 not found"),
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

    async def test_handles_exceptions_gracefully(self, mock_client):
        """Handles get_entity_state exceptions without crashing."""
        mock_client.get_entity_state.side_effect = [
            Exception("connection error"),
            Exception("connection error"),
        ]
        result = await wait_for_state_change(
            mock_client, "light.test", expected_state="on",
            timeout=0.2, poll_interval=0.05,
        )
        assert result is None
