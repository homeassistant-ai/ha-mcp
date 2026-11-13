"""A reverse proxy in front of real Core, as nginx or Supervisor ingress is.

The server's client reaches Core only through it. The gateway forwards every
request, the WebSocket included, unless a test queues what the gateway does
itself: answer a REST request, or drop the socket on a command.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import websocket_manager


class Gateway:
    def __init__(self, core_url: str, session: aiohttp.ClientSession) -> None:
        self._core_url = core_url
        self._session = session
        # Statuses the gateway answers itself, one per REST request.
        self.outages: list[int] = []
        # 200 bodies the gateway answers itself, such as its login page.
        self.pages: list[bytes] = []
        # WebSocket command types the gateway drops the socket on.
        self.drops: set[str] = set()
        # "METHOD /path" of every REST request that reached the gateway.
        self.requests: list[str] = []
        # Routes the gateway answers itself, ahead of forwarding to Core. A
        # test adds them before it takes ``gateway_client``, which serves
        # the app once.
        self.routes: list[web.AbstractRouteDef] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/websocket", self._websocket)
        app.add_routes(self.routes)
        app.router.add_route("*", "/{path:.*}", self._rest)
        return app

    async def _rest(self, request: web.Request) -> web.Response:
        self.requests.append(f"{request.method} {request.path}")
        if self.outages:
            return web.Response(status=self.outages.pop(0), text="upstream down")
        if self.pages:
            return web.Response(body=self.pages.pop(0))
        headers = {
            name: request.headers[name]
            for name in ("Authorization", "Content-Type")
            if name in request.headers
        }
        async with self._session.request(
            request.method,
            self._core_url + request.path_qs,
            headers=headers,
            data=await request.read(),
        ) as answer:
            return web.Response(
                status=answer.status,
                body=await answer.read(),
                content_type=answer.content_type,
            )

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        caller = web.WebSocketResponse()
        await caller.prepare(request)
        async with self._session.ws_connect(f"{self._core_url}/api/websocket") as core:
            relays = await asyncio.gather(
                _relay(caller, core, self.drops),
                _relay(core, caller, set()),
                return_exceptions=True,
            )
        for outcome in relays:
            # Sending to a side that has already closed is the normal end.
            if isinstance(outcome, Exception) and not isinstance(
                outcome, ConnectionError
            ):
                raise outcome
        return caller


async def _relay(
    source: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
    target: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
    drops: set[str],
) -> None:
    try:
        async for message in source:
            if message.type is aiohttp.WSMsgType.ERROR:
                raise message.data
            if message.type is not aiohttp.WSMsgType.TEXT:
                break
            if drops and json.loads(message.data).get("type") in drops:
                break
            await target.send_str(message.data)
    finally:
        await target.close()


@pytest.fixture
async def gateway(core_client: HomeAssistantClient) -> AsyncIterator[Gateway]:
    async with aiohttp.ClientSession() as session:
        yield Gateway(core_client.base_url, session)


@pytest.fixture
async def gateway_client(
    gateway: Gateway, aiohttp_client: Any, hass_access_token: str
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client, reaching Core only through ``gateway``."""
    server = await aiohttp_client(gateway.app())
    behind_gateway = HomeAssistantClient(
        base_url=str(server.make_url("")).rstrip("/"),
        token=hass_access_token,
        verify_ssl=False,
    )
    try:
        yield behind_gateway
    finally:
        try:
            await websocket_manager.disconnect()
        finally:
            await behind_gateway.close()
