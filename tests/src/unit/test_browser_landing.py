"""Unit tests for the browser landing page on GET requests."""

import httpx
import pytest
from fastmcp import FastMCP

from ha_mcp.__main__ import register_browser_landing


@pytest.fixture
def mcp_app():
    """Create a FastMCP app with the browser landing route registered."""
    server = FastMCP("test")
    register_browser_landing(server, "/mcp")
    return server.http_app(path="/mcp", stateless_http=True)


@pytest.mark.asyncio
async def test_get_returns_405_with_helpful_message(mcp_app):
    """GET should return 405 with the landing text and Allow header."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app), base_url="http://test"
    ) as client:
        resp = await client.get("/mcp")

    assert resp.status_code == 405
    assert "HA-MCP server is up and running" in resp.text
    assert resp.headers["allow"] == "POST, DELETE"


@pytest.mark.asyncio
async def test_post_not_intercepted_by_landing(mcp_app):
    """POST on the MCP path must reach the MCP handler, not the landing page."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
            headers={"Content-Type": "application/json"},
        )

    # The MCP handler errors (no lifespan in test), but the key assertion is
    # that POST was NOT intercepted by the landing page route.
    assert "HA-MCP server is up and running" not in resp.text
