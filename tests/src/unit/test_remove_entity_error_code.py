"""ha_remove_entity classifies a failure on Core's error code (#2632)."""

import json
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError

from .test_tools_entities_p2 import _make_client, _register


async def test_failure_mentioning_not_found_is_not_an_absent_entity() -> None:
    """A refusal whose text says 'not found' but whose code is not not_found
    is a failed removal, not an already-absent entity."""
    client = _make_client()
    client.send_websocket_message = AsyncMock(
        return_value={
            "success": False,
            "error": "Command failed: Config entry not found",
            "error_code": "home_assistant_error",
        }
    )
    tool = _register(client)["ha_remove_entity"]

    with pytest.raises(ToolError) as exc_info:
        await tool(entity_id="sensor.real")

    assert json.loads(str(exc_info.value))["error"]["code"] == "SERVICE_CALL_FAILED"


async def test_bulk_failure_mentioning_not_found_is_an_error_not_a_skip() -> None:
    client = _make_client()
    client.send_websocket_message = AsyncMock(
        return_value={
            "success": False,
            "error": "Config entry not found",
            "error_code": "home_assistant_error",
        }
    )
    tool = _register(client)["ha_remove_entity"]

    result = await tool(entity_id=["sensor.a"])

    assert result["skipped"] == []
    assert [e["entity_id"] for e in result["errors"]] == ["sensor.a"]


async def test_lost_connection_keeps_what_the_bulk_run_already_removed() -> None:
    """The removals that went through are reported, and the ids never reached
    are named so the caller can retry them."""
    client = _make_client()
    client.send_websocket_message = AsyncMock(
        side_effect=[
            {"success": True, "result": None},
            HomeAssistantConnectionError("socket closed"),
        ]
    )
    tool = _register(client)["ha_remove_entity"]

    result = await tool(entity_id=["sensor.a", "sensor.b", "sensor.c"])

    assert result["success"] is False
    assert result["removed"] == ["sensor.a"]
    assert result["errors"][0]["entity_id"] == "sensor.b"
    assert result["errors"][0]["code"] == "CONNECTION_FAILED"
    assert result["not_attempted"] == ["sensor.c"]
