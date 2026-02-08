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

        # Verify it mentions the Supervisor proxy limitation
        assert "supervisor" in error_message.lower()
        assert "delete" in error_message.lower()

        # Verify it provides the LLAT fix
        assert "long-lived access token" in error_message.lower()
        assert "configuration" in error_message.lower()

        # Verify it provides the disable+rename fallback
        assert "delete_" in error_message.lower()  # Prefix suggestion
        assert "ha ui" in error_message.lower()

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
