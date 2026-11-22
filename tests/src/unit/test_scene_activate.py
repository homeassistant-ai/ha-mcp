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

import httpx
import pytest
from homeassistant import config as conf_util
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
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

from ._config_dir_yaml import setup_scenes, write_config_files
from ._tool_errors import error_body, error_of

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


async def test_activate_by_key_never_reaches_the_scene_named_after_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``key_a`` ("First") has no registered entity yet, while ``key_b``
    ("Key A") is ``scene.key_a``: activating ``key_a`` turns on neither."""
    monkeypatch.setattr("ha_mcp.tools.entity_registration.RESOLVE_TIMEOUT", 0.05)
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"tv": {}}}
    )
    on = {"input_boolean.tv": {"state": "on"}}
    await setup_scenes(
        hass,
        [
            {"id": "key_a", "name": "First", "entities": on},
            {"id": "key_b", "name": "Key A", "entities": on},
        ],
    )
    er.async_get(hass).async_update_entity("scene.first", new_unique_id="pending")

    with pytest.raises(ToolError) as exc:
        await ConfigSceneTools(core_client).ha_config_set_scene(
            scene_id="key_a", activate=True
        )

    body = error_body(exc)
    assert (body["error"]["code"], body.get("scene_id")) == (
        "ENTITY_NOT_FOUND",
        "key_a",
    )
    assert _tv(hass) == "off"


async def test_activate_by_the_id_of_a_yaml_scene_outside_scenes_yaml(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's config API reads only ``scenes.yaml``, yet ``key_b`` ("Other")
    from ``configuration.yaml`` is still a scene id: activating it turns on
    ``scene.other``, not ``scene.key_b``, which is the stored scene "Key B"."""
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"tv": {}, "lamp": {}}}
    )
    await setup_scenes(
        hass,
        [{"id": "zz", "name": "Key B", "entities": {"input_boolean.lamp": "on"}}],
        yaml_only=[
            {"id": "key_b", "name": "Other", "entities": {"input_boolean.tv": "on"}}
        ],
    )

    result = await ConfigSceneTools(core_client).ha_config_set_scene(
        scene_id="key_b", activate=True
    )

    assert result["entity_id"] == "scene.other"
    await hass.async_block_till_done()
    lamp = hass.states.get("input_boolean.lamp")
    assert (_tv(hass), lamp and lamp.state) == ("on", "off")


@pytest.mark.parametrize("scene_id", ["gone", "scene.gone"])
async def test_activate_a_removed_yaml_scene_is_entity_unavailable(
    hass: HomeAssistant, core_client: HomeAssistantClient, scene_id: str
) -> None:
    """``gone`` was a YAML scene; after its removal Core keeps ``scene.gone``
    as an unavailable restored state, and its ``scene.turn_on`` would skip it
    without an error, so the tool refuses before calling the service."""
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"tv": {}}}
    )
    await setup_scenes(
        hass,
        [],
        yaml_only=[
            {"id": "gone", "name": "Gone", "entities": {"input_boolean.tv": "on"}}
        ],
    )
    await write_config_files(
        hass, {"configuration.yaml": "scene: !include scenes.yaml\n"}
    )
    await hass.services.async_call("scene", "reload", blocking=True)
    state = hass.states.get("scene.gone")
    assert state is not None and state.attributes.get("restored") is True

    with (
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as call_service,
        pytest.raises(ToolError) as exc,
    ):
        await ConfigSceneTools(core_client).ha_config_set_scene(
            scene_id=scene_id, activate=True
        )

    assert error_of(exc)["code"] == "ENTITY_UNAVAILABLE"
    call_service.assert_not_awaited()
    assert _tv(hass) == "off"


async def test_activate_by_key_whose_config_read_fails_activates_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``key_a`` has no registered entity and ``key_b`` holds ``scene.key_a``:
    when the config read of ``key_a`` fails with anything but Core's admin
    refusal, the error surfaces and ``scene.key_a`` stays off."""
    monkeypatch.setattr("ha_mcp.tools.entity_registration.RESOLVE_TIMEOUT", 0.05)
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"tv": {}}}
    )
    on = {"input_boolean.tv": {"state": "on"}}
    await setup_scenes(
        hass,
        [
            {"id": "key_a", "name": "First", "entities": on},
            {"id": "key_b", "name": "Key A", "entities": on},
        ],
    )
    er.async_get(hass).async_update_entity("scene.first", new_unique_id="pending")
    # Core keeps the loaded scenes but can no longer read their config.
    await write_config_files(hass, {"scenes.yaml": "- id: [unclosed\n"})
    statuses: list[int] = []

    async def record(response: httpx.Response) -> None:
        if response.url.path.endswith("/config/scene/config/key_a"):
            statuses.append(response.status_code)

    core_client.httpx_client.event_hooks["response"].append(record)
    with pytest.raises(ToolError):
        await ConfigSceneTools(core_client).ha_config_set_scene(
            scene_id="key_a", activate=True
        )

    assert statuses and all(status >= 500 for status in statuses)
    assert _tv(hass) == "off"


@pytest.mark.parametrize(
    ("scene_id", "entity_id"),
    [("movie_night", "scene.film_night"), ("no_id", "scene.no_id")],
)
async def test_a_non_admin_token_activates_by_bare_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    scene_id: str,
    entity_id: str,
) -> None:
    """Core lets only admins read ``scenes.yaml``: a stored scene activates
    through its registry entry, an id-less YAML scene by its slug."""
    assert await async_setup_component(hass, "auth", {})
    for user in await hass.auth.async_get_users():
        if not user.system_generated:
            await hass.auth.async_update_user(user, group_ids=["system-users"])
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"tv": {}}}
    )
    on = {"input_boolean.tv": "on"}
    await setup_scenes(
        hass,
        [{"id": "movie_night", "name": "Film Night", "entities": on}],
        yaml_only=[{"name": "No Id", "entities": on}],
    )

    result = await ConfigSceneTools(core_client).ha_config_set_scene(
        scene_id=scene_id, activate=True
    )

    assert await core_client.token_is_admin() is False
    assert result["entity_id"] == entity_id
    await hass.async_block_till_done()
    assert _tv(hass) == "on"


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
