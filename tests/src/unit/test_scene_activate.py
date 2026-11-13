"""Scene activation through ``ha_config_set_scene(activate=True)`` on real Core.

The scenes live in Core's ``scenes.yaml`` and switch ``input_boolean.tv``, so
an activation shows as Core's state, and a write's activation can only apply
the new snapshot once Core's ``scene.reload`` (scheduled after the config
API's answer) has run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config as conf_util
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import get_backup_manager
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._config_dir_yaml import setup_scenes
from ._tool_errors import error_of

_WRITE_MODES = ("config", "python_transform")


async def _scene(hass: HomeAssistant, entities: dict[str, Any]) -> None:
    """``movie_night`` (named "Film Night", so ``scene.film_night``) in scenes.yaml."""
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"tv": {}}}
    )
    await setup_scenes(
        hass, [{"id": "movie_night", "name": "Film Night", "entities": entities}]
    )


def _tv(hass: HomeAssistant) -> str:
    state = hass.states.get("input_boolean.tv")
    assert state is not None
    return state.state


async def _write_and_activate(
    tools: ConfigSceneTools, mode: str, entities: dict[str, Any]
) -> dict[str, Any]:
    """Write ``movie_night``'s ``entities`` through ``mode`` with ``activate=True``."""
    if mode == "config":
        return await tools.ha_config_set_scene(
            scene_id="movie_night",
            config={"name": "Film Night", "entities": entities},
            activate=True,
            wait=False,
            MandatoryBPS=False,
        )
    current = await tools.ha_config_get_scene(scene_id="movie_night")
    return await tools.ha_config_set_scene(
        scene_id="movie_night",
        python_transform=f"config['entities'] = {entities!r}",
        config_hash=current["config_hash"],
        activate=True,
        wait=False,
        MandatoryBPS=False,
    )


@pytest.mark.parametrize("scene_id", ["scene.film_night", "movie_night"])
async def test_activate_alone_applies_the_scene_without_writing_it(
    hass: HomeAssistant, core_client: HomeAssistantClient, scene_id: str
) -> None:
    """An entity_id or a storage key activates the scene; its config is untouched."""
    await _scene(hass, {"input_boolean.tv": {"state": "on"}})
    assert _tv(hass) == "off"

    with patch.object(
        core_client, "upsert_scene_config", wraps=core_client.upsert_scene_config
    ) as upsert:
        result = await ConfigSceneTools(core_client).ha_config_set_scene(
            scene_id=scene_id, activate=True, MandatoryBPS=False
        )

    assert result["action"] == "activate"
    assert result["entity_id"] == "scene.film_night"
    assert result["activated"] is True
    assert _tv(hass) == "on"
    upsert.assert_not_awaited()


