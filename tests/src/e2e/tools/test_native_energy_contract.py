"""Energy proposals use the same validator as a real Core write without saving."""

import pytest

from ..utilities.assertions import assert_mcp_success


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [
    {"type": "battery", "stat_energy_from": "sensor.total_energy_kwh"},
    {"type": "grid"},
])
async def test_incomplete_source_is_never_reported_as_valid(mcp_client, source):
    before = assert_mcp_success(await mcp_client.call_tool("ha_manage_energy_prefs", {"mode": "get"}))
    result = await mcp_client.call_tool("ha_manage_energy_prefs", {"mode": "set", "config": {"energy_sources": [source]}, "dry_run": True})
    if result.is_error:
        assert "VALIDATION_FAILED" in str(result)
    else:
        data = assert_mcp_success(result)
        data = data.get("data", data)
        assert data["proposal_validation"]["status"] == "unavailable"
        assert data["partial"] is True
    after = assert_mcp_success(await mcp_client.call_tool("ha_manage_energy_prefs", {"mode": "get"}))
    assert before == after
