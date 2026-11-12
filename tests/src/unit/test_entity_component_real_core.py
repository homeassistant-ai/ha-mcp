"""ha_get_entity over the ha_mcp_tools component, on a real ``hass``.

The component's ``entity_enrich`` and ``exposure`` commands answer here from
the same ``hass`` as Core's own registry commands. A failure of an advertised
command is one Core function the component calls raising Core's exception.
No answer to it is staged through Core's own WebSocket API: the command is
registered again with a handler that takes it and never replies, so the
client's command timeout ends the wait, or with one that revokes the caller's
token, so Core closes the connection.
"""

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.components import websocket_api as core_websocket_api
from homeassistant.components.homeassistant import exposed_entities as core_exposed
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import COMPONENT_INSTALL_HINT, ENTITY_ENRICH_FULL_NAME
from ha_mcp.tools.entity_exposure import WS_EXPOSURE
from ha_mcp.tools.tools_entities import WS_ENTITY_ENRICH, EntityTools

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


@callback
def _never_answers(
    hass: HomeAssistant,
    connection: core_websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Takes the command and sends nothing back."""


@callback
def _revokes_the_token(
    hass: HomeAssistant,
    connection: core_websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Revokes the caller's token, so Core closes the connection mid-command."""
    assert connection.refresh_token_id is not None
    token = hass.auth.async_get_refresh_token(connection.refresh_token_id)
    assert token is not None
    hass.auth.async_remove_refresh_token(token)


def _served_by(
    hass: HomeAssistant,
    command: str,
    handler: core_websocket_api.WebSocketCommandHandler,
) -> None:
    """Core serves ``command`` with ``handler`` in place of the component's."""
    core_websocket_api.async_register_command(
        hass, command, handler, vol.Schema({}, extra=vol.ALLOW_EXTRA)
    )


async def test_a_row_carries_the_name_home_assistant_shows(
    tools: EntityTools, power: str
) -> None:
    single = await tools.ha_get_entity(entity_id=power)
    bulk = await tools.ha_get_entity(entity_id=[power, power])

    assert single["entity_entry"]["original_name"] == "Power"
    assert single["entity_entry"]["full_name"] == "Power strip Power"
    assert [e["full_name"] for e in bulk["entity_entries"]] == ["Power strip Power"] * 2
    assert "warnings" not in single
    assert "warnings" not in bulk


async def test_a_component_that_predates_full_name_says_so(
    monkeypatch: pytest.MonkeyPatch, tools: EntityTools, power: str
) -> None:
    monkeypatch.setattr(
        system,
        "CAPABILITIES",
        [cap for cap in system.CAPABILITIES if cap != ENTITY_ENRICH_FULL_NAME],
    )

    single = await tools.ha_get_entity(entity_id=power)
    bulk = await tools.ha_get_entity(entity_id=[power])

    for entry, warnings in (
        (single["entity_entry"], single["warnings"]),
        (bulk["entity_entries"][0], bulk["warnings"]),
    ):
        # The rest of the enrichment is still served.
        assert entry["label_names"] == []
        assert "full_name" not in entry
        assert warnings == [
            "The ha_mcp_tools component predates full_name, so the entries come "
            "without it. " + COMPONENT_INSTALL_HINT
        ]


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


async def test_an_enrich_command_timeout_keeps_cores_rows_and_warns(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tools: EntityTools, power: str
) -> None:
    # The wait bounds every command of the call; an in-process Core answers
    # the others in milliseconds.
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 1)
    _served_by(hass, WS_ENTITY_ENRICH, _never_answers)

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
    _served_by(hass, WS_EXPOSURE, _never_answers)

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
    _served_by(hass, command, _revokes_the_token)

    with pytest.raises(ToolError) as exc_info:
        if command == WS_ENTITY_ENRICH:
            await tools.ha_get_entity(entity_id=power)
        else:
            await tools.ha_get_entity()

    assert _error(exc_info)["code"] == "CONNECTION_FAILED"
