"""The component's ``entity_lookup`` under the scene tools, and ``reference_data``.

Scenes live in ``scenes.yaml`` on a real ``hass``; the server's own client
talks to Core and to the component's commands registered on it
(``component_core_client``), so every registry answer is Core's.

``reference_data`` is no longer read by this server; servers released before
``config_references`` still read it, so its reply shape stays pinned.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import const as core_const
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.websocket_api.constants import (
    WS_INFO,
    WS_REFERENCE_DATA,
)
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import ws_waiters
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._component_core import ws_call, ws_result
from ._config_dir_yaml import setup_scenes
from ._core_setup import add_test_entry


async def _removal_waits_on(client: HomeAssistantClient, scene_id: str) -> str:
    """Remove ``scene_id`` and return the entity the removal waited to go."""
    real = ws_waiters.wait_for_entity_removed
    with patch.object(ws_waiters, "wait_for_entity_removed", wraps=real) as wait:
        result = await ConfigSceneTools(client).ha_config_remove_scene(
            scene_id=scene_id, wait=True
        )
    assert result["success"] is True
    assert "warnings" not in result, result
    assert wait.await_args is not None
    entity_id: str = wait.await_args.args[1]
    return entity_id


async def test_a_scene_sharing_a_unique_id_with_a_light_resolves_to_the_scene(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """The light's ``unique_id`` collides with the scene's storage key; the
    removal's ``domain="scene"`` narrowing never picks the light."""
    await setup_scenes(
        hass,
        [{"id": "movie_night", "name": "Movie Night", "entities": {"light.x": "on"}}],
    )
    entity_registry.async_get_or_create(
        "light", "hue", "movie_night", suggested_object_id="movie_night"
    )
    hass.states.async_set("light.movie_night", "on")

    assert await _removal_waits_on(component_core_client, "movie_night") == (
        "scene.movie_night"
    )


async def test_a_scene_named_apart_from_its_key_resolves_by_unique_id(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Core derives a scene's entity_id from its name, not its storage key."""
    await setup_scenes(
        hass,
        [
            {
                "id": "night_light_led_desk_strip",
                "name": "LED desk strip night light",
                "entities": {"light.x": "on"},
            }
        ],
    )

    entity_id = await _removal_waits_on(
        component_core_client, "night_light_led_desk_strip"
    )

    assert entity_id == "scene.led_desk_strip_night_light"


# --- reference_data ---------------------------------------------------------
@pytest.fixture
def light_and_sensor(hass: HomeAssistant) -> None:
    hass.states.async_set("light.a", "on")
    hass.states.async_set("sensor.b", "1")


async def test_targets_are_the_live_registry_ids(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    light_and_sensor: None,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """Without them the validator could not tell a typo'd device, area, floor
    or label id in a target from a real one; child devices count too."""
    entry = add_test_entry(hass)
    hub = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "hub")}
    )
    port = device_registry.async_get_or_create_child(
        config_entry_id=entry.entry_id,
        identifiers={("test", "port")},
        parent_device_id=hub.id,
    )
    area_registry.async_create("Kitchen")
    floor_registry.async_create("Ground")
    label_registry.async_create("Night")

    got = await ws_result(component_ws, WS_REFERENCE_DATA, include_targets=True)

    assert {key: sorted(got[key]) for key in got if key.endswith("_ids")} == {
        "entity_ids": ["light.a", "sensor.b"],
        "device_ids": sorted([hub.id, port.id]),
        "area_ids": ["kitchen"],
        "floor_ids": ["ground"],
        "label_ids": ["night"],
    }


async def test_targets_carry_cores_match_sentinels(
    component_ws: MockHAClientWebSocket,
    light_and_sensor: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core accepts ``none`` / ``all`` in a target, and the validator warned
    that no area "none" exists; the values come from Core, not a copy."""
    monkeypatch.setattr(core_const, "ENTITY_MATCH_NONE", "core-none")
    monkeypatch.setattr(core_const, "ENTITY_MATCH_ALL", "core-all")

    got = await ws_result(component_ws, WS_REFERENCE_DATA, include_targets=True)

    assert got["match_sentinels"] == ["core-none", "core-all"]


async def test_reply_keeps_its_shape_unless_targets_are_asked_for(
    component_ws: MockHAClientWebSocket, light_and_sensor: None
) -> None:
    got = await ws_result(component_ws, WS_REFERENCE_DATA)

    assert set(got) == {"services", "entity_ids"}


async def test_a_missing_target_registry_is_an_error_not_every_target_flagged(
    component_ws: MockHAClientWebSocket, light_and_sensor: None
) -> None:
    with patch.object(fr, "async_get", side_effect=RuntimeError("core drift")):
        reply = await ws_call(component_ws, WS_REFERENCE_DATA, include_targets=True)

    assert reply["success"] is False


async def test_targets_are_advertised(component_ws: MockHAClientWebSocket) -> None:
    info: dict[str, Any] = await ws_result(component_ws, WS_INFO)

    assert "reference_data_targets" in info["capabilities"]
