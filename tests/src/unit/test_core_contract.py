"""Core contracts follow the validator Core registered, including future fields.

The commands are Core's own (``energy``, ``history``, the recorder), set up on
a real ``hass``; the component's ``ha_mcp_tools/core_contract`` and its
siblings run through Core's WebSocket API, and the server reads them with its
own client.
"""

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.components import websocket_api as core_websocket_api
from homeassistant.components.energy.data import EnergyManager
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import MockHAClientWebSocket

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import energy_preferences
from ha_mcp.tools.core_contract import core_contract, validate_energy_proposal

from ._component_core import ws_call, ws_result
from ._core_client import refused_url

ENERGY = "energy/save_prefs"
CONTRACT = "ha_mcp_tools/core_contract"


async def _no_prefs_saved(ws: MockHAClientWebSocket) -> bool:
    reply = await ws_call(ws, "energy/get_prefs")
    return reply["success"] is False and reply["error"]["code"] == "not_found"


async def test_validation_uses_the_registered_schema_and_never_saves(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
) -> None:
    """A field a newer Core requires is enforced from Core's own validator,
    and validating never runs the save handler."""
    assert await async_setup_component(hass, "energy", {})
    handler, schema = hass.data[core_websocket_api.DOMAIN][ENERGY]
    core_websocket_api.async_register_command(
        hass, ENERGY, handler, schema.extend({vol.Required("future_required"): list})
    )

    invalid = await ws_result(component_ws, CONTRACT, command=ENERGY, payload={})
    valid = await ws_result(
        component_ws, CONTRACT, command=ENERGY, payload={"future_required": []}
    )

    assert invalid["valid"] is False
    assert "future_required" in str(invalid["errors"])
    assert valid["valid"] is True
    assert await _no_prefs_saved(component_ws)


@pytest.mark.parametrize("payload", [{"id": 27}, {"type": "call_service"}])
async def test_a_payload_cannot_override_the_transport_fields(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    caplog: pytest.LogCaptureFixture,
    payload: dict[str, Any],
) -> None:
    assert await async_setup_component(hass, "energy", {})

    reply = await ws_call(component_ws, CONTRACT, command=ENERGY, payload=payload)

    assert reply["success"] is False
    assert "id and type are reserved" in caplog.text


async def test_an_unregistered_command_is_unavailable_not_valid(
    component_ws: MockHAClientWebSocket,
) -> None:
    result = await ws_result(component_ws, CONTRACT, command=ENERGY, payload={})

    assert result["status"] == "unavailable"


async def test_the_bridge_refuses_commands_outside_its_list(
    component_ws: MockHAClientWebSocket,
) -> None:
    reply = await ws_call(component_ws, CONTRACT, command="call_service", payload={})

    assert reply["success"] is False
    assert reply["error"]["code"] == "invalid_format"


async def test_failed_capability_discovery_is_an_error_not_absence(
    hass_access_token: str, socket_enabled: None
) -> None:
    """A component that could not be asked is not a component that is absent."""
    client = HomeAssistantClient(
        base_url=refused_url(), token=hass_access_token, verify_ssl=False
    )
    try:
        with pytest.raises(ToolError, match="Could not obtain"):
            await core_contract(client, ENERGY, {})
    finally:
        await client.close()


async def test_a_proposal_core_rejects_is_a_tool_error(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
) -> None:
    assert await async_setup_component(hass, "energy", {})

    with pytest.raises(ToolError, match="energy_sources"):
        await validate_energy_proposal(component_core_client, {"energy_sources": "x"})


async def test_a_proposal_core_accepts_passes(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
) -> None:
    assert await async_setup_component(hass, "energy", {})

    result = await validate_energy_proposal(component_core_client, {})

    assert (result["status"], result["valid"]) == ("validated", True)


@pytest.mark.parametrize("defaults_fail", [False, True])
async def test_missing_core_defaults_do_not_hide_the_schema(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    defaults_fail: bool,
) -> None:
    """Core's defaults are an extra; a Core without them still describes."""
    assert await async_setup_component(hass, "energy", {})

    with ExitStack() as stack:
        if defaults_fail:
            stack.enter_context(
                patch.object(
                    EnergyManager,
                    "default_preferences",
                    side_effect=AttributeError("Core changed"),
                )
            )
        result = await ws_result(component_ws, CONTRACT, command=ENERGY)

    assert result["status"] == "available"
    assert ("default_preferences" in result) is not defaults_fail


async def test_unconfigured_prefs_stay_readable_when_defaults_discovery_fails(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core answers "No prefs" for an energy dashboard never configured."""
    assert await async_setup_component(hass, "energy", {})
    monkeypatch.setattr(
        energy_preferences,
        "core_contract",
        AsyncMock(side_effect=ToolError("discovery unavailable")),
    )

    result = await energy_preferences.get_energy_prefs(component_core_client)

    assert result["success"] is True
    assert result["config"] == {}
    assert "unavailable" in result["note"]


_CONTRACT_COMMANDS: dict[str, dict[str, Any]] = {
    CONTRACT: {"command": "history/history_during_period"},
    "ha_mcp_tools/statistics_units": {
        "statistic_ids": ["sensor.energy"],
        "units": {"energy": "kWh"},
    },
    "ha_mcp_tools/integration_commands": {"domain": "knx"},
}


@pytest.mark.parametrize("command", list(_CONTRACT_COMMANDS))
@pytest.mark.parametrize("admin", [False, True])
async def test_contract_commands_are_admin_only_before_any_preparation(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    component_ws_read_only: MockHAClientWebSocket,
    command: str,
    admin: bool,
) -> None:
    """A non-admin never reaches the recorder read behind statistics_units."""
    ws = component_ws if admin else component_ws_read_only
    real = recorder_statistics.async_list_statistic_ids

    with patch.object(
        recorder_statistics, "async_list_statistic_ids", wraps=real
    ) as read:
        reply = await ws_call(ws, command, **_CONTRACT_COMMANDS[command])

    assert reply["success"] is admin, reply
    reads_statistics = admin and command.endswith("statistics_units")
    assert read.await_count == (1 if reads_statistics else 0)
    if not admin:
        assert reply["error"]["code"] == "unauthorized"
