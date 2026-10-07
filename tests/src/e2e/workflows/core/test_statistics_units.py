"""Exercise real Core conversion and statistics without a current entity (#2682)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ...utilities.assertions import assert_mcp_success
from ...utilities.wait_helpers import wait_for_tool_result


@pytest.mark.asyncio
@pytest.mark.core
async def test_core_display_conversion_labels_the_converted_values(mcp_client, ha_client):
    """A stored MWh statistic must report MWh without a state, then kWh with conversion."""
    entity_id = f"sensor.e2e_statistics_{uuid4().hex}"
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
    args = {
        "source": "statistics", "entity_ids": entity_id,
        "start_time": start.isoformat(), "end_time": (start + timedelta(hours=3)).isoformat(),
        "period": "hour", "statistic_types": ["state", "sum", "change"],
    }
    state_created = False
    try:
        imported = await ha_client.send_websocket_message({
            "type": "recorder/import_statistics",
            "metadata": {
                "statistic_id": entity_id, "source": "recorder", "name": "E2E energy conversion",
                "unit_of_measurement": "MWh", "unit_class": "energy", "mean_type": 0, "has_sum": True,
            },
            "stats": [
                {"start": (start + timedelta(hours=i)).isoformat(), "state": value, "sum": value}
                for i, value in enumerate((1.0, 1.25, 1.5))
            ],
        })
        assert imported["success"], imported
        ready = await wait_for_tool_result(
            mcp_client, tool_name="ha_get_history", arguments=args,
            predicate=lambda d: len(d.get("data", d).get("entities", [{}])[0].get("statistics", [])) == 3,
            description="imported recorder statistics visible", timeout=30,
        )
        entity = ready.get("data", ready)["entities"][0]
        assert entity["unit_of_measurement"] == "MWh"
        assert [r["sum"] for r in entity["statistics"]] == [1.0, 1.25, 1.5]

        await ha_client._request("POST", f"/states/{entity_id}", json={
            "state": "1500", "attributes": {
                "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total",
            },
        })
        state_created = True
        result = assert_mcp_success(await mcp_client.call_tool("ha_get_history", args))
        entity = result.get("data", result)["entities"][0]
        assert entity["unit_of_measurement"] == "kWh"
        assert entity["statistics_metadata"]["statistics_unit_of_measurement"] == "MWh"
        assert [r["sum"] for r in entity["statistics"]] == [1000.0, 1250.0, 1500.0]
        assert entity["statistics"][1]["change"] == 250.0
    finally:
        try:
            if state_created:
                await ha_client._request("DELETE", f"/states/{entity_id}")
        finally:
            cleared = await ha_client.send_websocket_message({
                "type": "recorder/clear_statistics", "statistic_ids": [entity_id],
            })
            assert cleared["success"], cleared
