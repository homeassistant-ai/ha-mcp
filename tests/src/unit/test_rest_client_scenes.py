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
    SceneResolution,
    SceneStorageConfigNotFoundError,
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


class TestSceneResolvedShortCircuit:
    """Skipping the redundant ``resolve_scene_id`` lookup on get/upsert/delete
    scene methods (issue #1813 P5 item 3). ``get``/``delete`` take
    ``_resolved=True``; ``upsert`` takes a separate ``resolved_id`` write-target
    (so ``scene_id`` stays the caller's id for the missing-``name`` default —
    #1935). ``_make_mock_client`` makes ``send_websocket_message`` raise, so
    ``assert_not_called`` proves the resolver was skipped rather than merely
    falling back."""

    @pytest.fixture
    def mock_client(self):
        return _make_mock_client()

    @pytest.mark.asyncio
    async def test_get_scene_resolved_skips_lookup(self, mock_client):
        mock_client._request = AsyncMock(return_value={"name": "S", "entities": {}})

        result = await mock_client.get_scene_config("storage_key", _resolved=True)

        assert result["scene_id"] == "storage_key"
        mock_client.send_websocket_message.assert_not_called()
        mock_client._request.assert_called_once_with(
            "GET", "config/scene/config/storage_key"
        )

    @pytest.mark.asyncio
    async def test_upsert_scene_resolved_id_skips_lookup(self, mock_client):
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        result = await mock_client.upsert_scene_config(
            {"name": "S", "entities": {"light.k": {"state": "on"}}},
            "renamed_scene",
            resolved_id="storage_key",
        )

        assert result["scene_id"] == "storage_key"
        mock_client.send_websocket_message.assert_not_called()
        assert mock_client._request.call_args[0][1] == "config/scene/config/storage_key"

    @pytest.mark.asyncio
    async def test_upsert_scene_resolved_id_name_defaults_to_caller_scene_id(
        self, mock_client
    ):
        """#1935 regression: with ``resolved_id`` provided (renamed scene), a
        missing ``name`` defaults to the CALLER's ``scene_id`` — NOT the storage
        key — while the write still targets the resolved key with no re-resolve."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        config = {"entities": {"light.k": {"state": "on"}}}  # no name
        result = await mock_client.upsert_scene_config(
            config, "new_slug", resolved_id="old_storage_key"
        )

        # Write targets the resolved storage key; resolver never consulted.
        assert result["scene_id"] == "old_storage_key"
        mock_client.send_websocket_message.assert_not_called()
        method, endpoint = mock_client._request.call_args.args
        assert endpoint == "config/scene/config/old_storage_key"
        # Name defaulted from the caller's id, not the stale storage key.
        assert mock_client._request.call_args.kwargs["json"]["name"] == "new_slug"

    @pytest.mark.asyncio
    async def test_upsert_scene_name_default_strips_entity_prefix(self, mock_client):
        """A caller-facing ``scene.<slug>`` id must NOT become the scene name
        verbatim — HA derives the entity_id from the name slug, so a prefixed
        default would rename the scene and change its entity_id on a plain update."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        config = {"entities": {"light.k": {"state": "on"}}}  # no name
        await mock_client.upsert_scene_config(
            config, "scene.movie_night", resolved_id="movie_night"
        )

        assert mock_client._request.call_args.kwargs["json"]["name"] == "movie_night"

    @pytest.mark.asyncio
    async def test_delete_scene_resolved_skips_lookup(self, mock_client):
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        result = await mock_client.delete_scene_config("storage_key", _resolved=True)

        assert result["scene_id"] == "storage_key"
        mock_client.send_websocket_message.assert_not_called()
        mock_client._request.assert_called_once_with(
            "DELETE", "config/scene/config/storage_key"
        )


class TestSceneStorageConfigNotFound:
    """Issue #1971: a scene entity that resolves in the registry but 404s on the
    scenes.yaml-backed config API (a Hue/vendor scene, or a raw-YAML scene) must
    surface as the not-storage-scene case, not a bare missing-entity 404.

    ``config/scene/config/{id}`` is backed ONLY by the managed ``scenes.yaml``
    (verified against HA-core), so it 404s for any scene not in that file even
    though the entity exists - the registry resolve is what tells the two apart.
    """

    @pytest.fixture
    def mock_client(self):
        return _make_mock_client()

    @pytest.mark.asyncio
    async def test_resolve_scene_returns_platform_and_hit(self, mock_client):
        """``_resolve_scene`` threads out the registry ``platform`` and a
        ``registry_hit`` flag - the two signals a bare storage key drops."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "hue-uuid-123", "platform": "hue"}}
        )

        resolution = await mock_client._resolve_scene("bedroom_sleepy")

        assert resolution == SceneResolution(
            storage_key="hue-uuid-123", registry_hit=True, platform="hue"
        )

    @pytest.mark.asyncio
    async def test_resolve_scene_miss_reports_no_hit(self, mock_client):
        """A failed registry lookup falls back to the bare id with
        ``registry_hit=False`` - a write path must not treat the guess as real."""
        # _make_mock_client's send_websocket_message raises → lookup miss.
        resolution = await mock_client._resolve_scene("ghost_scene")

        assert resolution == SceneResolution(
            storage_key="ghost_scene", registry_hit=False, platform=None
        )

    @pytest.mark.asyncio
    async def test_get_scene_404_after_registry_hit_raises_storage_config_error(
        self, mock_client
    ):
        """Entity resolves (Hue) but the config API 404s → the distinct
        SceneStorageConfigNotFoundError carrying the platform, NOT a bare 404."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "hue-uuid-123", "platform": "hue"}}
        )
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not found", status_code=404
            )
        )

        with pytest.raises(SceneStorageConfigNotFoundError) as exc_info:
            await mock_client.get_scene_config("bedroom_sleepy")

        err = exc_info.value
        assert err.status_code == 404
        assert err.platform == "hue"
        assert err.scene_id == "bedroom_sleepy"
        assert err.storage_key == "hue-uuid-123"

    @pytest.mark.asyncio
    async def test_get_scene_404_registry_miss_stays_generic(self, mock_client):
        """No registry hit → a genuinely missing entity → the historic bare
        404, NOT the not-storage-scene subclass."""
        # send_websocket_message raises (miss) via _make_mock_client.
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not found", status_code=404
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_scene_config("truly_missing")

        assert not isinstance(exc_info.value, SceneStorageConfigNotFoundError)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_scene_404_after_registry_hit_raises_storage_config_error(
        self, mock_client
    ):
        """The delete read-path 404 classifies identically when the caller
        threads a registry-hit resolution through (#1971 delete parity)."""
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not found", status_code=404
            )
        )
        resolution = SceneResolution(
            storage_key="hue-uuid-123", registry_hit=True, platform="hue"
        )

        with pytest.raises(SceneStorageConfigNotFoundError) as exc_info:
            await mock_client.delete_scene_config(
                "bedroom_sleepy", resolution=resolution
            )

        assert exc_info.value.platform == "hue"
        assert exc_info.value.scene_id == "bedroom_sleepy"
        # Endpoint used the resolved storage key, not the caller slug.
        mock_client._request.assert_called_once_with(
            "DELETE", "config/scene/config/hue-uuid-123"
        )

    @pytest.mark.asyncio
    async def test_delete_scene_default_still_resolves(self, mock_client):
        """Contrast: without ``_resolved`` the registry resolver is consulted."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"result": {"unique_id": "storage_key"}}
        )
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        result = await mock_client.delete_scene_config("movie_night")

        assert result["scene_id"] == "storage_key"
        mock_client.send_websocket_message.assert_called_once()
