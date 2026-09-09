"""Native edits keep the dedicated dashboard tool's backup and policy boundary."""

import pytest
from fastmcp.exceptions import ToolError

from .test_call_service_ws_command import _make_tools


@pytest.mark.parametrize(
    "command", ["ha_mcp_tools/dashboard_edit", "HA_MCP_TOOLS/DASHBOARD_EDIT"]
)
async def test_native_edit_cannot_bypass_dashboard_tool(command):
    tools = _make_tools({"success": True, "result": None})
    with pytest.raises(ToolError, match="Use the dedicated ha-mcp tools instead"):
        await tools.ha_call_service(ws_command=command)
    tools._client.send_websocket_message.assert_not_awaited()


async def test_native_edit_cannot_bypass_dashboard_tool_in_sandbox():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from ha_mcp.tools.tools_code import _SandboxBridge

    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": None}
    )
    bridge = _SandboxBridge(
        MagicMock(), client, SimpleNamespace(code_mode_max_invocations=10)
    )
    result = await bridge.ws_send(
        {"type": "ha_mcp_tools/dashboard_edit", "config": {"views": []}}
    )
    assert "blocked" in result.get("error", "")
    client.send_websocket_message.assert_not_awaited()
