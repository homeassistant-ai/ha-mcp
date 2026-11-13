"""Native edits keep the dedicated dashboard tool's backup and policy boundary.

The component's own ``ha_mcp_tools/*`` commands are registered on Core, so the
edit would reach the real ``ha_mcp_tools/dashboard_edit``; a spy on the
client's WebSocket send proves a refused edit is never sent.
"""

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_service import ServiceTools

from ._client_spies import sent_messages
from ._code_mode import run_custom_tool


@pytest.mark.parametrize(
    "command", ["ha_mcp_tools/dashboard_edit", "HA_MCP_TOOLS/DASHBOARD_EDIT"]
)
async def test_native_edit_cannot_bypass_dashboard_tool(
    component_core_client: HomeAssistantClient, command: str
) -> None:
    tools = ServiceTools(
        component_core_client, DeviceControlTools(client=component_core_client)
    )
    with (
        sent_messages(component_core_client) as sent,
        pytest.raises(ToolError, match="Use the dedicated ha-mcp tools instead"),
    ):
        await tools.ha_call_service(ws_command=command)
    sent.assert_not_awaited()


async def test_native_edit_cannot_bypass_dashboard_tool_in_sandbox(
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sent_messages(component_core_client) as sent:
        result = await run_custom_tool(
            component_core_client,
            monkeypatch,
            "await ws_send({'type': 'ha_mcp_tools/dashboard_edit', 'config': {'views': []}})",
        )
    assert "blocked" in result.get("error", "")
    sent.assert_not_awaited()
