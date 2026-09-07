"""Native edits keep the dedicated dashboard tool's backup and policy boundary."""

import pytest
from fastmcp.exceptions import ToolError

from .test_call_service_ws_command import _make_tools


@pytest.mark.parametrize("command", ["ha_mcp_tools/dashboard_edit", "HA_MCP_TOOLS/DASHBOARD_EDIT"])
async def test_native_edit_cannot_bypass_dashboard_tool(command):
    tools = _make_tools({"success": True, "result": None})
    with pytest.raises(ToolError, match="dedicated tool guards with backups and conflict checks"):
        await tools.ha_call_service(ws_command=command)
    tools._client.send_websocket_message.assert_not_awaited()
