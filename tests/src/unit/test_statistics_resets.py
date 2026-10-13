"""Never expose value-scaled reset timestamps when native recovery is unavailable."""

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.tools.statistics_resets import restore_reset_timestamps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        {"success": False},
        TimeoutError("offline"),
        {"success": True, "result": {}},
    ],
)
async def test_unrecoverable_timestamp_is_omitted_without_losing_values(
    failure: Any,
) -> None:
    rows = {"sensor.energy": [{"start": 1000, "last_reset": 900000, "sum": 123}]}
    metadata = (
        {
            "sensor.energy": {
                "unit_class": "energy",
                "statistics_unit_of_measurement": "MWh",
                "display_unit_of_measurement": "kWh",
            }
        }
        if failure is not None
        else {}
    )
    client = Mock()
    client.send_websocket_message = AsyncMock(
        side_effect=failure if isinstance(failure, Exception) else None,
        return_value=failure,
    )
    warnings = await restore_reset_timestamps(
        client, rows, metadata, {"period": "hour"}
    )
    assert rows == {"sensor.energy": [{"start": 1000, "sum": 123}]}
    assert "last_reset omitted" in warnings[0]
    if failure is None:
        client.send_websocket_message.assert_not_called()
