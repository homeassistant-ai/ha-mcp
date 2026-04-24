"""Shared in-process FastMCP client context for UAT.

Used by the story runner (setup/verify/teardown) and pytest fixtures.
Constructing the server is ~1s; this lets callers share one instance
across many tool calls against the same HA instance.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import Client


@contextlib.asynccontextmanager
async def inprocess_mcp_client(
    ha_url: str, ha_token: str
) -> AsyncIterator[Client]:
    """Build one in-process FastMCP client for setup/verify/teardown.

    The env-swap and WebSocket disconnect point ha_mcp's module-level settings
    at the target HA instance; the websocket client reads env, not the passed
    HomeAssistantClient's base_url.
    """
    from fastmcp import Client

    import ha_mcp.config
    from ha_mcp.client import HomeAssistantClient
    from ha_mcp.client.websocket_client import websocket_manager
    from ha_mcp.server import HomeAssistantSmartMCPServer

    prev_url = os.environ.get("HOMEASSISTANT_URL")
    prev_token = os.environ.get("HOMEASSISTANT_TOKEN")
    prev_settings = ha_mcp.config._settings
    try:
        os.environ["HOMEASSISTANT_URL"] = ha_url
        os.environ["HOMEASSISTANT_TOKEN"] = ha_token
        ha_mcp.config._settings = None
        await websocket_manager.disconnect()

        client = HomeAssistantClient(base_url=ha_url, token=ha_token)
        server = HomeAssistantSmartMCPServer(client=client)
        async with Client(server.mcp) as mcp_client:
            yield mcp_client
    finally:
        if prev_url is None:
            os.environ.pop("HOMEASSISTANT_URL", None)
        else:
            os.environ["HOMEASSISTANT_URL"] = prev_url
        if prev_token is None:
            os.environ.pop("HOMEASSISTANT_TOKEN", None)
        else:
            os.environ["HOMEASSISTANT_TOKEN"] = prev_token
        ha_mcp.config._settings = prev_settings
