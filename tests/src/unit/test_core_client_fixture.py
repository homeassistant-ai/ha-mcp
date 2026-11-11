"""``core_client``: the server's Home Assistant client against a real ``hass``.

A tool test that mocks the client types in what Core "answered" and asserts
against that guess. These tests pin the fixture that replaces the mock: the
client the server ships, talking REST and WebSocket to Core's own API.
"""

from __future__ import annotations

import uuid

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient
from ha_mcp.tools.tools_zones import ZoneTools


async def test_rest_read_returns_the_state_core_holds(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A REST read through the client is Core's state, not a typed-in dict."""
    hass.states.async_set("light.kitchen", "on", {"brightness": 128})

    state = await core_client.get_entity_state("light.kitchen")

    assert state["state"] == "on"
    assert state["attributes"]["brightness"] == 128


async def test_websocket_command_is_answered_by_core(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
) -> None:
    """A WebSocket command through the client gets Core's registry row."""
    assert await async_setup_component(hass, "config", {})
    entry = entity_registry.async_get_or_create("light", "demo", "uid-1")

    reply = await core_client.send_websocket_message(
        {"type": "config/entity_registry/get", "entity_id": entry.entity_id}
    )

    assert reply["success"] is True
    assert reply["result"]["unique_id"] == "uid-1"


async def test_cores_refusal_surfaces_as_the_clients_error(
    core_client: HomeAssistantClient,
) -> None:
    """Core's own 404 reaches the caller, so no failure reply is typed in."""
    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await core_client.get_entity_state("light.missing")

    assert exc_info.value.status_code == 404


async def test_component_commands_answer_on_the_component_fixture(
    component_core_client: HomeAssistantClient,
) -> None:
    """The component variant serves ``ha_mcp_tools/*`` from the real handlers."""
    reply = await component_core_client.send_websocket_message(
        {"type": "ha_mcp_tools/info"}
    )

    assert reply["success"] is True


async def test_plain_fixture_is_an_install_without_the_component(
    core_client: HomeAssistantClient,
) -> None:
    """Without the component Core rejects its commands, as on a real install."""
    reply = await core_client.send_websocket_message({"type": "ha_mcp_tools/info"})

    assert reply["success"] is False
    assert reply["error_code"] == "unknown_command"


async def test_a_tool_runs_on_the_fixture_end_to_end(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A real tool lists a zone Core stored, with no mocked reply in between."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "zone", {})
    # Core restores a deleted zone's registry entry by name (test design rule 6).
    name = f"Office {uuid.uuid4().hex[:8]}"
    created = await core_client.send_websocket_message(
        {
            "type": "zone/create",
            "name": name,
            "latitude": 52.5,
            "longitude": 13.4,
            "radius": 75,
        }
    )
    assert created["success"] is True

    result = await ZoneTools(core_client).ha_get_zone()

    office = next(zone for zone in result["zones"] if zone["name"] == name)
    assert office["latitude"] == 52.5
    assert office["radius"] == 75
