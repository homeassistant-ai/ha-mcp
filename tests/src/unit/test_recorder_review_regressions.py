"""Read enrichment must preserve data and native parameter-error semantics."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import core_contract, response_helpers, tools_energy, tools_history
from ha_mcp.tools.statistics_helpers import resolve_requested_units
from ha_mcp.tools.statistics_resets import restore_reset_timestamps


@pytest.mark.asyncio
async def test_unrequested_unit_class_keeps_native_display_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.tools import component_api

    monkeypatch.setattr(
        component_api, "get_component_caps", AsyncMock(return_value=None)
    )
    water = {
        "entity_id": "sensor.water",
        "unit_of_measurement": "m³",
        "unit_source": "recorder_metadata",
        "statistics_metadata": {"unit_class": "volume"},
    }
    energy = {
        "entity_id": "sensor.energy",
        "unit_of_measurement": "kWh",
        "unit_source": "recorder_metadata",
        "statistics_metadata": {"unit_class": "energy"},
    }
    await resolve_requested_units(Mock(), [water, energy], {"energy": "MWh"})
    assert water["unit_of_measurement"] == "m³"
    assert water["unit_source"] == "recorder_metadata"
    assert energy["unit_source"] == "unknown"


@pytest.mark.asyncio
async def test_unrequested_unchanged_class_needs_no_reset_query() -> None:
    rows = {"sensor.water": [{"start": 1000, "last_reset": 900, "sum": 123}]}
    metadata = {
        "sensor.water": {
            "unit_class": "volume",
            "statistics_unit_of_measurement": "m³",
            "display_unit_of_measurement": "m³",
        }
    }
    client = Mock(send_websocket_message=AsyncMock())
    warnings = await restore_reset_timestamps(
        client, rows, metadata, {"units": {"energy": "MWh"}}
    )
    assert warnings == []
    assert rows["sensor.water"][0]["last_reset"] == 900
    client.send_websocket_message.assert_not_called()


@pytest.mark.asyncio
async def test_native_parameter_rejection_has_parameter_guidance() -> None:
    client = Mock(
        send_websocket_message=AsyncMock(
            side_effect=[
                {"success": True, "result": []},
                {
                    "success": False,
                    "error": "invalid native units.energy",
                    "error_code": "invalid_format",
                },
            ]
        )
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ToolError) as raised:
        await tools_history._fetch_statistics(
            client,
            ["sensor.energy"],
            start,
            start + timedelta(hours=1),
            "hour",
            ["sum"],
            10,
            0,
            {"units": {"energy": "kwh"}},
        )
    assert "VALIDATION_INVALID_PARAMETER" in str(raised.value)
    assert "units.energy" in str(raised.value)
    assert "state_class" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["history", "energy"])
@pytest.mark.parametrize("failure", [False, True])
async def test_optional_schema_preserves_reads_and_promotes_warnings(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    failure: bool,
) -> None:
    contract = {"status": "available", "warnings": ["Description is incomplete"]}
    lookup = AsyncMock(
        side_effect=ToolError("schema discovery timed out") if failure else None,
        return_value=contract,
    )
    monkeypatch.setattr(core_contract, "core_contract", lookup)
    client = Mock()
    if kind == "energy":
        tool = tools_energy.EnergyTools(client)
        monkeypatch.setattr(
            tool,
            "_get_prefs",
            AsyncMock(
                return_value={
                    "config": {"energy_sources": []},
                    "config_hash": "unchanged",
                }
            ),
        )
        result = await tool.ha_manage_energy_prefs(mode="get", include_schema=True)
        assert result["config"] == {"energy_sources": []}
        assert result["config_hash"] == "unchanged"
        payload = result
    else:
        monkeypatch.setattr(
            tools_history,
            "_fetch_history",
            AsyncMock(
                return_value={
                    "entities": [
                        {"entity_id": "sensor.energy", "states": [{"state": "5"}]}
                    ],
                }
            ),
        )
        monkeypatch.setattr(
            response_helpers,
            "fetch_ha_timezone",
            AsyncMock(return_value=("UTC", True)),
        )
        result = await tools_history.HistoryTools(client).ha_get_history(
            entity_ids=["sensor.energy"],
            start_time="1h",
            include_schema=True,
            fields=["entities"],
        )
        payload = result["data"]
        assert payload["entities"][0]["states"] == [{"state": "5"}]
        assert "warnings" not in payload
        assert any(
            "Could not fetch Home Assistant timezone" in w for w in result["warnings"]
        )
    assert any(
        "schema" in w.lower() or "incomplete" in w.lower() for w in result["warnings"]
    )
    assert result["warnings"]
    assert all(isinstance(warning, str) for warning in result["warnings"])
    assert "warnings" not in payload["core_contract"]
    assert payload["core_contract"]["status"] == (
        "unavailable" if failure else "available"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unit_class,stored,output,needs_query",
    [
        (None, "USD", "USD", False),
        ("energy", "kWh", "MWh", True),
    ],
)
async def test_native_converter_controls_nullable_class_reset_recovery(
    unit_class: str | None,
    stored: str,
    output: str,
    needs_query: bool,
) -> None:
    rows = {"sensor.test": [{"start": 1000, "last_reset": 900, "sum": 123}]}
    metadata = {
        "sensor.test": {
            "unit_class": None,
            "statistics_unit_of_measurement": stored,
            "display_unit_of_measurement": stored,
        }
    }
    resolved = {
        "sensor.test": {
            **metadata["sensor.test"],
            "conversion_unit_class": unit_class,
            "output_unit_of_measurement": output,
        }
    }
    client = Mock(
        send_websocket_message=AsyncMock(
            return_value={
                "success": True,
                "result": {"sensor.test": [{"start": 1000, "last_reset": 800}]},
            }
        )
    )
    assert (
        await restore_reset_timestamps(
            client,
            rows,
            metadata,
            {"units": {"energy": "MWh"}},
            resolved,
        )
        == []
    )
    assert rows["sensor.test"][0]["last_reset"] == (800 if needs_query else 900)
    if needs_query:
        assert client.send_websocket_message.call_args.args[0]["units"] == {
            "energy": "kWh"
        }
    else:
        client.send_websocket_message.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_converter_can_recover_resets_in_unchanged_default_units() -> (
    None
):
    rows = {"sensor.cost": [{"start": 1000, "last_reset": 900, "sum": 123}]}
    metadata = {
        "sensor.cost": {
            "unit_class": None,
            "statistics_unit_of_measurement": "USD",
            "display_unit_of_measurement": "USD",
        }
    }
    client = Mock(
        send_websocket_message=AsyncMock(
            return_value={
                "success": True,
                "result": {"sensor.cost": [{"start": 1000, "last_reset": 800}]},
            }
        )
    )
    assert (
        await restore_reset_timestamps(
            client,
            rows,
            metadata,
            {"units": {"energy": "MWh"}},
        )
        == []
    )
    assert rows["sensor.cost"][0]["last_reset"] == 800
    assert client.send_websocket_message.call_args.args[0]["units"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("component", [True, False])
async def test_default_conversion_recovers_null_class_reset_without_losing_unit(
    monkeypatch: pytest.MonkeyPatch, component: bool
) -> None:
    from ha_mcp.client import websocket_client
    from ha_mcp.tools import component_api

    start = datetime(2026, 1, 1, tzinfo=UTC)
    metadata = {
        "statistic_id": "sensor.energy",
        "unit_class": None,
        "statistics_unit_of_measurement": "kWh",
        "display_unit_of_measurement": "Wh",
    }
    ws = Mock(
        send_command=AsyncMock(
            return_value={
                "result": {
                    "records": [
                        {
                            **metadata,
                            "conversion_unit_class": "energy",
                            "output_unit_of_measurement": "Wh",
                        }
                    ]
                }
            }
        )
    )
    monkeypatch.setattr(
        component_api,
        "get_component_caps",
        AsyncMock(
            return_value=(
                component_api.ComponentCaps(1, "test", frozenset({"core_contract"}), {})
                if component
                else None
            )
        ),
    )
    monkeypatch.setattr(
        websocket_client, "get_websocket_client", AsyncMock(return_value=ws)
    )
    client = Mock(
        base_url="http://test",
        token="test",
        verify_ssl=True,
        send_websocket_message=AsyncMock(
            side_effect=[
                {"success": True, "result": [metadata]},
                {
                    "success": True,
                    "result": {
                        "sensor.energy": [
                            {"start": 1000, "sum": 1000, "last_reset": 900000}
                        ]
                    },
                },
                {
                    "success": True,
                    "result": {"sensor.energy": [{"start": 1000, "last_reset": 900}]},
                },
            ]
        ),
    )
    result = await tools_history._fetch_statistics(
        client,
        ["sensor.energy"],
        start,
        start + timedelta(hours=1),
        "hour",
        ["sum", "last_reset"],
        10,
        0,
    )
    entity = result["entities"][0]
    assert entity["unit_of_measurement"] == "Wh"
    if component:
        assert entity["statistics"][0]["last_reset"] == 900
        assert client.send_websocket_message.call_args.args[0]["units"] == {
            "energy": "kWh"
        }
    else:
        assert "last_reset" not in entity["statistics"][0]
        assert result["warnings"]
        ws.send_command.assert_not_called()
