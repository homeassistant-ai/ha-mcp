"""The server's own HA client, answered by a real ``hass``.

Its REST transport reaches Core's REST API through ``hass_client`` and its
WebSocket client reaches Core's WebSocket API through ``hass_ws_client``, so
every reply, error and stored object in a test that uses it is Core's own.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandError

from ._component_core import ws_call


class CoreWebSocket:
    """The server's WebSocket client, answered by Core's WebSocket API."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        # One reply per command: the test socket has no request multiplexing.
        self._lock = asyncio.Lock()

    async def send_command(self, command_type: str, **params: Any) -> dict[str, Any]:
        async with self._lock:
            reply = await ws_call(self._ws, command_type, **params)
        if not reply["success"]:
            message, code = websocket_client._extract_ws_error(reply["error"])
            raise HomeAssistantCommandError(f"Command failed: {message}", code)
        return {"success": True, "result": reply["result"]}


class CoreTransport(httpx.AsyncBaseTransport):
    """The server's REST transport, answered by Core's REST API."""

    def __init__(self, rest: Any) -> None:
        self._rest = rest

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._rest.request(
            request.method,
            request.url.raw_path.decode(),
            data=request.content or None,
            headers={"Content-Type": "application/json"},
        )
        return httpx.Response(response.status, content=await response.read())


@contextlib.asynccontextmanager
async def core_backed_client(
    hass: HomeAssistant,
    hass_ws_client: Any,
    hass_client: Any,
    *,
    domains: tuple[str, ...] = (),
    component: bool = False,
) -> AsyncIterator[HomeAssistantClient]:
    """The server's HA client on ``hass``, with ``domains`` set up.

    ``component`` registers the ``ha_mcp_tools/*`` commands; without it the
    install has no component, as the commands are then unknown to Core.
    """
    # ``config`` serves the registry and flow endpoints, ``api`` the REST API.
    for domain in ("api", "config", *domains):
        assert await async_setup_component(hass, domain, {})
    if component:
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    socket = CoreWebSocket(await hass_ws_client(hass))
    client = HomeAssistantClient(base_url="http://core.test", token="token")
    client.httpx_client = httpx.AsyncClient(
        base_url="http://core.test/api", transport=CoreTransport(await hass_client())
    )
    # Every module's get_websocket_client resolves through the manager.
    with patch.object(
        websocket_client.websocket_manager,
        "get_client",
        AsyncMock(return_value=socket),
    ):
        yield client
    await client.httpx_client.aclose()