async def test_activate_unknown_scene_is_entity_not_found(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's ``scene.turn_on`` accepts an unknown entity silently, so the tool
    must refuse it before calling the service."""
    await _scene(hass, {"input_boolean.tv": {"state": "on"}})

    with (
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as call_service,
        pytest.raises(ToolError) as exc,
    ):
        await ConfigSceneTools(core_client).ha_config_set_scene(
            scene_id="scene.missing", activate=True
        )

    assert error_of(exc)["code"] == "ENTITY_NOT_FOUND"
    call_service.assert_not_awaited()


async def test_activate_alone_refuses_a_category(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A category needs a config write; activating alone cannot assign one."""
    await _scene(hass, {"input_boolean.tv": {"state": "on"}})

    with (
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as call_service,
        pytest.raises(ToolError) as exc,
    ):
        await ConfigSceneTools(core_client).ha_config_set_scene(
            scene_id="scene.film_night", activate=True, category="evening"
        )

    assert "category" in str(exc.value).lower()
    call_service.assert_not_awaited()
    assert _tv(hass) == "off"


async def test_activate_alone_without_an_answer_is_connection_failed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _scene(hass, {"input_boolean.tv": {"state": "on"}})

    with (
        patch.object(
            core_client,
            "call_service",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
        ),
        pytest.raises(ToolError) as exc,
    ):
        await ConfigSceneTools(core_client).ha_config_set_scene(
            scene_id="scene.film_night", activate=True
        )

    assert error_of(exc)["code"] == "CONNECTION_FAILED"


@pytest.mark.parametrize("mode", _WRITE_MODES)
async def test_write_activates_the_new_snapshot_after_cores_reload(
    hass: HomeAssistant, core_client: HomeAssistantClient, mode: str
) -> None:
    """Core answers the write before its scene reload runs; activating then
    would apply the previous snapshot (here: leave the TV off)."""
    await _scene(hass, {"input_boolean.tv": {"state": "off"}})
    tools = ConfigSceneTools(core_client)
    calls_at_reload: list[tuple[int, int]] = []
    read_configuration = conf_util.async_hass_config_yaml

    async def slow_read(hass: HomeAssistant) -> dict[str, Any]:
        # In-process the reload finishes before the tool's next requests; on a
        # real install reading the configuration takes longer than they do.
        await asyncio.sleep(0.3)
        return await read_configuration(hass)

    with (
        patch.object(conf_util, "async_hass_config_yaml", slow_read),
        patch.object(
            core_client, "upsert_scene_config", wraps=core_client.upsert_scene_config
        ) as upsert,
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as call_service,
    ):

        @callback
        def reloaded(_event: Event) -> None:
            calls_at_reload.append((upsert.await_count, call_service.await_count))

        unsubscribe = hass.bus.async_listen("scene_reloaded", reloaded)
        try:
            result = await _write_and_activate(
                tools, mode, {"input_boolean.tv": {"state": "on"}}
            )
        finally:
            unsubscribe()

    # Written, then reloaded, and only then activated.
    assert calls_at_reload == [(1, 0)]
    call_service.assert_awaited_once()
    assert result["activated"] is True
    assert not any("reload" in w for w in result.get("warnings", []))
    assert _tv(hass) == "on"


@pytest.mark.parametrize("mode", _WRITE_MODES)
async def test_write_whose_activation_fails_still_reports_the_write(
    hass: HomeAssistant, core_client: HomeAssistantClient, mode: str
) -> None:
    """The scene names a light whose integration is not set up, so Core refuses
    ``scene.turn_on``; the config is written all the same."""
    await _scene(hass, {"input_boolean.tv": {"state": "off"}})
    hass.states.async_set("light.tv", "off")

    result = await _write_and_activate(
        ConfigSceneTools(core_client), mode, {"light.tv": {"state": "on"}}
    )

    assert result["success"] is True
    assert result["activated"] is False
    assert any("could not be activated" in w for w in result["warnings"])
    stored = await ConfigSceneTools(core_client).ha_config_get_scene(
        scene_id="movie_night"
    )
    assert stored["config"]["entities"] == {"light.tv": {"state": "on"}}


@pytest.mark.parametrize("mode", _WRITE_MODES)
async def test_write_is_not_activated_when_the_reload_cannot_be_watched(
    hass: HomeAssistant, core_client: HomeAssistantClient, mode: str
) -> None:
    """Without the reload event the tool cannot know the new snapshot is
    loaded, so it writes, does not activate, and says how to activate."""
    await _scene(hass, {"input_boolean.tv": {"state": "off"}})

    with (
        patch.object(
            HomeAssistantWebSocketClient,
            "subscribe_events",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
        ),
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as call_service,
    ):
        result = await _write_and_activate(
            ConfigSceneTools(core_client), mode, {"input_boolean.tv": {"state": "on"}}
        )

    call_service.assert_not_awaited()
    assert result["activated"] is False
    assert any(
        "not activated" in w and "scene_id='movie_night', activate=True" in w
        for w in result["warnings"]
    )
    await hass.async_block_till_done()
    assert _tv(hass) == "off"


async def test_only_a_write_takes_a_scene_backup(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Activating alone changes no config, so it leaves no restore point."""
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
    reset_global_settings()
    await _scene(hass, {"input_boolean.tv": {"state": "on"}})
    tools = ConfigSceneTools(core_client)

    def scene_snapshots() -> list[dict[str, Any]]:
        manager = get_backup_manager(core_client, get_global_settings())
        return manager.list_snapshots(domain="scene")

    await tools.ha_config_set_scene(scene_id="movie_night", activate=True)
    assert scene_snapshots() == []

    await tools.ha_config_set_scene(
        scene_id="movie_night",
        config={"name": "Film Night", "entities": {"input_boolean.tv": "off"}},
        activate=True,
        wait=False,
    )
    assert len(scene_snapshots()) == 1
