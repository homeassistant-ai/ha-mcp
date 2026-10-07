"""Energy proposals use Core's validator without saving, across both topologies."""

from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import Client

from ..utilities.assertions import MCPAssertions, assert_mcp_success
from ..utilities.topology import component_surface_available


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        {"type": "battery", "stat_energy_from": "sensor.total_energy_kwh"},
        {"type": "grid"},
        {"type": "grid", "cost_adjustment_day": 0},
    ],
)
async def test_incomplete_source_is_never_reported_as_valid(
    mcp_client: Client, source: dict[str, Any]
) -> None:
    before_raw = assert_mcp_success(
        await mcp_client.call_tool(
            "ha_manage_energy_prefs", {"mode": "get", "include_schema": True}
        )
    )
    before = before_raw.get("data", before_raw)
    supported = component_surface_available()
    assert (before["core_contract"]["status"] == "available") is supported
    arguments = {"mode": "set", "config": {"energy_sources": [source]}, "dry_run": True}
    if supported:
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_manage_energy_prefs", arguments, expected_error="VALIDATION_FAILED"
            )
    else:
        raw = assert_mcp_success(
            await mcp_client.call_tool("ha_manage_energy_prefs", arguments)
        )
        data = raw.get("data", raw)
        assert data["proposal_validation"]["status"] == "unavailable"
        assert data["partial"] is True
    after_raw = assert_mcp_success(
        await mcp_client.call_tool("ha_manage_energy_prefs", {"mode": "get"})
    )
    after = after_raw.get("data", after_raw)
    assert before["config"] == after["config"]
    assert before["config_hash"] == after["config_hash"]


@pytest.mark.asyncio
async def test_core_supported_last_reset_is_readable(mcp_client: Client) -> None:
    raw = assert_mcp_success(
        await mcp_client.call_tool(
            "ha_get_history",
            {
                "source": "statistics",
                "entity_ids": ["sensor.total_energy_kwh"],
                "start_time": "7d",
                "period": "hour",
                "statistic_types": ["last_reset"],
            },
        )
    )
    data = raw.get("data", raw)
    rows = data["entities"][0]["statistics"]
    assert rows, "Seeded kWh fixture must have recorder statistics"
    assert all("last_reset" in row for row in rows)


@pytest.mark.asyncio
async def test_explicit_units_change_values_without_using_default_unit_label(
    mcp_client: Client,
) -> None:
    query = {
        "source": "statistics",
        "entity_ids": ["sensor.total_energy_kwh"],
        "start_time": "7d",
        "period": "hour",
        "statistic_types": ["sum"],
        "include_schema": True,
    }
    responses = []
    for unit in ("kWh", "MWh"):
        raw = assert_mcp_success(
            await mcp_client.call_tool(
                "ha_get_history", {**query, "core_options": {"units": {"energy": unit}}}
            )
        )
        responses.append(raw.get("data", raw))
    small, large = (r["entities"][0] for r in responses)
    assert small["statistics"] and large["statistics"]
    for a, b in zip(small["statistics"], large["statistics"], strict=True):
        assert a["sum"] == pytest.approx(b["sum"] * 1000)
    assert (
        responses[0]["core_contract"]["status"] == "available"
    ) is component_surface_available()
    if component_surface_available():
        assert small["unit_of_measurement"] == "kWh"
        assert large["unit_of_measurement"] == "MWh"
    else:
        assert small["unit_source"] == large["unit_source"] == "unknown"


@pytest.mark.asyncio
async def test_energy_schema_exposes_native_battery_fields(mcp_client: Client) -> None:
    raw = assert_mcp_success(
        await mcp_client.call_tool(
            "ha_manage_energy_prefs", {"mode": "get", "include_schema": True}
        )
    )
    result = raw.get("data", raw)
    contract = result["core_contract"]
    if component_surface_available():
        assert contract["status"] == "available"
        # The previous copy omitted this required field. Do not require the
        # entire serialized shape: native serializers can legitimately evolve.
        assert "stat_energy_to" in str(contract)
        assert "cost_adjustment_day" in str(contract)
        assert contract["description_complete"] is False
    else:
        assert contract["status"] == "unavailable"
