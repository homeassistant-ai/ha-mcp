"""Unit tests for the browser landing page on GET requests."""

import httpx
import pytest
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse


@pytest.mark.asyncio
async def test_get_returns_landing_page():
    """GET on the MCP path should return 200 with the landing text."""
    server = FastMCP("test")

    @server.custom_route("/mcp", methods=["GET"])
    async def _browser_landing(_: Request) -> PlainTextResponse:
        return PlainTextResponse("HA-MCP server is up and running.")

    app = server.http_app(path="/mcp", stateless_http=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/mcp")

    assert resp.status_code == 200
    assert "HA-MCP server is up and running" in resp.text
