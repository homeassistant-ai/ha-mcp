"""Unit tests for REST client script-related methods.

These tests verify error handling for script configuration operations,
especially the 405 Method Not Allowed error for YAML-defined scripts,
and script ID resolution via entity registry (#463).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
)


def _make_mock_client() -> HomeAssistantClient:
    """Create a mock HomeAssistantClient with WebSocket fallback.

    The send_websocket_message mock raises an exception so that
    _resolve_script_id falls back to using the bare id. This keeps
    existing tests backward-compatible.
    """
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        client = HomeAssistantClient()
        client.base_url = "http://test.local:8123"
        client.token = "test-token"
        client.timeout = 30
        client.httpx_client = MagicMock()
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket not available in tests")
        )
        return client


class TestDeleteScriptConfig:
    """Tests for delete_script_config error handling."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock HomeAssistantClient for testing."""
        return _make_mock_client()

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
    async def test_delete_script_405_addon_proxy_limitation(self, mock_client):
        """405 error should raise HomeAssistantAPIError with helpful message.

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

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_script_config("test_script")

        error = exc_info.value
        assert error.status_code == 405

        # Verify the error message is helpful
        error_message = str(error)
        assert "cannot delete" in error_message.lower()

        # Verify it mentions the addon proxy limitation
        assert "add-on" in error_message.lower()
        assert "supervisor" in error_message.lower()

        # Verify it mentions YAML as a possible cause
        assert "yaml" in error_message.lower()

        # Verify it provides workarounds
        assert "workaround" in error_message.lower()
        assert "pip" in error_message.lower() or "docker" in error_message.lower()
        assert "delete_" in error_message.lower()  # Prefix suggestion

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
        return _make_mock_client()

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
        return _make_mock_client()

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


class TestResolveScriptId:
    """Tests for _resolve_script_id entity registry resolution (#463).

    When a script is renamed in the HA UI, the entity_id changes but the
    storage key (unique_id) stays the same. _resolve_script_id looks up
    the entity registry via WebSocket to find the actual storage key.
    """

    @pytest.fixture
    def mock_client(self):
        """Create a mock HomeAssistantClient for testing."""
        return _make_mock_client()

    @pytest.mark.asyncio
    async def test_resolve_bare_id_matching_unique_id(self, mock_client):
        """When unique_id matches bare_id, return it unchanged."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "my_script"}}
        )

        result = await mock_client._resolve_script_id("my_script")

        assert result == "my_script"
        mock_client.send_websocket_message.assert_called_once_with(
            {"type": "config/entity_registry/get", "entity_id": "script.my_script"}
        )

    @pytest.mark.asyncio
    async def test_resolve_renamed_script(self, mock_client):
        """When script was renamed, return the original storage key."""
        # User renamed script.old_name → script.new_name in the UI
        # The storage key is still "old_name"
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "old_name"}}
        )

        result = await mock_client._resolve_script_id("new_name")

        assert result == "old_name"

    @pytest.mark.asyncio
    async def test_resolve_strips_script_prefix(self, mock_client):
        """Input with 'script.' prefix should be handled correctly."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "storage_key"}}
        )

        result = await mock_client._resolve_script_id("script.some_script")

        assert result == "storage_key"
        mock_client.send_websocket_message.assert_called_once_with(
            {"type": "config/entity_registry/get", "entity_id": "script.some_script"}
        )

    @pytest.mark.asyncio
    async def test_resolve_fallback_on_registry_failure(self, mock_client):
        """WebSocket failure should fall back to bare id."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket connection failed")
        )

        result = await mock_client._resolve_script_id("my_script")

        assert result == "my_script"

    @pytest.mark.asyncio
    async def test_resolve_fallback_on_unsuccessful_response(self, mock_client):
        """Registry returning success=False should fall back to bare id."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": {"code": "not_found"}}
        )

        result = await mock_client._resolve_script_id("my_script")

        assert result == "my_script"

    @pytest.mark.asyncio
    async def test_resolve_fallback_on_missing_unique_id(self, mock_client):
        """Registry response without unique_id should fall back to bare id."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {}}
        )

        result = await mock_client._resolve_script_id("my_script")

        assert result == "my_script"

    @pytest.mark.asyncio
    async def test_get_script_config_uses_resolved_id(self, mock_client):
        """get_script_config should use the resolved storage key."""
        # Simulate a renamed script
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "original_storage_key"}}
        )
        mock_config = {"alias": "My Script", "sequence": []}
        mock_client._request = AsyncMock(return_value=mock_config)

        result = await mock_client.get_script_config("renamed_script")

        assert result["success"] is True
        assert result["script_id"] == "original_storage_key"
        mock_client._request.assert_called_once_with(
            "GET", "config/script/config/original_storage_key"
        )

    @pytest.mark.asyncio
    async def test_delete_script_config_uses_resolved_id(self, mock_client):
        """delete_script_config should use the resolved storage key."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "original_storage_key"}}
        )
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        result = await mock_client.delete_script_config("renamed_script")

        assert result["success"] is True
        assert result["script_id"] == "original_storage_key"
        mock_client._request.assert_called_once_with(
            "DELETE", "config/script/config/original_storage_key"
        )

    @pytest.mark.asyncio
    async def test_upsert_script_config_uses_resolved_id(self, mock_client):
        """upsert_script_config should use the resolved storage key."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "original_storage_key"}}
        )
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        config = {"alias": "Updated Script", "sequence": [{"delay": {"seconds": 1}}]}
        result = await mock_client.upsert_script_config(config, "renamed_script")

        assert result["success"] is True
        assert result["script_id"] == "original_storage_key"
        mock_client._request.assert_called_once_with(
            "POST", "config/script/config/original_storage_key", json=config
        )

    @pytest.mark.asyncio
    async def test_get_script_404_shows_both_ids(self, mock_client):
        """404 error should mention both the input and resolved id."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "original_key"}}
        )
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not found", status_code=404
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_script_config("renamed_script")

        error_msg = str(exc_info.value)
        assert "renamed_script" in error_msg
        assert "original_key" in error_msg
