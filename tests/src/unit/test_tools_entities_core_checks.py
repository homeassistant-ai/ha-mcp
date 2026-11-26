"""ha_set_entity leaves a check to Core where Core makes it, and asks Core
where it does not: Core 2026.12's refusal of an unknown area, and the units
Core converts a sensor to. Everything runs on a real ``hass``.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools

from ._entity_registry_core import sent_types
from ._tool_errors import error_body


@pytest.fixture
async def tools(hass: HomeAssistant, core_client: HomeAssistantClient) -> EntityTools:
    assert await async_setup_component(hass, "config", {})
    return EntityTools(core_client)


def _options(registry: er.EntityRegistry, entity_id: str) -> dict[str, Any]:
    entry = registry.async_get(entity_id)
    assert entry is not None
    return dict(entry.options)


@pytest.mark.skipif(
    (MAJOR_VERSION, MINOR_VERSION) < (2026, 12),
    reason="Core checks the area itself since 2026.12",
)
async def test_core_refuses_an_unknown_area_itself(
    tools: EntityTools, entity_registry: er.EntityRegistry, sent: AsyncMock
) -> None:
    """Core's refusal reaches the caller with its code and the area hints, and
    HA-MCP reads no area registry of its own to check the same thing first."""
    entry = entity_registry.async_get_or_create("light", "demo", "test")

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_set_entity(entity_id=entry.entity_id, area_id="ghost_area")

    body = error_body(exc_info)
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["ha_error_code"] == "invalid_info"
    assert "ghost_area" in body["error"]["message"]
    assert any("area IDs" in s for s in body["error"]["suggestions"])
    assert sent_types(sent) == ["config/entity_registry/update"]
    stored = entity_registry.async_get(entry.entity_id)
    assert stored is not None and stored.area_id is None


@pytest.mark.parametrize(
    ("own", "call", "unit", "refused"),
    [
        (None, None, "°F", False),
        (None, None, "kWh", True),
        ("energy", None, "kWh", False),
        (None, "energy", "kWh", False),
        (None, "energy", "°F", True),
    ],
    ids=["listed", "not-listed", "entry-class", "call-class", "call-class-refused"],
)
async def test_a_unit_core_does_not_list_for_the_device_class_is_refused(
    hass: HomeAssistant,
    tools: EntityTools,
    entity_registry: er.EntityRegistry,
    own: str | None,
    call: str | None,
    unit: str,
    refused: bool,
) -> None:
    """Core keeps a unit it cannot convert to and ignores it; the units come
    from Core's list for the device class the sensor ends up with."""
    assert await async_setup_component(hass, "sensor", {})
    entry = entity_registry.async_get_or_create(
        "sensor", "demo", "temp", original_device_class="temperature"
    )
    entity_registry.async_update_entity(entry.entity_id, device_class=own)
    options = {"sensor": {"unit_of_measurement": unit}}
    write = tools.ha_set_entity(
        entity_id=entry.entity_id, options=options, device_class=call
    )

    if not refused:
        await write
        assert _options(entity_registry, entry.entity_id)["sensor"] == options["sensor"]
        return
    with pytest.raises(ToolError) as exc_info:
        await write

    body = error_body(exc_info)
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["unit_of_measurement"] == unit
    assert body["convertible_units"] and unit not in body["convertible_units"]
    assert "sensor" not in _options(entity_registry, entry.entity_id)
