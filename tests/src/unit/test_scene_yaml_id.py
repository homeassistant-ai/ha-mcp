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

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._config_dir_yaml import setup_scenes
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
async def test_a_yaml_scene_id_names_that_scene(
    hass: HomeAssistant, core_client: HomeAssistantClient, call: str, collided: bool
) -> None:
    """``yid`` is "X Scene" (``scene.x_scene``) in ``configuration.yaml``; with
    ``collided``, the stored scene ``zz`` named "Yid" holds ``scene.yid``. Each
    call names ``scene.x_scene`` as not editable and leaves ``scenes.yaml`` as
    it was."""
    stored = [{"id": "zz", "name": "Yid", "entities": _ON}] if collided else []
    await setup_scenes(
        hass, stored, yaml_only=[{"id": "yid", "name": "X Scene", "entities": _ON}]
    )
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
