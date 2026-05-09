"""Unit tests for REST client scene-related methods.

These tests verify error handling for scene configuration operations,
especially the 405 Method Not Allowed error for YAML-defined scenes,
and scene ID resolution via entity registry. Mirror the script test
shape (test_rest_client_scripts.py) so the parity is auditable.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
)


def _make_mock_client() -> HomeAssistantClient:
    """Create a mock HomeAssistantClient with WebSocket fallback.

    The send_websocket_message mock raises so resolve_scene_id falls back to
    the bare id, keeping tests independent of the registry resolver.
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


class TestDeleteSceneConfig:
    """Tests for delete_scene_config error handling."""

    @pytest.fixture
    def mock_client(self):
        return _make_mock_client()

    @pytest.mark.asyncio
    async def test_delete_scene_success(self, mock_client):
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        result = await mock_client.delete_scene_config("movie_night")

        assert result["success"] is True
        assert result["scene_id"] == "movie_night"
        assert result["operation"] == "deleted"
        mock_client._request.assert_called_once_with(
            "DELETE", "config/scene/config/movie_night"
        )

    @pytest.mark.asyncio
    async def test_delete_scene_not_found_404(self, mock_client):
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not found", status_code=404
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_scene_config("nonexistent_scene")

        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_delete_scene_405_addon_proxy_limitation(self, mock_client):
        """405 should surface a helpful workaround-bearing error message."""
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed", status_code=405
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_scene_config("test_scene")

        error = exc_info.value
        assert error.status_code == 405

        msg = str(error).lower()
        assert "cannot delete" in msg
        assert "add-on" in msg
        assert "supervisor" in msg
        assert "yaml" in msg
        assert "workaround" in msg
        assert "delete_" in msg  # The DELETE_-prefix fallback suggestion

    @pytest.mark.asyncio
    async def test_delete_scene_other_error_propagates(self, mock_client):
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 500 - Internal Server Error", status_code=500
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_scene_config("test_scene")

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_delete_scene_generic_exception_propagates(self, mock_client):
        mock_client._request = AsyncMock(side_effect=RuntimeError("Unexpected"))

        with pytest.raises(RuntimeError) as exc_info:
            await mock_client.delete_scene_config("test_scene")

        assert "Unexpected" in str(exc_info.value)


class TestGetSceneConfig:
    """Tests for get_scene_config."""

    @pytest.fixture
    def mock_client(self):
        return _make_mock_client()

    @pytest.mark.asyncio
    async def test_get_scene_success(self, mock_client):
        mock_config = {
            "name": "Movie Night",
            "entities": {
                "light.living_room": {"state": "on", "brightness": 50},
            },
            "icon": "mdi:movie",
        }
        mock_client._request = AsyncMock(return_value=mock_config)

        result = await mock_client.get_scene_config("movie_night")

        assert result["success"] is True
        assert result["scene_id"] == "movie_night"
        assert result["config"] == mock_config

    @pytest.mark.asyncio
    async def test_get_scene_not_found_404(self, mock_client):
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not found", status_code=404
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_scene_config("nonexistent_scene")

        assert exc_info.value.status_code == 404


class TestUpsertSceneConfig:
    """Tests for upsert_scene_config validation."""

    @pytest.fixture
    def mock_client(self):
        return _make_mock_client()

    @pytest.mark.asyncio
    async def test_upsert_scene_with_entities_dict(self, mock_client):
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        config = {
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on", "brightness": 200}},
        }

        result = await mock_client.upsert_scene_config(config, "test_scene")

        assert result["success"] is True
        assert result["scene_id"] == "test_scene"

    @pytest.mark.asyncio
    async def test_upsert_scene_missing_entities_field(self, mock_client):
        """A scene without 'entities' is rejected at the client layer."""
        config = {"name": "Empty Scene"}

        with pytest.raises(ValueError) as exc_info:
            await mock_client.upsert_scene_config(config, "test_scene")

        assert "entities" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_upsert_scene_adds_name_if_missing(self, mock_client):
        """Scenes without 'name' default to scene_id (mirrors script alias default)."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        config = {"entities": {"light.kitchen": {"state": "on"}}}

        await mock_client.upsert_scene_config(config, "test_scene")

        call_args = mock_client._request.call_args
        json_arg = call_args[1]["json"]
        assert json_arg["name"] == "test_scene"


class TestResolveSceneId:
    """Tests for resolve_scene_id entity registry resolution."""

    @pytest.fixture
    def mock_client(self):
        return _make_mock_client()

    @pytest.mark.asyncio
    async def test_resolve_bare_id_matching_unique_id(self, mock_client):
        """Unique_id matches bare id → returned unchanged."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "movie_night"}}
        )

        result = await mock_client.resolve_scene_id("movie_night")

        assert result == "movie_night"
        mock_client.send_websocket_message.assert_called_once_with(
            {"type": "config/entity_registry/get", "entity_id": "scene.movie_night"}
        )

    @pytest.mark.asyncio
    async def test_resolve_renamed_scene(self, mock_client):
        """Renamed scene → return the registry's unique_id, not the entity_id slug."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "original_storage_key"}}
        )

        result = await mock_client.resolve_scene_id("renamed_scene")

        assert result == "original_storage_key"

    @pytest.mark.asyncio
    async def test_resolve_strips_scene_prefix(self, mock_client):
        """Caller may pass 'scene.<id>'; the resolver strips it before lookup."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "movie_night"}}
        )

        result = await mock_client.resolve_scene_id("scene.movie_night")

        assert result == "movie_night"
        mock_client.send_websocket_message.assert_called_once_with(
            {"type": "config/entity_registry/get", "entity_id": "scene.movie_night"}
        )

    @pytest.mark.asyncio
    async def test_resolve_falls_back_on_websocket_failure(self, mock_client):
        """WebSocket exceptions fall back to the bare id (no hard failure)."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("WS unavailable")
        )

        result = await mock_client.resolve_scene_id("test_scene")

        assert result == "test_scene"

    @pytest.mark.asyncio
    async def test_resolve_falls_back_on_unsuccessful_response(self, mock_client):
        """An entity-registry response with success=False falls back to bare id."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": {"code": "not_found"}}
        )

        result = await mock_client.resolve_scene_id("test_scene")

        assert result == "test_scene"
