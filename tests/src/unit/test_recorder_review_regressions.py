"""Read enrichment must preserve data and native parameter-error semantics.

Statistics are imported into Core's recorder and read through
``ha_get_history(source="statistics")``; unit resolution comes from the
component's ``ha_mcp_tools/statistics_units`` where it is installed, and every
row, rejection and contract is Core's.
"""

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import core_contract as component_contract
from custom_components.ha_mcp_tools import websocket_api as component_ws
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import websocket_manager
from ha_mcp.tools.tools_energy import EnergyTools
from ha_mcp.tools.tools_history import HistoryTools

from ._client_spies import routing_spies, sent_messages
from ._recorded_history import import_statistic, record_states, statistics

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 1, 2, tzinfo=UTC)
LAST_RESET = datetime(2023, 12, 31, tzinfo=UTC)
STATISTICS = "recorder/statistics_during_period"
STATISTICS_UNITS = "ha_mcp_tools/statistics_units"


async def _import(
    hass: HomeAssistant,
    statistic_id: str,
    stored: str,
    unit_class: str | None,
    display: str | None = None,
) -> None:
    """One hour of a sum statistic with a reset, stored in ``stored``; its
    state shows ``display`` (``stored`` by default)."""
    hass.states.async_set(statistic_id, "1", {"unit_of_measurement": display or stored})
    await import_statistic(
        hass,
        statistic_id,
        stored,
        unit_class,
        [{"start": START, "last_reset": LAST_RESET, "sum": 1000.0}],
    )


async def _core_last_reset(client: HomeAssistantClient, statistic_id: str) -> Any:
    """The reset as Core stores it: read without any unit conversion."""
    reply = await client.send_websocket_message(
        {
            "type": STATISTICS,
            "start_time": START.isoformat(),
            "end_time": END.isoformat(),
            "statistic_ids": [statistic_id],
            "period": "hour",
            "types": ["last_reset"],
            "units": {},
        }
    )
    assert reply["success"] is True
    return reply["result"][statistic_id][0]["last_reset"]


def _reset_queries(sent: AsyncMock) -> list[dict[str, Any]]:
    return [
        call.args[0]
        for call in sent.await_args_list
        if call.args[0]["type"] == STATISTICS
        and call.args[0].get("types") == ["last_reset"]
    ]


def _entities(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entity["entity_id"]: entity for entity in result["data"]["entities"]}


