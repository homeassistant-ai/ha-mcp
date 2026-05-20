"""Unit tests for REST client automation-related methods.

These tests verify error handling for automation configuration operations,
especially the 405 Method Not Allowed error for addon proxy limitations.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
)


class TestDeleteAutomationConfig:
    """Tests for delete_automation_config error handling."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock HomeAssistantClient for testing."""
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client.base_url = "http://test.local:8123"
            client.token = "test-token"
            client.timeout = 30
            client.httpx_client = MagicMock()
            return client

    @pytest.mark.asyncio
    async def test_delete_automation_success(self, mock_client):
        """Successful automation deletion should return success response."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")

        result = await mock_client.delete_automation_config("automation.test_automation")

        assert result["identifier"] == "automation.test_automation"
        assert result["unique_id"] == "test_unique_id"
        assert result["operation"] == "deleted"
        mock_client._request.assert_called_once_with(
            "DELETE", "/config/automation/config/test_unique_id"
        )

    @pytest.mark.asyncio
    async def test_delete_automation_not_found_404(self, mock_client):
        """404 error should raise HomeAssistantAPIError with 'not found' message."""
        mock_client._resolve_automation_id = AsyncMock(return_value="nonexistent_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not found",
                status_code=404,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("automation.nonexistent")

        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_delete_automation_405_addon_proxy_limitation(self, mock_client):
        """405 error should raise HomeAssistantAPIError with helpful message.

        This tests the fix for issue #414 where automations cannot be deleted
        via the API when running ha-mcp as a Home Assistant add-on because
        the Supervisor ingress proxy blocks DELETE HTTP method.
        """
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed",
                status_code=405,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("automation.test_automation")

        error = exc_info.value
        assert error.status_code == 405

        # Verify the error message is helpful
        error_message = str(error)
        assert "cannot delete" in error_message.lower()

        # Verify it mentions the addon proxy limitation
        assert "add-on" in error_message.lower()
        assert "supervisor" in error_message.lower()
        assert "delete" in error_message.lower()

        # Verify it provides workarounds
        assert "workaround" in error_message.lower()
        assert "pip" in error_message.lower() or "docker" in error_message.lower()
        assert "delete_" in error_message.lower()  # Prefix suggestion
        assert "home assistant ui" in error_message.lower()

    @pytest.mark.asyncio
    async def test_delete_automation_other_error_propagates(self, mock_client):
        """Other API errors should propagate unchanged."""
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 500 - Internal Server Error",
                status_code=500,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("automation.test_automation")

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_delete_automation_generic_exception_propagates(self, mock_client):
        """Non-API exceptions should propagate."""
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=RuntimeError("Unexpected error")
        )

        with pytest.raises(RuntimeError) as exc_info:
            await mock_client.delete_automation_config("automation.test_automation")

        assert "Unexpected error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_delete_automation_with_unique_id_directly(self, mock_client):
        """Should work with unique_id passed directly (not entity_id)."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._resolve_automation_id = AsyncMock(return_value="direct_unique_id")

        result = await mock_client.delete_automation_config("direct_unique_id")

        assert result["identifier"] == "direct_unique_id"
        assert result["unique_id"] == "direct_unique_id"
        assert result["operation"] == "deleted"


class TestPollForAutomationEntity:
    """Tests for the poll cadence in _poll_for_automation_entity (issue #1380)."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock HomeAssistantClient for testing."""
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client.base_url = "http://test.local:8123"
            client.token = "test-token"
            client.timeout = 30
            client.httpx_client = MagicMock()
            return client

    def test_poll_cadence_shape(self):
        """Pin the cadence tuple. Mutation test: changing the tuple in
        rest_client.py without updating this assertion fails CI."""
        assert HomeAssistantClient._POLL_CADENCE == (0.1, 1.0, 4.9)
        # 3 attempts preserves the original failure-path get_states() load.
        assert len(HomeAssistantClient._POLL_CADENCE) == 3
        # 6.0s upper bound preserves the original 1+2+3s budget on slow HA.
        assert sum(HomeAssistantClient._POLL_CADENCE) == pytest.approx(6.0)
        # First poll is sub-200ms so the happy path returns before the
        # original 1.0s first-iteration burn.
        assert HomeAssistantClient._POLL_CADENCE[0] <= 0.2

    @pytest.mark.asyncio
    async def test_poll_returns_entity_id_on_first_attempt(self, mock_client):
        """When HA publishes the entity within the first 0.1s window, the
        poll returns on iteration 1 and only sleeps once."""
        mock_client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "automation.test_target",
                    "attributes": {"id": "unique_42"},
                }
            ]
        )
        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result == "automation.test_target"
        assert sleep_calls == [0.1]
        assert mock_client.get_states.call_count == 1

    @pytest.mark.asyncio
    async def test_poll_returns_none_on_full_miss(self, mock_client):
        """When the entity never appears, the poll exhausts the cadence,
        sleeps for the full 6s budget across 3 attempts, and returns None."""
        mock_client.get_states = AsyncMock(return_value=[])
        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result is None
        assert sleep_calls == [0.1, 1.0, 4.9]
        assert mock_client.get_states.call_count == 3

    @pytest.mark.asyncio
    async def test_poll_returns_entity_id_on_later_attempt(self, mock_client):
        """When HA publishes after the first poll, later iterations succeed
        without sleeping for the full cadence."""
        # First call returns no match, second call returns the match.
        mock_client.get_states = AsyncMock(
            side_effect=[
                [],
                [
                    {
                        "entity_id": "automation.slow_target",
                        "attributes": {"id": "unique_99"},
                    }
                ],
            ]
        )
        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_99")

        assert result == "automation.slow_target"
        assert sleep_calls == [0.1, 1.0]
        assert mock_client.get_states.call_count == 2

    @pytest.mark.asyncio
    async def test_poll_ignores_non_automation_entities(self, mock_client):
        """States not starting with `automation.` are skipped so we don't
        match a script/scene that happens to carry the same id attribute."""
        mock_client.get_states = AsyncMock(
            return_value=[
                {"entity_id": "script.distractor", "attributes": {"id": "unique_42"}},
                {
                    "entity_id": "automation.actual",
                    "attributes": {"id": "unique_42"},
                },
            ]
        )

        async def fake_sleep(duration: float) -> None:
            return None

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result == "automation.actual"

    @pytest.mark.asyncio
    async def test_poll_swallows_get_states_exception(self, mock_client):
        """A transient `get_states` failure must not propagate; the caller
        gets None and the upsert path surfaces entity_not_verified=True.

        Uses ``HomeAssistantAPIError`` for the transient mock — the realistic
        failure class for a polling-side ``get_states`` blip is an API error
        from the REST client, not a bare ``RuntimeError``. Matches the
        established sibling pattern in ``test_wait_helpers.py:54`` and
        ``:88``.

        Pins ``call_count == 1`` to lock in the early-exit semantics: the
        ``try`` at ``rest_client.py:934`` wraps the entire ``for`` loop, so a
        transient on iteration 1 hits the ``except HomeAssistantError`` clause
        and exits without re-entering the cadence. A future refactor moving
        the ``try`` inside the loop (so transients retry until cadence
        exhaustion) would fail this assertion loudly."""
        mock_client.get_states = AsyncMock(
            side_effect=HomeAssistantAPIError("transient")
        )

        async def fake_sleep(duration: float) -> None:
            return None

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result is None
        assert mock_client.get_states.call_count == 1
