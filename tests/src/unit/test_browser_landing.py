"""Unit tests for the browser landing page on GET requests."""

import httpx
import pytest
from fastmcp import FastMCP

from ha_mcp.__main__ import _registered_landing_paths, register_browser_landing


@pytest.fixture(autouse=True)
def _clear_landing_registry():
    """Clear the registered-paths set so each test starts fresh."""
    _registered_landing_paths.clear()
    yield
    _registered_landing_paths.clear()


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
    assert "Block AI training bots" in resp.text
    assert "dash.cloudflare.com" in resp.text
    assert resp.headers["allow"] == "POST, DELETE"


@pytest.mark.asyncio
async def test_head_returns_405_with_allow_header(mcp_app):
    """HEAD on the MCP path should return 405 with the correct Allow header."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app), base_url="http://test"
    ) as client:
        resp = await client.head("/mcp")

    assert resp.status_code == 405
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
    assert resp.status_code != 405  # POST must not be intercepted by the landing route
    assert "HA-MCP server is up and running" not in resp.text


@pytest.mark.asyncio
async def test_custom_path_mounts_at_correct_path():
    """Landing page should mount at the custom path, not the default."""
    server = FastMCP("test")
    register_browser_landing(server, "/secret-abc")
    app = server.http_app(path="/secret-abc", stateless_http=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Custom path should serve the landing page
        resp = await client.get("/secret-abc")
        assert resp.status_code == 405
        assert "HA-MCP server is up and running" in resp.text

        # Default /mcp path should NOT serve the landing page
        resp_default = await client.get("/mcp")
        assert resp_default.status_code != 405
        assert "HA-MCP server is up and running" not in resp_default.text
