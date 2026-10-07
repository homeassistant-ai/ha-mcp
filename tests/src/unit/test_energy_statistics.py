"""Energy inspection must expose recorder metadata without changing preferences."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_energy import EnergyTools


@pytest.mark.asyncio
async def test_energy_inspection_resolves_all_referenced_statistics_without_writes():
    prefs = {
        "energy_sources": [{"type": "solar", "stat_energy_from": "sensor.solar"}],
        "device_consumption": [{"stat_consumption": "sensor.missing", "included_in_stat": "sensor.solar"}],
        "device_consumption_water": [],
    }
    record = {
        "statistic_id": "sensor.solar", "unit_class": "energy", "has_sum": True,
        "statistics_unit_of_measurement": "MWh", "display_unit_of_measurement": "kWh",
    }

    async def dispatch(message):
        if message["type"] == "energy/get_prefs":
            return {"success": True, "result": prefs}
        if message["type"] == "recorder/get_statistics_metadata":
            assert sorted(message["statistic_ids"]) == ["sensor.missing", "sensor.solar"]
            return {"success": True, "result": [record]}
        raise AssertionError(f"Unexpected write or request: {message}")

    client = MagicMock(send_websocket_message=AsyncMock(side_effect=dispatch))
    tool = EnergyTools(client)
    ordinary = await tool.ha_manage_energy_prefs(mode="get")
    inspected = await tool.ha_manage_energy_prefs(mode="get", include_statistics=True)
    assert inspected["config"] == ordinary["config"] == prefs
    assert inspected["config_hash"] == ordinary["config_hash"]
    records = {row["statistic_id"]: row for row in inspected["statistics_metadata"]}
    assert records["sensor.solar"]["unit_of_measurement"] == "kWh"
    assert records["sensor.solar"]["statistics_unit_of_measurement"] == "MWh"
    assert records["sensor.missing"]["unit_reason"] == "statistics_metadata_missing"
    assert inspected["warnings"]


@pytest.mark.asyncio
async def test_empty_energy_config_does_not_request_all_recorder_metadata():
    client = MagicMock(send_websocket_message=AsyncMock(return_value={
        "success": False, "error": {"code": "not_found", "message": "No prefs"}
    }))
    result = await EnergyTools(client).ha_manage_energy_prefs(mode="get", include_statistics=True)
    assert result["statistics_metadata"] == []
    assert client.send_websocket_message.await_count == 1
