"""A scene ``id`` defined outside ``scenes.yaml``, read through the config tools.

Core's scene config API reads only ``scenes.yaml``; a YAML scene with an ``id``
in ``configuration.yaml`` still registers under that id. Its id names it, even
while a stored scene holds ``scene.<id>``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._config_dir_yaml import setup_scenes, write_config_files
from ._tool_errors import error_body

_ON = {"sun.sun": "above_horizon"}

_CALLS: dict[str, Callable[[ConfigSceneTools], Awaitable[Any]]] = {
    "get": lambda t: t.ha_config_get_scene(scene_id="yid"),
    "set": lambda t: t.ha_config_set_scene(
        scene_id="yid",
        config={"name": "Rewritten", "entities": _ON},
        wait=False,
        MandatoryBPS=False,
    ),
    "remove": lambda t: t.ha_config_remove_scene(scene_id="yid"),
}


@pytest.mark.parametrize("call", list(_CALLS))
@pytest.mark.parametrize("collided", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
async def test_a_yaml_scene_id_names_that_scene(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    call: str,
    collided: bool,
    disabled: bool,
) -> None:
    """``yid`` is "X Scene" (``scene.x_scene``) in ``configuration.yaml``; with
    ``collided``, the stored scene ``zz`` named "Yid" holds ``scene.yid``; with
    ``disabled``, ``scene.x_scene`` is disabled and has no state. Each call
    names ``scene.x_scene`` as not editable and leaves ``scenes.yaml`` as it
    was."""
    stored = [{"id": "zz", "name": "Yid", "entities": _ON}] if collided else []
    await setup_scenes(
        hass, stored, yaml_only=[{"id": "yid", "name": "X Scene", "entities": _ON}]
    )
    if disabled:
        er.async_get(hass).async_update_entity(
            "scene.x_scene", disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert hass.states.get("scene.x_scene") is None
    scenes_yaml = Path(hass.config.path("scenes.yaml"))
    before = await hass.async_add_executor_job(scenes_yaml.read_text)

    with pytest.raises(ToolError) as exc:
        await _CALLS[call](ConfigSceneTools(core_client))

    body = error_body(exc)
    assert body["error"]["code"] == "CONFIG_NOT_FOUND"
    suggestions = " ".join(body["error"].get("suggestions") or body["suggestions"])
    assert "scene_id='scene.x_scene'" in suggestions
    assert "scene.yid" not in suggestions
    assert await hass.async_add_executor_job(scenes_yaml.read_text) == before


@pytest.mark.parametrize("disabled", [False, True])
async def test_a_yaml_scene_whose_entity_is_its_id_is_not_written(
    hass: HomeAssistant, core_client: HomeAssistantClient, disabled: bool
) -> None:
    """``yid`` named "Yid" holds ``scene.yid``, live or disabled: a set by
    ``yid`` is refused and leaves ``scenes.yaml`` as it was."""
    await setup_scenes(
        hass, [], yaml_only=[{"id": "yid", "name": "Yid", "entities": _ON}]
    )
    if disabled:
        er.async_get(hass).async_update_entity(
            "scene.yid", disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert hass.states.get("scene.yid") is None
    scenes_yaml = Path(hass.config.path("scenes.yaml"))
    before = await hass.async_add_executor_job(scenes_yaml.read_text)

    with pytest.raises(ToolError) as exc:
        await _CALLS["set"](ConfigSceneTools(core_client))

    assert error_body(exc)["error"]["code"] == "CONFIG_NOT_FOUND"
    assert await hass.async_add_executor_job(scenes_yaml.read_text) == before


@pytest.mark.parametrize(
    ("name", "entity_id"), [("Gone Scene", "scene.gone_scene"), ("Gone", "scene.gone")]
)
async def test_the_registry_entry_of_a_removed_yaml_scene_blocks_no_create(
    hass: HomeAssistant, core_client: HomeAssistantClient, name: str, entity_id: str
) -> None:
    """``gone`` was a YAML scene; Core keeps its registry entry after it is
    removed and writes a ``restored`` state for it. A set by ``gone`` then
    stores a new scene under that key, also when the entity is ``scene.gone``;
    before that, a get by ``gone`` finds no scene, while a get by the entity_id
    still answers for the leftover entry."""
    await setup_scenes(
        hass, [], yaml_only=[{"id": "gone", "name": name, "entities": _ON}]
    )
    await write_config_files(
        hass, {"configuration.yaml": "scene: !include scenes.yaml\n"}
    )
    await hass.services.async_call("scene", "reload", blocking=True)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("scene", "homeassistant", "gone") == entity_id
    state = hass.states.get(entity_id)
    assert state is not None and state.attributes.get("restored") is True
    tools = ConfigSceneTools(core_client)
    with pytest.raises(ToolError) as exc:
        await tools.ha_config_get_scene(scene_id=entity_id)
    assert error_body(exc)["error"]["code"] == "CONFIG_NOT_FOUND"
    with pytest.raises(ToolError) as exc:
        await tools.ha_config_get_scene(scene_id="gone")
    assert error_body(exc)["error"]["code"] == "ENTITY_NOT_FOUND"

    await tools.ha_config_set_scene(
        scene_id="gone",
        config={"name": name, "entities": _ON},
        wait=False,
        MandatoryBPS=False,
    )

    stored = await core_client.get_scene_config("gone", _resolved=True)
    assert stored["config"]["name"] == name


async def test_an_integration_scene_restored_at_startup_still_answers_by_its_slug(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A Hue scene ``evening`` whose bridge was offline at startup holds only a
    restored state; a get by ``evening`` still names it as an integration
    scene."""
    await setup_scenes(hass, [])
    entry = er.async_get(hass).async_get_or_create(
        "scene", "hue", "evening", suggested_object_id="evening"
    )
    entry.write_unavailable_state(hass)

    with pytest.raises(ToolError) as exc:
        await ConfigSceneTools(core_client).ha_config_get_scene(scene_id="evening")

    body = error_body(exc)
    assert body["error"]["code"] == "CONFIG_NOT_FOUND"
    assert body["platform"] == "hue"


@pytest.mark.parametrize("call", ["get", "set"])
async def test_a_stored_scene_with_a_restored_state_still_answers_by_its_slug(
    hass: HomeAssistant, core_client: HomeAssistantClient, call: str
) -> None:
    """Stored scene ``k1700`` named "Movie" holds ``scene.movie``; with
    ``scenes.yaml`` no longer included, Core restores its state. A get by
    ``movie`` still returns ``k1700`` and a set by ``movie`` writes nothing."""
    await setup_scenes(hass, [{"id": "k1700", "name": "Movie", "entities": _ON}])
    await write_config_files(hass, {"configuration.yaml": "homeassistant:\n"})
    await hass.services.async_call("scene", "reload", blocking=True)
    state = hass.states.get("scene.movie")
    assert state is not None and state.attributes.get("restored") is True
    scenes_yaml = Path(hass.config.path("scenes.yaml"))
    before = await hass.async_add_executor_job(scenes_yaml.read_text)
    tools = ConfigSceneTools(core_client)

    if call == "get":
        result = await tools.ha_config_get_scene(scene_id="movie")
        assert result["config"]["id"] == "k1700"
    else:
        with pytest.raises(ToolError) as exc:
            await tools.ha_config_set_scene(
                scene_id="movie",
                config={"name": "Movie", "entities": _ON},
                wait=False,
                MandatoryBPS=False,
            )
        assert error_body(exc)["error"]["code"] == "VALIDATION_FAILED"
    assert await hass.async_add_executor_job(scenes_yaml.read_text) == before
