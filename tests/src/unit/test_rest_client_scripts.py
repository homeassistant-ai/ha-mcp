"""Unit tests for REST client script-related methods.

These tests verify error handling for script configuration operations,
especially the 405 Method Not Allowed error for YAML-defined scripts.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
)


class TestDeleteScriptConfig:
    """Tests for delete_script_config error handling."""

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
    async def test_delete_script_success(self, mock_client):
        """Successful script deletion should return success response."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        result = await mock_client.delete_script_config("test_script")

        assert result["success"] is True
        assert result["script_id"] == "test_script"
        assert result["operation"] == "deleted"
        mock_client._request.assert_called_once_with(
            "DELETE", "config/script/config/test_script"
        )

    @pytest.mark.asyncio
    async def test_delete_script_not_found_404(self, mock_client):
        """404 error should raise HomeAssistantAPIError with 'not found' message."""
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not found",
                status_code=404,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_script_config("nonexistent_script")

        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_delete_script_405_falls_back_to_disable_and_rename(self, mock_client):
        """405 error should automatically disable and rename the script.

        This tests the fix for issues #261 and #414 where scripts cannot be
        deleted via the API due to:
        1. HA Supervisor addon proxy blocking DELETE method
        2. YAML-mode scripts that cannot be deleted via API
        """
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed",
                status_code=405,
            )
        )
        mock_client.get_script_config = AsyncMock(return_value={
            "success": True,
            "script_id": "test_script",
            "config": {
                "alias": "My Script",
                "sequence": [{"delay": {"seconds": 1}}],
            },
        })
        mock_client.upsert_script_config = AsyncMock(return_value={
            "success": True,
            "script_id": "test_script",
            "operation": "updated",
        })
        mock_client.call_service = AsyncMock(return_value=[])

        result = await mock_client.delete_script_config("test_script")

        assert result["operation"] == "marked_for_deletion"
        assert "warning" in result

        # Verify the script was renamed with DELETE_ prefix
        upsert_call = mock_client.upsert_script_config.call_args
        config_arg = upsert_call[0][0]
        assert config_arg["alias"] == "DELETE_My Script"

        # Verify homeassistant.turn_off was called to disable
        mock_client.call_service.assert_called_once_with(
            "homeassistant", "turn_off",
            {"entity_id": "script.test_script"},
        )

        # Verify warning mentions key information
        warning = result["warning"]
        assert "supervisor" in warning.lower()
        assert "disabled" in warning.lower()
        assert "delete_my script" in warning.lower()
        assert "ha ui" in warning.lower()
        assert "yaml" in warning.lower()
        assert "long-lived access token" in warning.lower()

    @pytest.mark.asyncio
    async def test_delete_script_405_fallback_failure_raises_error(self, mock_client):
        """If the fallback also fails, raise a helpful error."""
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed",
                status_code=405,
            )
        )
        mock_client.get_script_config = AsyncMock(
            side_effect=Exception("Failed to get config")
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_script_config("test_script")

        error = exc_info.value
        assert error.status_code == 405
        error_message = str(error).lower()
        assert "fallback" in error_message
        assert "long-lived access token" in error_message
        assert "yaml" in error_message

    @pytest.mark.asyncio
    async def test_delete_script_405_disable_failure_still_succeeds(self, mock_client):
        """If disable fails but rename succeeds, still return marked_for_deletion."""
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed",
                status_code=405,
            )
        )
        mock_client.get_script_config = AsyncMock(return_value={
            "success": True,
            "script_id": "test_script",
            "config": {
                "alias": "My Script",
                "sequence": [{"delay": {"seconds": 1}}],
            },
        })
        mock_client.upsert_script_config = AsyncMock(return_value={
            "success": True,
            "script_id": "test_script",
            "operation": "updated",
        })
        # Disable call fails
        mock_client.call_service = AsyncMock(side_effect=Exception("Service failed"))

        result = await mock_client.delete_script_config("test_script")

        # Should still succeed - rename is sufficient
        assert result["operation"] == "marked_for_deletion"

    @pytest.mark.asyncio
    async def test_delete_script_405_already_prefixed(self, mock_client):
        """If already prefixed with DELETE_, don't double-prefix."""
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed",
                status_code=405,
            )
        )
        mock_client.get_script_config = AsyncMock(return_value={
            "success": True,
            "script_id": "test_script",
            "config": {
                "alias": "DELETE_Already Marked",
                "sequence": [{"delay": {"seconds": 1}}],
            },
        })
        mock_client.upsert_script_config = AsyncMock(return_value={
            "success": True,
            "script_id": "test_script",
            "operation": "updated",
        })
        mock_client.call_service = AsyncMock(return_value=[])

        result = await mock_client.delete_script_config("test_script")

        # Verify alias was NOT double-prefixed
        upsert_call = mock_client.upsert_script_config.call_args
        config_arg = upsert_call[0][0]
        assert config_arg["alias"] == "DELETE_Already Marked"

    @pytest.mark.asyncio
    async def test_delete_script_other_error_propagates(self, mock_client):
        """Other API errors should propagate unchanged."""
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 500 - Internal Server Error",
                status_code=500,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_script_config("test_script")

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_delete_script_generic_exception_propagates(self, mock_client):
        """Non-API exceptions should propagate."""
        mock_client._request = AsyncMock(
            side_effect=RuntimeError("Unexpected error")
        )

        with pytest.raises(RuntimeError) as exc_info:
            await mock_client.delete_script_config("test_script")

        assert "Unexpected error" in str(exc_info.value)


