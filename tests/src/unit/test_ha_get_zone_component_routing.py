"""``ha_get_zone`` lists every zone from the component, or Core's zone/list without it.

Core's ``zone/list`` serves only the zone storage, so the home zone (from the
general settings) and YAML zones come from their states. The component's
``helpers_list`` serves all of them in one frame. Core holds a stored zone
("Work", created through ``zone/create``), a YAML zone ("School") and its home
zone; what each read sent is what Core received.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import websocket_api
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.component_api import get_component_caps, invalidate_caps
from ha_mcp.tools.tools_zones import ZoneTools

from ._command_faults import dropped_on, pooled_socket, received, unanswered
from ._tool_errors import error_of

HELPERS_LIST = "ha_mcp_tools/helpers_list"
INFO = "ha_mcp_tools/info"
ZONE_LIST = "zone/list"
REGISTRY_LIST = "config/entity_registry/list"
LEGACY_WARNING = "served via legacy path"


async def _zone_client(
    hass: HomeAssistant, client: HomeAssistantClient
) -> HomeAssistantClient:
    """``client`` on a Core with the three zones and its registry commands."""
    assert await async_setup_component(hass, "config", {})
    await _zones(hass, client)
    return client


async def _zones(hass: HomeAssistant, client: HomeAssistantClient) -> None:
    assert await async_setup_component(
        hass,
        "zone",
        {"zone": [{"name": "School", "latitude": 1.0, "longitude": 2.0}]},
    )
    created = await client.send_websocket_message(
        {
            "type": "zone/create",
            "name": "Work",
            "latitude": 40.0,
            "longitude": -74.0,
            "radius": 100,
            "icon": "mdi:briefcase",
        }
    )
    assert created["result"]["id"] == "work"


@pytest.fixture
async def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The component on a Core with the three zones and its registry commands."""
    return await _zone_client(hass, component_core_client)


