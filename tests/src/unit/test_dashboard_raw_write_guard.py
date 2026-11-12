"""Native edits keep the dedicated dashboard tool's backup and policy boundary.

Core's WebSocket API carries a recording ``ha_mcp_tools/dashboard_edit``
command, so a refused edit is proved by the command never being called.
"""

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_service import ServiceTools

from ._code_mode import run_custom_tool


@pytest.fixture
def dashboard_edit_calls(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> list[dict[str, Any]]:
    """Register ``ha_mcp_tools/dashboard_edit`` on Core; return its calls."""
    calls: list[dict[str, Any]] = []

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "ha_mcp_tools/dashboard_edit",
            vol.Optional("config"): dict,
        }
    )
    @websocket_api.async_response
    async def _record(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        calls.append(msg)
        connection.send_result(msg["id"], None)

    websocket_api.async_register_command(hass, _record)
    return calls


@pytest.mark.parametrize(
    "command", ["ha_mcp_tools/dashboard_edit", "HA_MCP_TOOLS/DASHBOARD_EDIT"]
)
async def test_native_edit_cannot_bypass_dashboard_tool(
    core_client: HomeAssistantClient,
    dashboard_edit_calls: list[dict[str, Any]],
    command: str,
) -> None:
    tools = ServiceTools(core_client, DeviceControlTools(client=core_client))
    with pytest.raises(ToolError, match="Use the dedicated ha-mcp tools instead"):
        await tools.ha_call_service(ws_command=command)
    assert dashboard_edit_calls == []


async def test_native_edit_cannot_bypass_dashboard_tool_in_sandbox(
    core_client: HomeAssistantClient,
    dashboard_edit_calls: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await run_custom_tool(
        core_client,
        monkeypatch,
        "await ws_send({'type': 'ha_mcp_tools/dashboard_edit', 'config': {'views': []}})",
    )
    assert "blocked" in result.get("error", "")
    assert dashboard_edit_calls == []
