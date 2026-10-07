"""Native fields and options survive the recorder tool adapters."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.core_contract import merge_core_options
from ha_mcp.tools.tools_history import _fetch_history, _fetch_statistics


@pytest.mark.asyncio
async def test_future_statistics_type_reaches_core_and_response_is_preserved() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    row = {"start": 1, "end": 2, "future_native_type": {"opaque": [3]}}
    client = AsyncMock()
    client.send_websocket_message.side_effect = [
        {"success": True, "result": []},
        {"success": True, "result": {"sensor.energy": [row]}},
    ]
    result = await _fetch_statistics(
        client,
        ["sensor.energy"],
        start,
        start + timedelta(hours=1),
        "hour",
        ["future_native_type"],
        10,
        0,
    )
    assert client.send_websocket_message.call_args.args[0]["types"] == [
        "future_native_type"
    ]
    assert result["entities"][0]["statistics"] == [row]


@pytest.mark.asyncio
async def test_history_keeps_native_fields_beside_readable_aliases() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    row = {"s": "on", "lc": 1, "future_native_field": {"opaque": True}}
    client = AsyncMock()
    client.send_websocket_message.return_value = {
        "success": True,
        "result": {"light.test": [row]},
    }
    result = await _fetch_history(
        client,
        ["light.test"],
        start,
        start + timedelta(hours=1),
        True,
        True,
        10,
        0,
        100,
        1000,
    )
    actual = result["entities"][0]["states"][0]
    assert actual.items() >= row.items()
    assert actual["state"] == "on"


@pytest.mark.parametrize(
    "key", ["id", "type", "entity_ids", "start_time", "no_attributes"]
)
def test_native_options_cannot_bypass_query_guards(key: str) -> None:
    with pytest.raises(ToolError, match="protected"):
        merge_core_options(
            {
                "entity_ids": ["sensor.safe"],
                "start_time": "fixed",
                "no_attributes": True,
            },
            {key: "override"},
        )


def test_new_native_options_are_not_silently_filtered() -> None:
    assert merge_core_options({"period": "hour"}, {"future": {"nested": 3}})[
        "future"
    ] == {"nested": 3}
