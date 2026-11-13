"""ha_get_entity over the ha_mcp_tools component, on a real ``hass``.

The component's ``entity_enrich`` and ``exposure`` commands answer here from
the same ``hass`` as Core's own registry commands.
"""

from typing import Any

import pytest
from homeassistant.components import websocket_api as core_websocket_api
from homeassistant.components.homeassistant import exposed_entities as core_exposed
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import websocket_api
from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import (
    COMPONENT_VIA_HACS,
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    ENTITY_ENRICH_FULL_NAME,
)
from ha_mcp.tools.entity_exposure import WS_EXPOSURE
from ha_mcp.tools.tools_entities import WS_ENTITY_ENRICH, EntityTools

_WITHOUT_ENRICHMENT = "the entries come without area, floor, label_names and full_name"
_WITHOUT_ENTITY_INFO = "the listing comes without entity_info (names and areas)"


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


async def _rows(
    tools: EntityTools, entity_id: str
) -> list[tuple[dict[str, Any], list[str] | None]]:
    """The rows and warnings of each way to ask for one entity by id.

    The bulk call names the id twice; the unique_id is the ``power`` fixture's.
    """
    single = await tools.ha_get_entity(entity_id=entity_id)
    bulk = await tools.ha_get_entity(entity_id=[entity_id, entity_id])
    unique = await tools.ha_get_entity(unique_id="power")
    return [
        (single["entity_entry"], single.get("warnings")),
        *((row, bulk.get("warnings")) for row in bulk["entity_entries"]),
        (unique["entity_entries"][0], unique.get("warnings")),
    ]


async def test_a_row_carries_the_name_home_assistant_shows(
    tools: EntityTools, power: str
) -> None:
    for entry, warnings in await _rows(tools, power):
        assert entry["original_name"] == "Power"
        assert entry["full_name"] == "Power strip Power"
        assert entry["label_names"] == []
        assert warnings is None


async def test_a_component_that_predates_full_name_says_so(
    monkeypatch: pytest.MonkeyPatch, tools: EntityTools, power: str
) -> None:
    monkeypatch.setattr(
        system,
        "CAPABILITIES",
        [cap for cap in system.CAPABILITIES if cap != ENTITY_ENRICH_FULL_NAME],
    )

    for entry, warnings in await _rows(tools, power):
        # The rest of the enrichment is still served.
        assert entry["label_names"] == []
        assert "full_name" not in entry
        assert warnings == [
            "The ha_mcp_tools component predates full_name, so the entries come "
            "without it. Update it " + COMPONENT_VIA_HACS
        ]


async def test_without_a_component_the_rows_say_to_install_it(
    core_client: HomeAssistantClient, power: str
) -> None:
    for entry, warnings in await _rows(EntityTools(core_client), power):
        assert entry["original_name"] == "Power"
        assert "area" not in entry
        assert "full_name" not in entry
        assert warnings == [
            f"No ha_mcp_tools component answered, so {_WITHOUT_ENRICHMENT}. "
            "Install it " + COMPONENT_VIA_HACS
        ]


async def test_a_call_that_finds_no_row_gets_no_warning(
    core_client: HomeAssistantClient,
) -> None:
    result = await EntityTools(core_client).ha_get_entity(entity_id=["sensor.nope"])

    assert result["count"] == 0
    assert "warnings" not in result


@pytest.mark.parametrize("dropped", ["entity_enrich", DEVICE_REGISTRY_CHILD_SEMANTICS])
async def test_a_component_too_old_for_the_enrichment_says_to_update_it(
    monkeypatch: pytest.MonkeyPatch, tools: EntityTools, power: str, dropped: str
) -> None:
    monkeypatch.setattr(
        system,
        "CAPABILITIES",
        [cap for cap in system.CAPABILITIES if cap != dropped],
    )

    for entry, warnings in await _rows(tools, power):
        assert "area" not in entry
        assert warnings == [
            f"The ha_mcp_tools component is too old, so {_WITHOUT_ENRICHMENT}. "
            "Update it " + COMPONENT_VIA_HACS
        ]


async def test_an_advertised_enrich_command_core_does_not_know_says_to_update(
    hass: HomeAssistant, tools: EntityTools, power: str
) -> None:
    # Core answers unknown_command for a command nothing registers.
    hass.data[core_websocket_api.DOMAIN].pop(WS_ENTITY_ENRICH)

    result = await tools.ha_get_entity(entity_id=power)

    assert "area" not in result["entity_entry"]
    assert result["warnings"] == [
        f"The ha_mcp_tools component is too old, so {_WITHOUT_ENRICHMENT}. "
        "Update it " + COMPONENT_VIA_HACS
    ]


