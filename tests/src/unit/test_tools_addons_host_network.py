"""Direct-port routing tests for host-network apps."""

from unittest.mock import MagicMock

import pytest

from ha_mcp.tools.tools_addons import _resolve_http_route, _resolve_ws_route


@pytest.mark.asyncio
async def test_host_network_direct_port_depends_on_caller_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an MCP app sharing the host namespace can use loopback."""
    client = MagicMock()
    addon = {
        "name": "Host Network App",
        "slug": "host_network_app",
        "host_network": True,
        "ip_address": "172.30.32.1",
    }

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    http_route, _ = await _resolve_http_route(client, addon, "api", 6052)
    assert http_route == "http://172.30.32.1:6052/api"

    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    http_route, _ = await _resolve_http_route(client, addon, "api", 6052)
    ws_route, _ = await _resolve_ws_route(client, addon, "ws", 6052)
    assert http_route == "http://127.0.0.1:6052/api"
    assert ws_route == "ws://127.0.0.1:6052/ws"
