"""Conftest for dashboard E2E tests — routes proxied tools through the gateway.

Dashboard tools are no longer registered individually with MCP. They are served
via the ``ha_manage_dashboards`` category gateway (see tool_proxy.py). This
conftest provides a transparent wrapper so existing tests can call tools by
their original names while the underlying transport routes them through the
gateway's ``tool`` + ``args`` interface.
"""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastmcp import Client

logger = logging.getLogger(__name__)

# Tools served via ha_manage_dashboards gateway
# (from tools_config_dashboards + tools_resources modules)
_GATEWAY_TOOLS: frozenset[str] = frozenset({
    # tools_config_dashboards
    "ha_config_get_dashboard",
    "ha_config_set_dashboard",
    "ha_config_update_dashboard_metadata",
    "ha_config_delete_dashboard",
    "ha_get_dashboard_guide",
    "ha_get_card_types",
    "ha_get_card_documentation",
    "ha_dashboard_find_card",
    # tools_resources
    "ha_config_list_dashboard_resources",
    "ha_config_set_dashboard_resource",
    "ha_config_set_inline_dashboard_resource",
    "ha_config_delete_dashboard_resource",
})

_GATEWAY_NAME = "ha_manage_dashboards"


class _GatewayRoutingClient:
    """Wraps a FastMCP Client to route proxied tool calls through the gateway."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def call_tool(
        self, tool_name: str, params: dict[str, Any] | None = None
    ) -> Any:
        if tool_name in _GATEWAY_TOOLS:
            gateway_params: dict[str, Any] = {"tool": tool_name}
            if params:
                gateway_params["args"] = json.dumps(params)
            return await self._client.call_tool(_GATEWAY_NAME, gateway_params)
        return await self._client.call_tool(tool_name, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


@pytest.fixture
async def mcp_client(mcp_server) -> AsyncGenerator[_GatewayRoutingClient]:
    """Override mcp_client to route dashboard tools through the category gateway."""
    client = Client(mcp_server.mcp)
    async with client:
        logger.debug("Dashboard test client: routing proxied tools through gateway")
        yield _GatewayRoutingClient(client)
