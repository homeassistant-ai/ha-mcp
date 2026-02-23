"""Conftest for dashboard E2E tests — routes proxied tools through gateways.

Dashboard tools are no longer registered individually with MCP. They are served
via two category gateways (see tool_proxy.py):
- ``ha_dashboard_info`` — read-only tools (get, find, guide, docs, list resources)
- ``ha_manage_dashboards`` — write tools (set, delete dashboards and resources)

This conftest provides a transparent wrapper so existing tests can call tools by
their original names while the underlying transport routes them through the
correct gateway's ``tool`` + ``args`` interface.
"""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastmcp import Client

logger = logging.getLogger(__name__)

# Maps each proxied tool to its gateway.
_TOOL_TO_GATEWAY: dict[str, str] = {
    # ha_dashboard_info (read-only)
    "ha_config_get_dashboard": "ha_dashboard_info",
    "ha_dashboard_find_card": "ha_dashboard_info",
    "ha_get_dashboard_guide": "ha_dashboard_info",
    "ha_get_card_documentation": "ha_dashboard_info",
    "ha_config_list_dashboard_resources": "ha_dashboard_info",
    # ha_manage_dashboards (write)
    "ha_config_set_dashboard": "ha_manage_dashboards",
    "ha_config_delete_dashboard": "ha_manage_dashboards",
    "ha_config_set_dashboard_resource": "ha_manage_dashboards",
    "ha_config_delete_dashboard_resource": "ha_manage_dashboards",
}


class _GatewayRoutingClient:
    """Wraps a FastMCP Client to route proxied tool calls through gateways."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def call_tool(
        self, tool_name: str, params: dict[str, Any] | None = None
    ) -> Any:
        gateway = _TOOL_TO_GATEWAY.get(tool_name)
        if gateway:
            gateway_params: dict[str, Any] = {"tool": tool_name}
            if params:
                gateway_params["args"] = json.dumps(params)
            return await self._client.call_tool(gateway, gateway_params)
        return await self._client.call_tool(tool_name, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


@pytest.fixture
async def mcp_client(mcp_server) -> AsyncGenerator[_GatewayRoutingClient]:
    """Override mcp_client to route dashboard tools through category gateways."""
    client = Client(mcp_server.mcp)
    async with client:
        logger.debug("Dashboard test client: routing proxied tools through gateways")
        yield _GatewayRoutingClient(client)
