"""Native fields and options survive the recorder tool adapters."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.core_contract import merge_core_options
from ha_mcp.tools.history_response import format_history_response
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
@pytest.mark.parametrize("minimal", [True, False])
@pytest.mark.parametrize(
    "row,expected",
    [
        (
            {
                "s": "on",
                "lu": 1700000000.25,
                "lc": 1700000000.0,
                "a": {"friendly_name": "Test"},
                "future_native_field": {"opaque": True},
            },
            {
                "state": "on",
                "last_updated": "2023-11-14T22:13:20.250000+00:00",
                "last_changed": "2023-11-14T22:13:20+00:00",
                "attributes": {"friendly_name": "Test"},
                "future_native_field": {"opaque": True},
            },
        ),
        (
            {"s": "off", "lu": 0, "future_native_field": [1]},
            {
                "state": "off",
                "last_updated": "1970-01-01T00:00:00+00:00",
                "last_changed": "1970-01-01T00:00:00+00:00",
                "future_native_field": [1],
            },
        ),
        (
            {"renamed_state": "on", "new_field": 5},
            {"renamed_state": "on", "new_field": 5},
        ),
        (
            {"s": "on", "state": "future Core value"},
            {"s": "on", "state": "future Core value"},
        ),
        (
            {
                "lu": 1700000000,
                "last_updated": "future value",
                "last_changed": "opaque",
            },
            {
                "lu": 1700000000,
                "last_updated": "future value",
                "last_changed": "opaque",
            },
        ),
    ],
)
async def test_history_renames_present_keys_once_and_preserves_all_core_data(
    minimal: bool,
    row: dict,
    expected: dict,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
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
        minimal,
        True,
        10,
        0,
        100,
        1000,
    )
    wrapped = format_history_response(
        {"data": result, "metadata": {"home_assistant_timezone": "UTC"}}
    )
    actual = wrapped["data"]["entities"][0]["states"][0]
    assert actual == expected


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timestamp,timezone,fetch_failed,expected",
    [
        (1700000000.25, "America/New_York", False, "2023-11-14T17:13:20.250000-05:00"),
        (1719856800, "America/New_York", False, "2024-07-01T14:00:00-04:00"),
        (1700000000.25, "UTC", True, "2023-11-14T22:13:20.250000+00:00"),
        (1700000000.25, "Invalid/Timezone", False, "2023-11-14T22:13:20.250000+00:00"),
    ],
)
async def test_history_preserves_native_attribute_values_through_timezone_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    timestamp: float,
    timezone: str,
    fetch_failed: bool,
    expected: str,
) -> None:
    from ha_mcp.tools import response_helpers, tools_history

    row = {
        "s": "on",
        "lu": timestamp,
        "a": {"last_updated": "2026-01-01T00:00:00Z"},
    }
    client = AsyncMock()
    client.send_websocket_message.return_value = {
        "success": True,
        "result": {"light.test": [row]},
    }
    monkeypatch.setattr(
        response_helpers,
        "fetch_ha_timezone",
        AsyncMock(return_value=(timezone, fetch_failed)),
    )
    result = await tools_history.HistoryTools(client).ha_get_history(
        entity_ids=["light.test"],
        start_time="1h",
        minimal_response=False,
    )
    assert result["data"]["entities"][0]["states"] == [
        {
            "state": row["s"],
            "last_updated": expected,
            "last_changed": expected,
            "attributes": row["a"],
        }
    ]
    assert result["metadata"]["timestamp_format"].startswith("ISO 8601")
    assert bool(result.get("warnings")) is fetch_failed
