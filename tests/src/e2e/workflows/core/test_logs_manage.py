"""ha_manage_logs against real Core: statistics commands and the recorder wait."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from ha_mcp._vendor.fastmcp import Client

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.cleanup import CleanupSteps
from ...utilities.component_path import assert_component_path
from ...utilities.topology import component_surface_available
from ...utilities.wait_helpers import wait_for_tool_result


def _sums(data: dict[str, Any]) -> list[float]:
    entities = data.get("data", data).get("entities") or [{}]
    return [row["sum"] for row in entities[0].get("statistics") or []]


@pytest.mark.asyncio
@pytest.mark.core
@pytest.mark.component_topology
async def test_statistic_import_adjust_and_clear(mcp_client: Client) -> None:
    """An imported statistic is listed, an adjustment moves its later sums,
    and a clear removes it."""
    mcp = MCPAssertions(mcp_client)
    statistic_id = f"test:e2e_logs_manage_{uuid4().hex[:8]}"
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        days=2
    )
    hours = [start + timedelta(hours=i) for i in range(3)]
    history = {
        "source": "statistics",
        "entity_ids": statistic_id,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=3)).isoformat(),
        "period": "hour",
    }
    try:
        await mcp.call_tool_success(
            "ha_manage_logs",
            {
                "action": "recorder/import_statistics",
                "data": {
                    "metadata": {
                        "statistic_id": statistic_id,
                        "source": "test",
                        "name": "E2E ha_manage_logs",
                        "has_sum": True,
                        "mean_type": 0,
                        "unit_class": "energy",
                        "unit_of_measurement": "kWh",
                    },
                    "stats": [
                        {"start": hour.isoformat(), "state": total, "sum": total}
                        for hour, total in zip(hours, (1, 2, 3), strict=True)
                    ],
                },
            },
        )
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_manage_logs",
            arguments={"action": "recorder/list_statistic_ids", "limit": 1000},
            predicate=lambda d: any(
                row["statistic_id"] == statistic_id for row in d["result"]
            ),
            description="imported statistic listed",
            timeout=30,
        )
        # The query matches the name and the id; the entities' aliases are
        # searched only with the component.
        searched = await mcp.call_tool_success(
            "ha_manage_logs",
            {
                "action": "recorder/list_statistic_ids",
                "query": f"HA_MANAGE_LOGS {statistic_id.split(':')[1]}",
            },
        )
        assert [row["statistic_id"] for row in searched["result"]] == [statistic_id]
        assert_component_path(searched)
        aliases_skipped = any("aliases" in w for w in searched.get("warnings", []))
        assert aliases_skipped is not component_surface_available()
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_history",
            arguments=history,
            predicate=lambda d: _sums(d) == [1, 2, 3],
            description="imported statistic rows readable",
            timeout=30,
        )

        adjusted = await mcp.call_tool_success(
            "ha_manage_logs",
            {
                "action": "recorder/adjust_sum_statistics",
                "data": {
                    "statistic_id": statistic_id,
                    "start_time": hours[1].isoformat(),
                    "adjustment": 10,
                    "adjustment_unit_of_measurement": "kWh",
                },
            },
        )
        assert_component_path(adjusted)
        assert [row["statistic_id"] for row in adjusted["statistics_metadata"]] == [
            statistic_id
        ]
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_history",
            arguments=history,
            predicate=lambda d: _sums(d) == [1, 12, 13],
            description="adjusted sums readable",
            timeout=30,
        )

        cleared = await mcp.call_tool_success(
            "ha_manage_logs",
            {
                "action": "recorder/clear_statistics",
                "data": {"statistic_ids": [statistic_id]},
            },
        )
        # Without the component the clear may still be running when the tool reads back.
        if component_surface_available():
            assert cleared["statistics_metadata"] == []
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_history",
            arguments=history,
            predicate=lambda d: _sums(d) == [],
            description="cleared statistic gone",
            timeout=30,
        )
    finally:
        with CleanupSteps() as cleanup, cleanup.step():
            await safe_call_tool(
                mcp_client,
                "ha_manage_logs",
                {
                    "action": "recorder/clear_statistics",
                    "data": {"statistic_ids": [statistic_id]},
                },
            )


@pytest.mark.asyncio
@pytest.mark.core
@pytest.mark.component_topology
async def test_a_recorder_service_reports_how_it_waited(mcp_client: Client) -> None:
    """With the component the recorder itself is waited for and its status
    comes back; without it the result says what its queue cannot show."""
    mcp = MCPAssertions(mcp_client)

    result = await mcp.call_tool_success(
        "ha_manage_logs", {"action": "recorder.enable"}
    )

    assert_component_path(result)
    assert result["recorder"]["recording"] is True
    if component_surface_available():
        assert set(result["recorder"]) >= {
            "purge_keep_days",
            "auto_purge",
            "oldest_state",
        }
        assert result["recorder"]["enabled"] is True
        assert "warnings" not in result
    else:
        assert any("may still be running" in warning for warning in result["warnings"])
