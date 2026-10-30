"""An update cannot blank a helper's name, as a create cannot (#2632)."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from .test_helper_update_persistence import (  # noqa: F401 - fixtures
    mock_client,
    register_tools,
)


async def test_blank_name_update_is_refused_before_any_write(
    register_tools: dict[str, Any],  # noqa: F811
    mock_client: MagicMock,  # noqa: F811
) -> None:
    """Core's zone schema would store a blank name, which the zone dialog
    refuses, so the tool refuses it without writing."""
    mock_client.send_websocket_message = AsyncMock(
        side_effect=mock_client._make_ws_responses("zone")
    )

    with pytest.raises(ToolError) as exc_info:
        await register_tools["ha_config_set_helper"](
            helper_type="zone",
            helper_id="zone.home_office",
            config={"name": "  "},
        )

    error = json.loads(str(exc_info.value))["error"]
    assert "name cannot be blank" in error["message"]
    assert not any(
        c[0][0].get("type", "").endswith("/update")
        for c in mock_client.send_websocket_message.call_args_list
    )
