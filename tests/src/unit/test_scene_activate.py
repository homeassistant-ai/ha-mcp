"""Scene activation through ha_config_set_scene(activate=True)."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
    SceneResolution,
)
from ha_mcp.tools import entity_registration, tools_config_scenes
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    # Credential-less: the component route and the reload waiter stay off.
    client.base_url = None
    client.token = None
    client.upsert_scene_config = AsyncMock(
        return_value={"success": True, "scene_id": "movie_night"}
    )
    client.get_entity_state = AsyncMock(
        return_value={"entity_id": "scene.movie_night", "state": "unknown"}
    )
    client.call_service = AsyncMock(return_value=[])
    client.get_services = AsyncMock(return_value=[])
    client.get_states = AsyncMock(return_value=[])
    client.send_websocket_message = AsyncMock(
        return_value={
            "success": True,
            "result": [
                {
                    "entity_id": "scene.movie_night",
                    "unique_id": "movie_night",
                    "platform": "homeassistant",
                }
            ],
        }
    )
    client._resolve_scene = AsyncMock(
        side_effect=lambda sid: SceneResolution(
            storage_key=sid.removeprefix("scene."),
            registry_hit=False,
            platform=None,
        )
    )
    client._scene_state_exists = AsyncMock(return_value=False)
    return client


class _ConfirmedReload:
    """Stand-in for config_reload_waiter whose reload is always confirmed."""

    def __init__(self, _client: Any, _event_type: str, *, enabled: bool) -> None:
        pass

    async def __aenter__(self) -> Any:
        async def wait_for_reload() -> bool:
            return True

        return wait_for_reload

    async def __aexit__(self, *_exc: Any) -> None:
        pass


_SCENE = {"name": "Movie Night", "entities": {"light.tv": {"state": "on"}}}
_WRITE_MODES = ("config", "python_transform")


async def _write_and_activate(
    tools: ConfigSceneTools, mode: str, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Write ``movie_night`` through ``mode`` with ``activate=True``."""
    if mode == "config":
        return await tools.ha_config_set_scene(
            scene_id="movie_night",
            config=dict(_SCENE),
            activate=True,
            wait=False,
            MandatoryBPS=False,
        )

    async def fetch_and_verify_hash(
        scene_id: str, config_hash: str, action: str
    ) -> tuple[dict[str, Any], str]:
        return {"id": "movie_night", **_SCENE}, "movie_night"

    async def get_config(
        scene_id: str, *, _resolved: bool = False
    ) -> tuple[dict[str, Any], str, str]:
        return {"id": "movie_night", **_SCENE}, "new-hash", "movie_night"

    monkeypatch.setattr(tools, "_fetch_and_verify_hash", fetch_and_verify_hash)
    monkeypatch.setattr(tools, "_get_scene_config_internal", get_config)
    return await tools.ha_config_set_scene(
        scene_id="movie_night",
        python_transform="config['name'] = 'Film Night'",
        config_hash="old-hash",
        activate=True,
        wait=False,
        MandatoryBPS=False,
    )


@pytest.fixture
def tools(mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> ConfigSceneTools:
    monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)
    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 0)
    return ConfigSceneTools(mock_client)


@pytest.mark.unit
@pytest.mark.parametrize("scene_id", ["scene.movie_night", "movie_night"])
async def test_standalone_activate_calls_scene_turn_on(
    tools: ConfigSceneTools, mock_client: MagicMock, scene_id: str
) -> None:
    result = await tools.ha_config_set_scene(
        scene_id=scene_id, activate=True, MandatoryBPS=False
    )

    assert result["action"] == "activate"
    assert result["entity_id"] == "scene.movie_night"
    assert result["activated"] is True
    mock_client.call_service.assert_awaited_once_with(
        "scene", "turn_on", {"entity_id": "scene.movie_night"}
    )
    mock_client.upsert_scene_config.assert_not_called()


