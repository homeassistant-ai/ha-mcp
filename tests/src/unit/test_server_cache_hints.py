"""Gateways can share ha-mcp's tool list across callers (SEP-2549).

Under MCP 2026-07-28 every cacheable result carries ``ttlMs`` and
``cacheScope``. The SDK default is ``private``, which shared caches such as MCP
gateways read as a per-user catalog, forcing a namespace prefix on federated
tool names. ha-mcp's catalog is the same for every caller of an endpoint.
"""

from __future__ import annotations

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.server import HomeAssistantSmartMCPServer


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> HomeAssistantSmartMCPServer:
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "unused")
    return HomeAssistantSmartMCPServer()


async def test_gateways_can_share_the_tool_list(
    server: HomeAssistantSmartMCPServer,
) -> None:
    async with Client(server.mcp, mode="2026-07-28") as client:
        assert client.protocol_version == "2026-07-28"
        tools = await client.list_tools_mcp()
        resources = await client.list_resources_mcp()
    assert tools.tools
    for result in (tools, resources):
        assert result.cache_scope == "public"
        assert result.ttl_ms > 0
