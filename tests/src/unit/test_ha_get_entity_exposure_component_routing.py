"""The exposure listing behind ``ha_get_entity``, on a real ``hass``.

Core's ``homeassistant/expose_entity/list`` gives a bare ``{entity_id:
{assistant: True}}`` map. When the component advertises ``exposure`` (with
``device_registry_child_semantics``), one ``ha_mcp_tools/exposure`` frame gives
the same map plus ``entity_info`` (friendly name, domain, area, floor, labels,
state). Without it, or when the component no longer knows the command, the
listing is Core's map alone. The warning that says why is pinned in
``test_entity_component_real_core.py``. Registries and exposure settings are
Core's own.
"""

from __future__ import annotations

import uuid
from typing import NamedTuple
from unittest.mock import patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import entity_exposure
from ha_mcp.tools.component_api import get_component_caps, invalidate_caps
from ha_mcp.tools.entity_exposure import WS_EXPOSURE
from ha_mcp.tools.tools_entities import EntityTools

from ._component_frames import observed, older_component

LEGACY_LIST = "homeassistant/expose_entity/list"


class Home(NamedTuple):
    """A lamp exposed to Assist and a fan exposed to Alexa."""

    lamp: str
    fan: str
    area: str


@pytest.fixture
async def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "config", {})
    return component_core_client


@pytest.fixture
async def home(hass: HomeAssistant, client: HomeAssistantClient) -> Home:
    suffix = uuid.uuid4().hex[:8]
    floor = fr.async_get(hass).async_create(f"Main {suffix}")
    area = ar.async_get(hass).async_create(f"Kitchen {suffix}", floor_id=floor.floor_id)
    label = lr.async_get(hass).async_create(f"Favorites {suffix}")
    registry = er.async_get(hass)
    lamp = registry.async_get_or_create("light", "test", "lamp").entity_id
    fan = registry.async_get_or_create("fan", "test", "fan").entity_id
    registry.async_update_entity(lamp, area_id=area.id, labels={label.label_id})
    hass.states.async_set(lamp, "on", {"friendly_name": "Lamp A"})
    hass.states.async_set(fan, "off", {"friendly_name": "Fan"})
    async_expose_entity(hass, "conversation", lamp, True)
    async_expose_entity(hass, "cloud.alexa", fan, True)
    return Home(lamp, fan, area.name)


def _core_map(home: Home) -> dict[str, dict[str, bool]]:
    return {home.lamp: {"conversation": True}, home.fan: {"cloud.alexa": True}}


async def test_the_listing_keeps_cores_map_and_names_each_entity(
    hass: HomeAssistant, client: HomeAssistantClient, home: Home
) -> None:
    with (
        observed(hass, WS_EXPOSURE) as frames,
        observed(hass, LEGACY_LIST) as legacy,
    ):
        resp = await EntityTools(client).ha_get_entity()

    assert resp["exposed_entities"] == _core_map(home)
    assert resp["count"] == 2
    assert set(resp["entity_info"]) == {home.lamp, home.fan}
    assert resp["entity_info"][home.lamp]["area"] == home.area
    assert legacy == []
    assert [frame.get("entity_id") for frame in frames] == [None]


async def test_entity_info_covers_only_the_entities_the_filter_keeps(
    hass: HomeAssistant, client: HomeAssistantClient, home: Home
) -> None:
    resp = await EntityTools(client).ha_get_entity(exposed_to="cloud.alexa")

    assert set(resp["exposed_entities"]) == {home.fan}
    assert set(resp["entity_info"]) == {home.fan}


@pytest.mark.parametrize(
    "missing",
    [
        pytest.param("exposure", id="no-exposure-command"),
        # Placement from a component without it can be stale for a child device.
        pytest.param("device_registry_child_semantics", id="no-child-semantics"),
    ],
)
async def test_an_older_component_lists_cores_map_alone(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    home: Home,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    older_component(monkeypatch, missing)

    with (
        observed(hass, WS_EXPOSURE) as frames,
        observed(hass, LEGACY_LIST) as legacy,
    ):
        resp = await EntityTools(client).ha_get_entity()

    assert resp["exposed_entities"] == _core_map(home)
    assert "entity_info" not in resp
    assert (len(legacy), frames) == (1, [])


async def test_a_downgraded_component_lists_cores_map_and_is_probed_again(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    home: Home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await get_component_caps(client) is not None
    monkeypatch.delitem(hass.data[websocket_api.DOMAIN], WS_EXPOSURE)

    with (
        patch.object(entity_exposure, "invalidate_caps", wraps=invalidate_caps) as drop,
        observed(hass, LEGACY_LIST) as legacy,
    ):
        resp = await EntityTools(client).ha_get_entity()

    assert resp["exposed_entities"] == _core_map(home)
    assert "entity_info" not in resp
    assert len(legacy) == 1
    drop.assert_called_once_with(client)
