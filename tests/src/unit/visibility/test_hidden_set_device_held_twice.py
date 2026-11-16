"""The enforced hidden set reads the device registry from Core, with the
component installed too.

A device the registry holds in two copies that disagree arrives from Core in
both, and a call is refused rather than checked against a set built on one of
them. The
component's device list leaves such a device out: a set read from it would be
built without the area the device gives its entities, and nothing would say so.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.visibility.model import VisibilityConfig

from .._tool_errors import error_of
from ._chained_enforcement import _ChainedEnforcement, _returns, make_context


async def test_a_device_held_twice_refuses_the_call_with_the_component(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    assert await async_setup_component(hass, "config", {})
    kitchen = area_registry.async_create("Kitchen")
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "lamp")}
    )
    in_the_kitchen = device_registry.async_update_device(device.id, area_id=kitchen.id)
    light = entity_registry.async_get_or_create(
        "light", "test", "lamp", suggested_object_id="lamp", device_id=device.id
    )
    hass.states.async_set(light.entity_id, "on")
    set_config(enabled=True, enforce=True, exclude_areas=[kitchen.id])
    moved_out = device_registry.async_update_device(device.id, area_id=None)

    with (
        patch.object(
            dr.ActiveDeviceRegistryItems,
            "values",
            return_value=[in_the_kitchen, moved_out],
        ),
        pytest.raises(ToolError) as refused,
    ):
        await _ChainedEnforcement(lambda: component_core_client).on_call_tool(
            make_context("ha_get_state", {"entity_id": "sun.sun"}), _returns("ok")
        )

    assert "could not be loaded" in error_of(refused)["message"]
