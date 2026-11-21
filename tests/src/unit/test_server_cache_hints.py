"""The server marks its cacheable results shareable across callers (SEP-2549).

Under MCP 2026-07-28 every ``tools/list`` result carries ``ttlMs`` and
``cacheScope``. The SDK default is ``private``, which shared caches such as MCP
gateways read as a per-user catalog. ha-mcp's catalog depends only on the
deployment's settings and the endpoint, so it advertises ``public``.
"""

from __future__ import annotations

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.tool_catalog import CATALOG_CACHE_HINT

TTL_MS = CATALOG_CACHE_HINT["cache_ttl"] * 1000


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> HomeAssistantSmartMCPServer:
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "unused")
    return HomeAssistantSmartMCPServer()


async def test_modern_tools_list_is_public_with_ttl(
    server: HomeAssistantSmartMCPServer,
) -> None:
    async with Client(server.mcp, mode="2026-07-28") as client:
        assert client.protocol_version == "2026-07-28"
        result = await client.list_tools_mcp()
    assert result.tools
    assert result.cache_scope == "public"
    assert result.ttl_ms == TTL_MS > 0


async def test_modern_resources_list_shares_the_hint(
    server: HomeAssistantSmartMCPServer,
) -> None:
    async with Client(server.mcp, mode="2026-07-28") as client:
        resources = await client.list_resources_mcp()
    assert resources.cache_scope == "public"
    assert resources.ttl_ms == TTL_MS


async def test_legacy_tools_list_carries_no_cache_fields(
    server: HomeAssistantSmartMCPServer,
) -> None:
    # Pre-2026 protocol revisions have no caching fields; the hint must not leak.
    async with Client(server.mcp, mode="legacy") as client:
        result = await client.list_tools_mcp()
        dumped = result.model_dump(by_alias=True, exclude_unset=True)
    assert result.tools
    assert "cacheScope" not in dumped
    assert "ttlMs" not in dumped