@pytest.mark.unit
async def test_standalone_activate_unknown_scene_is_entity_not_found(
    tools: ConfigSceneTools, mock_client: MagicMock
) -> None:
    mock_client.get_entity_state = AsyncMock(
        side_effect=HomeAssistantAPIError("not found", status_code=404)
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_scene(scene_id="scene.missing", activate=True)

    assert json.loads(str(exc_info.value))["error"]["code"] == "ENTITY_NOT_FOUND"
    mock_client.call_service.assert_not_called()


@pytest.mark.unit
async def test_standalone_activate_rejects_category(
    tools: ConfigSceneTools, mock_client: MagicMock
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_scene(
            scene_id="scene.movie_night", activate=True, category="evening"
        )

    assert "category" in str(exc_info.value).lower()
    mock_client.call_service.assert_not_called()


@pytest.mark.unit
async def test_standalone_activate_service_failure_raises(
    tools: ConfigSceneTools, mock_client: MagicMock
) -> None:
    mock_client.call_service = AsyncMock(
        side_effect=HomeAssistantConnectionError("connection lost")
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_scene(scene_id="scene.movie_night", activate=True)

    assert json.loads(str(exc_info.value))["error"]["code"] == "CONNECTION_FAILED"


@pytest.mark.unit
@pytest.mark.parametrize("mode", _WRITE_MODES)
async def test_config_write_activates_after_scene_reload(
    tools: ConfigSceneTools,
    mock_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    order: list[str] = []

    async def upsert(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        order.append("write")
        return {"success": True, "scene_id": "movie_night"}

    async def call_service(*_args: Any, **_kwargs: Any) -> list[Any]:
        order.append("activate")
        return []

    mock_client.upsert_scene_config = AsyncMock(side_effect=upsert)
    mock_client.call_service = AsyncMock(side_effect=call_service)

    class _Waiter:
        def __init__(self, _client: Any, event_type: str, *, enabled: bool) -> None:
            assert event_type == "scene_reloaded"
            assert enabled is True

        async def __aenter__(self) -> Any:
            order.append("subscribe")

            async def wait_for_reload() -> bool:
                order.append("reloaded")
                return True

            return wait_for_reload

        async def __aexit__(self, *_exc: Any) -> None:
            order.append("unsubscribe")

    monkeypatch.setattr(tools_config_scenes, "config_reload_waiter", _Waiter)

    result = await _write_and_activate(tools, mode, monkeypatch)

    assert order == ["subscribe", "write", "reloaded", "unsubscribe", "activate"]
    assert result["activated"] is True
    assert not any("reload" in w for w in result.get("warnings", []))


@pytest.mark.unit
@pytest.mark.parametrize("mode", _WRITE_MODES)
async def test_config_write_activation_failure_is_partial_success(
    tools: ConfigSceneTools,
    mock_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setattr(tools_config_scenes, "config_reload_waiter", _ConfirmedReload)
    mock_client.call_service = AsyncMock(
        side_effect=HomeAssistantAPIError("scene failed")
    )

    result = await _write_and_activate(tools, mode, monkeypatch)

    assert result["success"] is True
    assert result["activated"] is False
    assert any("could not be activated" in w for w in result["warnings"])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"scene_id": "movie_night", "activate": True}, True),
        ({"scene_id": "movie_night", "activate": True, "config": {}}, False),
        ({"scene_id": "movie_night", "config": {}}, False),
    ],
)
def test_scene_backup_skips_only_standalone_activation(
    kwargs: dict[str, Any], expected: bool
) -> None:
    assert tools_config_scenes._skip_scene_activation_backup(kwargs) is expected


@pytest.mark.unit
@pytest.mark.parametrize("mode", _WRITE_MODES)
async def test_config_write_skips_activation_when_reload_unconfirmed(
    tools: ConfigSceneTools,
    mock_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    result = await _write_and_activate(tools, mode, monkeypatch)

    mock_client.call_service.assert_not_called()
    assert result["activated"] is False
    assert any(
        "not activated" in w and "scene_id='movie_night', activate=True" in w
        for w in result["warnings"]
    )
