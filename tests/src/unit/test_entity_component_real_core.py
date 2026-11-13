"""ha_get_entity over the ha_mcp_tools component, on a real ``hass``.

The component's ``entity_enrich`` and ``exposure`` commands answer here from
the same ``hass`` as Core's own registry commands. A failure of an advertised
command is one Core function the component calls raising Core's exception.
No answer to it is staged through Core's own WebSocket API
(``_core_staging``).
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
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import (
    COMPONENT_INSTALL_HINT,
    COMPONENT_VIA_HACS,
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    ENTITY_ENRICH_FULL_NAME,
)
from ha_mcp.tools.entity_exposure import WS_EXPOSURE
from ha_mcp.tools.tools_entities import WS_ENTITY_ENRICH, EntityTools

from ._core_staging import never_answers, revokes_the_token, served_by
from ._tool_errors import _error

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
    """The row and the warnings of each way to ask for one entity by id."""
    single = await tools.ha_get_entity(entity_id=entity_id)
    bulk = await tools.ha_get_entity(entity_id=[entity_id])
    unique = await tools.ha_get_entity(unique_id="power")
    return [
        (single["entity_entry"], single.get("warnings")),
        (bulk["entity_entries"][0], bulk.get("warnings")),
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


async def test_a_failing_enrich_command_keeps_cores_rows_and_warns(
    monkeypatch: pytest.MonkeyPatch, tools: EntityTools, power: str
) -> None:
    def unreadable(*_: Any) -> str:
        raise HomeAssistantError("Name unreadable")

    monkeypatch.setattr(er, "async_get_full_entity_name", unreadable)

    for entry, warnings in await _rows(tools, power):
        assert entry["entity_id"] == power
        assert "area" not in entry
        assert "full_name" not in entry
        assert warnings == [
            "ha_mcp_tools/entity_enrich failed: Name unreadable; the entries "
            "come without area, floor, label_names and full_name. "
            + COMPONENT_INSTALL_HINT
        ]


async def test_a_listing_the_component_serves_names_its_entities_without_warning(
    hass: HomeAssistant, tools: EntityTools, power: str
) -> None:
    core_exposed.async_expose_entity(hass, "conversation", power, True)

    result = await tools.ha_get_entity()

    assert set(result["entity_info"]) == {power}
    assert "warnings" not in result


@pytest.mark.parametrize("call", [{}, {"exposed_to": "conversation"}])
async def test_without_a_component_the_listing_says_to_install_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    power: str,
    call: dict[str, str],
) -> None:
    core_exposed.async_expose_entity(hass, "conversation", power, True)

    result = await EntityTools(core_client).ha_get_entity(**call)

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


async def test_an_enrich_command_timeout_keeps_cores_rows_and_warns(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools, power: str
) -> None:
    # The wait bounds every command of the call; an in-process Core answers
    # the others in milliseconds.
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 1)
    served_by(hass, WS_ENTITY_ENRICH, never_answers)

    result = await tools.ha_get_entity(entity_id=power)

    assert result["entity_entry"]["entity_id"] == power
    assert result["warnings"] == [
        "ha_mcp_tools/entity_enrich failed: Command timeout; the entries come without "
        "area, floor, label_names and full_name. " + COMPONENT_INSTALL_HINT
    ]


async def test_an_exposure_command_timeout_is_the_tools_error(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools
) -> None:
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 1)
    served_by(hass, WS_EXPOSURE, never_answers)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_get_entity()

    error = _error(exc_info)
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert error["message"] == "ha_mcp_tools/exposure failed: Command timeout"


# Core closes the socket of the revoked token; the client's close reply then
# re-arms aiohttp's heartbeat timer on that closed server socket, which nothing
# cancels (as in test_remove_entity_error_code.py).
@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.parametrize("command", [WS_ENTITY_ENRICH, WS_EXPOSURE])
async def test_a_lost_connection_during_a_component_read_ends_the_call(
    hass: HomeAssistant, tools: EntityTools, power: str, command: str
) -> None:
    """A dropped connection is the call's CONNECTION_FAILED, not a component
    failure: no warning over Core's rows, no SERVICE_CALL_FAILED naming the
    component."""
    served_by(hass, command, revokes_the_token)

    with pytest.raises(ToolError) as exc_info:
        if command == WS_ENTITY_ENRICH:
            await tools.ha_get_entity(entity_id=power)
        else:
            await tools.ha_get_entity()

    assert _error(exc_info)["code"] == "CONNECTION_FAILED"
