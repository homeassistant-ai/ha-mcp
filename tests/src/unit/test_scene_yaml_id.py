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


async def test_the_registry_entry_of_a_removed_yaml_scene_blocks_no_create(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """``gone`` was a YAML scene; Core keeps its registry entry after it is
    removed and writes a ``restored`` state for it. A set by ``gone`` then
    stores a new scene under that key."""
    await setup_scenes(
        hass, [], yaml_only=[{"id": "gone", "name": "Gone Scene", "entities": _ON}]
    )
    await write_config_files(
        hass, {"configuration.yaml": "scene: !include scenes.yaml\n"}
    )
    await hass.services.async_call("scene", "reload", blocking=True)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("scene", "homeassistant", "gone")
    state = hass.states.get("scene.gone_scene")
    assert state is not None and state.attributes.get("restored") is True

    await ConfigSceneTools(core_client).ha_config_set_scene(
        scene_id="gone",
        config={"name": "Gone Scene", "entities": _ON},
        wait=False,
        MandatoryBPS=False,
    )

    stored = await core_client.get_scene_config("gone", _resolved=True)
    assert stored["config"]["name"] == "Gone Scene"