async def test_a_unique_id_shared_by_two_platforms_enriches_both_rows(
    hass: HomeAssistant, tools: EntityTools, power: str
) -> None:
    other = (
        er.async_get(hass)
        .async_get_or_create(
            "sensor", "other", "power", has_entity_name=True, original_name="Draw"
        )
        .entity_id
    )

    result = await tools.ha_get_entity(unique_id="power")

    assert result["matches"] == 2
    full_names = {
        row["entity_id"]: row["full_name"] for row in result["entity_entries"]
    }
    assert full_names == {power: "Power strip Power", other: "Draw"}
    assert "warnings" not in result


async def test_an_advertised_exposure_command_core_does_not_know_says_to_update(
    hass: HomeAssistant, tools: EntityTools
) -> None:
    hass.data[core_websocket_api.DOMAIN].pop(WS_EXPOSURE)

    result = await tools.ha_get_entity()

    assert "entity_info" not in result
    assert result["warnings"] == [
        f"The ha_mcp_tools component is too old, so {_WITHOUT_ENTITY_INFO}. "
        "Update it " + COMPONENT_VIA_HACS
    ]


async def test_a_component_that_refuses_the_caller_is_not_called_missing(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    hass_read_only_access_token: str,
    power: str,
) -> None:
    """The component's commands need an admin; Core's registry reads do not."""
    client = HomeAssistantClient(
        base_url=component_core_client.base_url,
        token=hass_read_only_access_token,
        verify_ssl=False,
    )
    try:
        result = await EntityTools(client).ha_get_entity(entity_id=power)
    finally:
        await client.close()

    assert result["entity_entry"]["original_name"] == "Power"
    [warning] = result["warnings"]
    assert warning.startswith("The ha_mcp_tools component could not be asked (")
    assert warning.endswith(f"), so {_WITHOUT_ENRICHMENT}.")


async def test_a_listing_the_component_serves_names_its_entities_without_warning(
    hass: HomeAssistant, tools: EntityTools, power: str
) -> None:
    core_exposed.async_expose_entity(hass, "conversation", power, True)

    result = await tools.ha_get_entity()

    assert set(result["entity_info"]) == {power}
    assert "warnings" not in result


async def test_without_a_component_the_listing_says_to_install_it(
    hass: HomeAssistant, core_client: HomeAssistantClient, power: str
) -> None:
    core_exposed.async_expose_entity(hass, "conversation", power, True)

    result = await EntityTools(core_client).ha_get_entity()

    assert set(result["exposed_entities"]) == {power}
    assert "entity_info" not in result
    assert result["warnings"] == [
        f"No ha_mcp_tools component answered, so {_WITHOUT_ENTITY_INFO}. "
        "Install it " + COMPONENT_VIA_HACS
    ]


@pytest.mark.parametrize("dropped", ["exposure", DEVICE_REGISTRY_CHILD_SEMANTICS])
async def test_a_component_too_old_for_the_listing_says_to_update_it(
    monkeypatch: pytest.MonkeyPatch, tools: EntityTools, dropped: str
) -> None:
    monkeypatch.setattr(
        system,
        "CAPABILITIES",
        [cap for cap in system.CAPABILITIES if cap != dropped],
    )

    result = await tools.ha_get_entity()

    assert "entity_info" not in result
    assert result["warnings"] == [
        f"The ha_mcp_tools component is too old, so {_WITHOUT_ENTITY_INFO}. "
        "Update it " + COMPONENT_VIA_HACS
    ]


async def test_a_listing_whose_component_probe_fails_says_it_could_not_ask(
    monkeypatch: pytest.MonkeyPatch, tools: EntityTools
) -> None:
    def unreadable(*_: Any) -> None:
        raise HomeAssistantError("Info unreadable")

    monkeypatch.setattr(websocket_api, "_do_info", unreadable)

    result = await tools.ha_get_entity()

    assert "entity_info" not in result
    [warning] = result["warnings"]
    assert warning.startswith("The ha_mcp_tools component could not be asked (")
    assert warning.endswith(f"), so {_WITHOUT_ENTITY_INFO}.")