class TestGetScriptConfig:
    """Tests for get_script_config error handling."""

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
    async def test_get_script_success(self, mock_client):
        """Successful script retrieval should return config."""
        mock_config = {
            "alias": "Test Script",
            "sequence": [{"delay": {"seconds": 1}}],
            "mode": "single",
        }
        mock_client._request = AsyncMock(return_value=mock_config)

        result = await mock_client.get_script_config("test_script")

        assert result["success"] is True
        assert result["script_id"] == "test_script"
        assert result["config"] == mock_config

    @pytest.mark.asyncio
    async def test_get_script_not_found_404(self, mock_client):
        """404 error should raise HomeAssistantAPIError."""
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not found",
                status_code=404,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_script_config("nonexistent_script")

        assert exc_info.value.status_code == 404


class TestUpsertScriptConfig:
    """Tests for upsert_script_config validation."""

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
    async def test_upsert_script_with_sequence(self, mock_client):
        """Regular script with sequence should succeed."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        config = {
            "alias": "Test Script",
            "sequence": [{"delay": {"seconds": 1}}],
        }

        result = await mock_client.upsert_script_config(config, "test_script")

        assert result["success"] is True
        assert result["script_id"] == "test_script"

    @pytest.mark.asyncio
    async def test_upsert_script_with_blueprint(self, mock_client):
        """Blueprint-based script should succeed."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        config = {
            "alias": "Blueprint Script",
            "use_blueprint": {
                "path": "test.yaml",
                "input": {},
            },
        }

        result = await mock_client.upsert_script_config(config, "test_script")

        assert result["success"] is True
        assert result["script_id"] == "test_script"

    @pytest.mark.asyncio
    async def test_upsert_script_missing_both_sequence_and_blueprint(self, mock_client):
        """Script without sequence or use_blueprint should fail."""
        config = {
            "alias": "Incomplete Script",
        }

        with pytest.raises(ValueError) as exc_info:
            await mock_client.upsert_script_config(config, "test_script")

        assert "sequence" in str(exc_info.value) and "use_blueprint" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_upsert_script_adds_alias_if_missing(self, mock_client):
        """Script without alias should get alias from script_id."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        config = {
            "sequence": [{"delay": {"seconds": 1}}],
        }

        await mock_client.upsert_script_config(config, "test_script")

        # Verify alias was added
        call_args = mock_client._request.call_args
        json_arg = call_args[1]["json"]
        assert json_arg["alias"] == "test_script"