async def test_unrequested_unit_class_keeps_native_display_unit(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without the component a requested unit cannot be confirmed, but only the
    requested class loses its label."""
    await _import(hass, "sensor.water", "m³", "volume")
    await _import(hass, "sensor.energy", "kWh", "energy")

    result = await statistics(
        core_client,
        ["sensor.water", "sensor.energy"],
        START,
        END,
        statistic_types=["sum"],
        core_options={"units": {"energy": "MWh"}},
    )

    entities = _entities(result)
    assert entities["sensor.water"]["unit_of_measurement"] == "m³"
    assert entities["sensor.water"]["unit_source"] == "recorder_metadata"
    assert entities["sensor.energy"]["unit_source"] == "unknown"


async def test_unrequested_unchanged_class_needs_no_reset_query(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _import(hass, "sensor.water", "m³", "volume")
    stored_reset = await _core_last_reset(core_client, "sensor.water")

    with sent_messages(core_client) as sent:
        result = await statistics(
            core_client,
            ["sensor.water"],
            START,
            END,
            statistic_types=["sum", "last_reset"],
            core_options={"units": {"energy": "MWh"}},
        )

    assert "warnings" not in result
    row = _entities(result)["sensor.water"]["statistics"][0]
    assert row["last_reset"] == stored_reset
    assert _reset_queries(sent) == []


async def test_native_parameter_rejection_has_parameter_guidance(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core refuses the lowercase unit; the error is the caller's parameter,
    not a statistic missing its state_class."""
    await _import(hass, "sensor.energy", "kWh", "energy")

    with pytest.raises(ToolError) as raised:
        await statistics(
            core_client,
            ["sensor.energy"],
            START,
            END,
            statistic_types=["sum"],
            core_options={"units": {"energy": "kwh"}},
        )

    payload = json.loads(str(raised.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "units.energy" in payload["error"]["message"]
    assert "state_class" not in str(raised.value)


Read = tuple[dict[str, Any], dict[str, Any]]


async def _energy_get(hass: HomeAssistant, client: HomeAssistantClient) -> Read:
    """The saved energy preferences with their contract: (response, payload)."""
    assert await async_setup_component(hass, "energy", {})
    saved = await client.send_websocket_message(
        {"type": "energy/save_prefs", "energy_sources": []}
    )
    assert saved["success"] is True
    prefs = (await client.send_websocket_message({"type": "energy/get_prefs"}))[
        "result"
    ]
    result = await EnergyTools(client).ha_manage_energy_prefs(
        mode="get", include_schema=True
    )
    assert result["config"] == prefs
    return result, result


async def _history_get(hass: HomeAssistant, client: HomeAssistantClient) -> Read:
    """One recorded state with the history contract: (response, data)."""
    assert await async_setup_component(hass, "history", {})
    await record_states(hass, "sensor.energy", ("5", None, START.timestamp() + 60))
    result = await HistoryTools(client).ha_get_history(
        entity_ids=["sensor.energy"],
        start_time=START.isoformat(),
        end_time=END.isoformat(),
        include_schema=True,
        fields=["entities"],
    )
    payload = result["data"]
    assert [row["state"] for row in payload["entities"][0]["states"]] == ["5"]
    assert "warnings" not in payload
    return result, payload


@pytest.mark.parametrize("kind", ["history", "energy"])
@pytest.mark.parametrize("failure", [False, True])
async def test_optional_schema_preserves_reads_and_promotes_warnings(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    kind: str,
    failure: bool,
) -> None:
    """The read is kept whether the component describes Core's contract or
    fails to; the contract's warnings move to the top-level list."""
    # The component fails while describing Core's contract and answers an error.
    fails = patch.object(
        component_contract,
        "describe_contract",
        side_effect=HomeAssistantError("contract unavailable"),
    )
    read = _energy_get if kind == "energy" else _history_get
    with fails if failure else nullcontext():
        result, payload = await read(hass, component_core_client)

    contract = payload["core_contract"]
    assert "warnings" not in contract
    assert contract["status"] == ("unavailable" if failure else "available")
    assert all(isinstance(warning, str) for warning in result.get("warnings", []))
    if failure:
        assert contract["reason"] in result["warnings"]
    elif contract["description_complete"] is False:
        # Core's energy/save_prefs schema holds validators the component cannot
        # describe; the component says so in the contract's warnings.
        assert any("cannot be described" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    ("stored", "unit_class", "needs_query"),
    [("USD", None, False), ("kWh", None, True)],
    ids=["no-converter", "converter-found-by-unit"],
)
async def test_native_converter_controls_nullable_class_reset_recovery(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    stored: str,
    unit_class: str | None,
    needs_query: bool,
) -> None:
    """A statistic recorded without a unit class: Core's converter, which the
    component reports, decides whether energy units apply and so whether the
    reset must be re-read in the stored unit."""
    await _import(hass, "sensor.test", stored, unit_class)
    stored_reset = await _core_last_reset(component_core_client, "sensor.test")

    with sent_messages(component_core_client) as sent:
        result = await statistics(
            component_core_client,
            ["sensor.test"],
            START,
            END,
            statistic_types=["sum", "last_reset"],
            core_options={"units": {"energy": "MWh"}},
        )

    assert "warnings" not in result
    assert _entities(result)["sensor.test"]["statistics"][0]["last_reset"] == (
        stored_reset
    )
    queries = _reset_queries(sent)
    if needs_query:
        assert [query["units"] for query in queries] == [{"energy": "kWh"}]
    else:
        assert queries == []


async def test_unknown_converter_can_recover_resets_in_unchanged_default_units(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without the component Core's default output is the stored unit, so the
    reset is re-read with no unit option at all."""
    await _import(hass, "sensor.cost", "USD", None)
    stored_reset = await _core_last_reset(core_client, "sensor.cost")

    with sent_messages(core_client) as sent:
        result = await statistics(
            core_client,
            ["sensor.cost"],
            START,
            END,
            statistic_types=["sum", "last_reset"],
            core_options={"units": {"energy": "MWh"}},
        )

    assert not any("last_reset" in warning for warning in result["warnings"])
    row = _entities(result)["sensor.cost"]["statistics"][0]
    assert row["last_reset"] == stored_reset
    assert [query["units"] for query in _reset_queries(sent)] == [{}]


@pytest.mark.parametrize("component", [True, False])
async def test_default_conversion_recovers_null_class_reset_without_losing_unit(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    component: bool,
) -> None:
    """Stored in kWh without a unit class and shown in Wh: Core converts to the
    display unit. The component names the converter, so the reset is re-read in
    kWh; without it the reset cannot be trusted and is left out."""
    if component:
        component_ws.async_register_commands(hass)
    await _import(hass, "sensor.energy", "kWh", None, display="Wh")
    stored_reset = await _core_last_reset(core_client, "sensor.energy")
    socket = await websocket_manager.get_client(
        url=core_client.base_url, token=core_client.token, verify_ssl=False
    )

    with routing_spies(core_client, socket) as (legacy, pooled):
        result = await statistics(
            core_client,
            ["sensor.energy"],
            START,
            END,
            statistic_types=["sum", "last_reset"],
        )

    entity = _entities(result)["sensor.energy"]
    assert entity["unit_of_measurement"] == "Wh"
    resolved = [
        call for call in pooled.call_args_list if call.args[0] == STATISTICS_UNITS
    ]
    if component:
        assert entity["statistics"][0]["last_reset"] == stored_reset
        assert [query["units"] for query in _reset_queries(legacy)] == [
            {"energy": "kWh"}
        ]
        assert len(resolved) == 1
    else:
        assert "last_reset" not in entity["statistics"][0]
        assert result["warnings"]
        assert resolved == []
