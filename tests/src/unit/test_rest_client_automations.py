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
    async def test_delete_automation_405_falls_back_to_disable_and_rename(self, mock_client):
        """405 error should automatically disable and rename the automation.

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
        mock_client.get_automation_config = AsyncMock(return_value={
            "alias": "My Automation",
            "trigger": [{"platform": "state"}],
            "action": [{"service": "light.turn_on"}],
        })
        mock_client.upsert_automation_config = AsyncMock(return_value={
            "unique_id": "test_unique_id",
            "operation": "updated",
        })
        mock_client.call_service = AsyncMock(return_value=[])

        result = await mock_client.delete_automation_config("automation.test_automation")

        assert result["operation"] == "marked_for_deletion"
        assert "warning" in result

        # Verify the automation was renamed with DELETE_ prefix
        upsert_call = mock_client.upsert_automation_config.call_args
        config_arg = upsert_call[0][0]
        assert config_arg["alias"] == "DELETE_My Automation"

        # Verify automation.turn_off was called to disable
        mock_client.call_service.assert_called_once_with(
            "automation", "turn_off",
            {"entity_id": "automation.test_automation"},
        )

        # Verify warning tells user what happened and how to fix
        warning = result["warning"]
        assert "supervisor" in warning.lower()
        assert "disabled" in warning.lower()
        assert "delete_my automation" in warning.lower()
        assert "ha ui" in warning.lower()
        assert "long-lived access token" in warning.lower()

    @pytest.mark.asyncio
    async def test_delete_automation_405_fallback_failure_raises_error(self, mock_client):
        """If the fallback also fails, raise a helpful error."""
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed",
                status_code=405,
            )
        )
        mock_client.get_automation_config = AsyncMock(
            side_effect=Exception("Failed to get config")
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("automation.test_automation")

        error = exc_info.value
        assert error.status_code == 405
        error_message = str(error).lower()
        assert "fallback" in error_message
        assert "long-lived access token" in error_message

    @pytest.mark.asyncio
    async def test_delete_automation_405_disable_failure_still_succeeds(self, mock_client):
        """If disable fails but rename succeeds, still return marked_for_deletion."""
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed",
                status_code=405,
            )
        )
        mock_client.get_automation_config = AsyncMock(return_value={
            "alias": "My Automation",
            "trigger": [{"platform": "state"}],
            "action": [{"service": "light.turn_on"}],
        })
        mock_client.upsert_automation_config = AsyncMock(return_value={
            "unique_id": "test_unique_id",
            "operation": "updated",
        })
        # Disable call fails
        mock_client.call_service = AsyncMock(side_effect=Exception("Service failed"))

        result = await mock_client.delete_automation_config("automation.test_automation")

        # Should still succeed - rename is sufficient
        assert result["operation"] == "marked_for_deletion"

    @pytest.mark.asyncio
    async def test_delete_automation_405_already_prefixed(self, mock_client):
        """If already prefixed with DELETE_, don't double-prefix."""
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed",
                status_code=405,
            )
        )
        mock_client.get_automation_config = AsyncMock(return_value={
            "alias": "DELETE_Already Marked",
            "trigger": [{"platform": "state"}],
            "action": [{"service": "light.turn_on"}],
        })
        mock_client.upsert_automation_config = AsyncMock(return_value={
            "unique_id": "test_unique_id",
            "operation": "updated",
        })
        mock_client.call_service = AsyncMock(return_value=[])

        result = await mock_client.delete_automation_config("automation.test_automation")

        # Verify alias was NOT double-prefixed
        upsert_call = mock_client.upsert_automation_config.call_args
        config_arg = upsert_call[0][0]
        assert config_arg["alias"] == "DELETE_Already Marked"

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
