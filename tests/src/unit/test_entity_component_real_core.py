"""ha_get_entity over the ha_mcp_tools component, on a real ``hass``.

The component's ``entity_enrich`` and ``exposure`` commands answer here from
the same ``hass`` as Core's own registry commands. A failure of an advertised
command is one Core function the component calls raising Core's exception.
"""

from typing import Any

import pytest
from homeassistant.components.homeassistant import exposed_entities as core_exposed
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import COMPONENT_INSTALL_HINT
from ha_mcp.tools.tools_entities import EntityTools

from ._tool_errors import _error


@pytest.fixture(autouse=True)
async def _core(hass: HomeAssistant) -> None:
    for domain in ("config", "homeassistant"):
        assert await async_setup_component(hass, domain, {})


@pytest.fixture
def power(hass: HomeAssistant) -> str:
    """An entity named after its device, as an integration names it."""
    config_entry = MockConfigEntry(domain="demo")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("demo", "strip")},
        name="Power strip",
    )
    return (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            "demo",
            "power",
            device_id=device.id,
            has_entity_name=True,
            original_name="Power",
        )
        .entity_id
    )


@pytest.fixture
def tools(component_core_client: HomeAssistantClient) -> EntityTools:
    return EntityTools(component_core_client)


async def test_a_row_carries_the_name_home_assistant_shows(
    tools: EntityTools, power: str
) -> None:
    single = await tools.ha_get_entity(entity_id=power)
    bulk = await tools.ha_get_entity(entity_id=[power, power])

    assert single["entity_entry"]["original_name"] == "Power"
    assert single["entity_entry"]["full_name"] == "Power strip Power"
    assert [e["full_name"] for e in bulk["entity_entries"]] == ["Power strip Power"] * 2


async def test_a_failing_enrich_command_keeps_cores_rows_and_warns(
    monkeypatch: pytest.MonkeyPatch, tools: EntityTools, power: str
) -> None:
    def unreadable(*_: Any) -> str:
        raise HomeAssistantError("Name unreadable")

    monkeypatch.setattr(er, "async_get_full_entity_name", unreadable)

    single = await tools.ha_get_entity(entity_id=power)
    bulk = await tools.ha_get_entity(entity_id=[power])

    for entry, warnings in (
        (single["entity_entry"], single["warnings"]),
        (bulk["entity_entries"][0], bulk["warnings"]),
    ):
        assert entry["entity_id"] == power
        assert "area" not in entry
        assert "full_name" not in entry
        assert warnings == [
            "ha_mcp_tools/entity_enrich failed: Name unreadable; the entries "
            "come without area, floor, label_names and full_name. "
            + COMPONENT_INSTALL_HINT
        ]


async def test_a_failing_exposure_command_is_the_tools_error(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
) -> None:
    """Core's own list would still answer; the advertised command decides."""

    def unreadable(*_: Any) -> None:
        raise HomeAssistantError("Store unreadable")

    monkeypatch.setattr(core_exposed, "async_get_assistant_settings", unreadable)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_get_entity()

    error = _error(exc_info)
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert error["message"] == "ha_mcp_tools/exposure failed: Store unreadable"
    assert error["suggestion"] == COMPONENT_INSTALL_HINT
