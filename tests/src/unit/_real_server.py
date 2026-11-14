"""The real ha-mcp server, built on the server's own client, for wiring tests.

Construction runs the real ``_initialize_server``, so the middleware chain a
test reads from ``server.mcp.middleware`` is the one the server registers.
The FastMCP lifespan (HACS nudge, admin-token check, app discovery) is this
repository's own collaborator and talks to the network, so it is replaced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

import ha_mcp.server as server_module
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import reset_global_settings


@asynccontextmanager
async def _no_lifespan(_server: Any) -> AsyncIterator[dict[str, Any]]:
    yield {}


def build_server(
    monkeypatch: pytest.MonkeyPatch, client: HomeAssistantClient
) -> server_module.HomeAssistantSmartMCPServer:
    """Build the server from the current environment, without its lifespan.

    Settings are re-read, so a test sets its environment first; the caller's
    ``monkeypatch`` restores it, and the next settings read starts fresh.
    """
    monkeypatch.setattr(server_module, "server_lifespan", _no_lifespan)
    reset_global_settings()
    return server_module.HomeAssistantSmartMCPServer(client=client)