@pytest.fixture
async def legacy_client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The same Core without the component."""
    return await _zone_client(hass, core_client)


def _by_id(resp: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {zone["id"]: zone for zone in resp["zones"]}


def _legacy_warnings(resp: dict[str, Any]) -> list[str]:
    return [w for w in resp.get("warnings", []) if LEGACY_WARNING in w]


async def test_one_component_frame_lists_stored_yaml_and_home_zones(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    with received(hass, INFO, HELPERS_LIST, ZONE_LIST) as seen:
        first = await ZoneTools(client).ha_get_zone()
        second = await ZoneTools(client).ha_get_zone()

    assert set(_by_id(first)) == set(_by_id(second)) == {"work", "school", "home"}
    assert seen[ZONE_LIST] == []
    assert len(seen[INFO]) == 1
    assert [
        (m["helper_types"], m["include_flow_helpers"]) for m in seen[HELPERS_LIST]
    ] == [
        (["zone"], False),
        (["zone"], False),
    ]


async def test_a_stored_zone_reads_the_same_with_and_without_the_component(
    hass: HomeAssistant, legacy_client: HomeAssistantClient
) -> None:
    legacy = _by_id(await ZoneTools(legacy_client).ha_get_zone())["work"]
    websocket_api.async_register_commands(hass)
    invalidate_caps(legacy_client)

    with received(hass, HELPERS_LIST) as seen:
        component = _by_id(await ZoneTools(legacy_client).ha_get_zone())["work"]

    assert len(seen[HELPERS_LIST]) == 1
    assert component == legacy
    assert (component["entity_id"], component["editable"], component["source"]) == (
        "zone.work",
        True,
        "storage",
    )


async def test_the_home_and_yaml_zones_are_listed_as_not_editable(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    zones = _by_id(await ZoneTools(client).ha_get_zone())

    for zone_id in ("home", "school"):
        assert (zones[zone_id]["editable"], zones[zone_id]["source"]) == (False, "yaml")
    assert zones["home"]["latitude"] == hass.config.latitude
    assert zones["school"]["entity_id"] == "zone.school"


async def test_a_stored_zone_is_read_by_id_through_the_component(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    with received(hass, ZONE_LIST) as seen:
        resp = await ZoneTools(client).ha_get_zone(zone_id="work")

    assert (resp["zone_id"], resp["zone"]["name"], resp["zone"]["source"]) == (
        "work",
        "Work",
        "storage",
    )
    assert seen[ZONE_LIST] == []


async def test_an_unknown_zone_is_not_found(client: HomeAssistantClient) -> None:
    with pytest.raises(ToolError, match="Zone not found"):
        await ZoneTools(client).ha_get_zone(zone_id="does_not_exist")


async def test_without_the_component_cores_zone_list_and_states_serve(
    hass: HomeAssistant, legacy_client: HomeAssistantClient
) -> None:
    with received(hass, ZONE_LIST) as seen:
        resp = await ZoneTools(legacy_client).ha_get_zone()

    assert set(_by_id(resp)) == {"work", "school", "home"}
    assert len(seen[ZONE_LIST]) == 1
    assert _legacy_warnings(resp) == []


async def test_a_downgraded_component_falls_back_silently(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], HELPERS_LIST)

    with received(hass, ZONE_LIST) as seen:
        resp = await ZoneTools(client).ha_get_zone()

    assert len(seen[ZONE_LIST]) == 1
    assert set(_by_id(resp)) == {"work", "school", "home"}
    assert _legacy_warnings(resp) == []


async def test_a_listing_core_never_answers_falls_back_with_a_warning(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with received(hass, ZONE_LIST) as seen:
        async with unanswered(hass, HELPERS_LIST, monkeypatch):
            resp = await ZoneTools(client).ha_get_zone()

    assert len(seen[ZONE_LIST]) == 1
    assert set(_by_id(resp)) == {"work", "school", "home"}
    assert len(_legacy_warnings(resp)) == 1


async def test_a_dropped_socket_falls_back_with_a_warning(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The legacy listing reconnects, so it does not die with the socket."""
    assert await get_component_caps(client) is not None
    socket = await pooled_socket(client)

    with dropped_on(hass, HELPERS_LIST, socket), received(hass, ZONE_LIST) as seen:
        resp = await ZoneTools(client).ha_get_zone()

    assert len(seen[ZONE_LIST]) == 1
    assert set(_by_id(resp)) == {"work", "school", "home"}
    assert len(_legacy_warnings(resp)) == 1


async def test_an_unreadable_registry_only_costs_the_registry_icons(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Without Core's ``config`` integration the registry list is refused; the
    component listed every zone, so an unknown zone is still not found."""
    await _zones(hass, component_core_client)
    tools = ZoneTools(component_core_client)

    resp = await tools.ha_get_zone()
    with pytest.raises(ToolError) as exc:
        await tools.ha_get_zone(zone_id="nowhere")

    assert set(_by_id(resp)) == {"work", "school", "home"}
    assert any(
        w.startswith("Icons set in the entity registry are missing")
        for w in resp["warnings"]
    )
    assert error_of(exc)["code"] == "RESOURCE_NOT_FOUND"


async def test_a_registry_read_that_drops_keeps_the_component_listing(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    assert await get_component_caps(client) is not None
    socket = await pooled_socket(client)

    with dropped_on(hass, REGISTRY_LIST, socket):
        resp = await ZoneTools(client).ha_get_zone()

    assert set(_by_id(resp)) == {"work", "school", "home"}
    assert any(
        w.startswith("Icons set in the entity registry are missing")
        for w in resp["warnings"]
    )


async def test_unreadable_states_only_cost_who_is_in_each_zone(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither the component's states read nor Core's REST states answer."""
    assert await get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], "ha_mcp_tools/states")

    with patch.object(
        client,
        "get_states",
        AsyncMock(side_effect=HomeAssistantConnectionError("states down")),
    ):
        resp = await ZoneTools(client).ha_get_zone()

    assert set(_by_id(resp)) == {"work", "school", "home"}
    assert any("persons" in w and "states down" in w for w in resp["warnings"])
