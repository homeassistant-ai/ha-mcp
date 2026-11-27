"""ha_set_entity leaves a check to Core where Core makes it, and asks Core
where it does not: Core 2026.12's refusal of an unknown area, and the units
Core converts a sensor to. Everything runs on a real ``hass``.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools

from ._entity_registry_core import core_client as core_client
from ._entity_registry_core import sent_types
from ._tool_errors import error_body


def _options(registry: er.EntityRegistry, entity_id: str) -> dict[str, Any]:
    entry = registry.async_get(entity_id)
    assert entry is not None
    return dict(entry.options)


@pytest.mark.skipif(
    (MAJOR_VERSION, MINOR_VERSION) < (2026, 12),
    reason="Core checks the area itself since 2026.12",
)
async def test_core_refuses_an_unknown_area_itself(
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    sent: AsyncMock,
) -> None:
    """Core's refusal reaches the caller with its code and the area hints, and
    HA-MCP reads no area registry of its own to check the same thing first."""
    entry = entity_registry.async_get_or_create("light", "demo", "test")

    with pytest.raises(ToolError) as exc_info:
        await EntityTools(core_client).ha_set_entity(
            entity_id=entry.entity_id, area_id="ghost_area"
        )

    body = error_body(exc_info)
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["ha_error_code"] == "invalid_info"
    assert "ghost_area" in body["error"]["message"]
    assert any("area IDs" in s for s in body["error"]["suggestions"])
    assert "config/entity_registry/update" in sent_types(sent)
    assert "config/area_registry/list" not in sent_types(sent)
    stored = entity_registry.async_get(entry.entity_id)
    assert stored is not None and stored.area_id is None


# The sensor converts with the integration's device class (temperature), so
# a device class set in the registry or by the same call changes nothing.
@pytest.mark.parametrize(
    ("own", "call", "unit", "refused"),
    [
        (None, None, "°F", False),
        (None, None, "kWh", True),
        ("energy", None, "kWh", True),
        (None, "energy", "kWh", True),
    ],
    ids=["listed", "not-listed", "registry-class", "call-class"],
)
async def test_a_unit_core_does_not_list_for_the_device_class_is_refused(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    own: str | None,
    call: str | None,
    unit: str,
    refused: bool,
) -> None:
    """The units come from Core's list for the integration's device class."""
    assert await async_setup_component(hass, "sensor", {})
    entry = entity_registry.async_get_or_create(
        "sensor", "demo", "temp", original_device_class="temperature"
    )
    entity_registry.async_update_entity(entry.entity_id, device_class=own)
    options = {"sensor": {"unit_of_measurement": unit}}
    write = EntityTools(core_client).ha_set_entity(
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
    assert body["device_class"] == "temperature"
    assert "sensor" not in _options(entity_registry, entry.entity_id)


async def test_a_unit_core_gave_no_list_for_is_written_with_a_warning(
    core_client: HomeAssistantClient, entity_registry: er.EntityRegistry
) -> None:
    """A unit that was never checked is not reported as a plain success."""
    # No ``sensor`` integration, so Core does not know the units command.
    entry = entity_registry.async_get_or_create(
        "sensor", "demo", "temp", original_device_class="temperature"
    )
    options = {"sensor": {"unit_of_measurement": "kWh"}}

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=entry.entity_id, options=options
    )

    assert any("not checked" in warning for warning in result["warnings"])
    assert _options(entity_registry, entry.entity_id)["sensor"] == options["sensor"]


async def test_a_write_that_sets_no_area_does_not_read_cores_version(
    core_client: HomeAssistantClient, entity_registry: er.EntityRegistry
) -> None:
    """Core's version decides only who checks an area, so a rename asks nothing."""
    entry = entity_registry.async_get_or_create("light", "demo", "test")

    with patch.object(
        core_client, "get_config", wraps=core_client.get_config
    ) as get_config:
        await EntityTools(core_client).ha_set_entity(
            entity_id=entry.entity_id, name="Renamed"
        )

    get_config.assert_not_awaited()
