"""ha_remove_entity classifies a failure on Core's error code (#2632)."""

import json
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from .test_tools_entities_p2 import _make_client, _register


async def test_failure_mentioning_not_found_is_not_an_absent_entity():
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
