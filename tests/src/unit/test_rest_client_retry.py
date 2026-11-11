"""Unit tests for the client's transient-gateway retry.

A reverse proxy / Supervisor ingress returns 502/503/504 when HA Core is
restarting or briefly overloaded behind it, and a 502 storm once failed ~190
unrelated tests at once. Reads retry with bounded backoff.

Writes never do. None of those statuses proves the request failed to execute
(RFC 9110 has 502 as "received an invalid response from an inbound server"), so
a replay can double-apply — see ``test_no_write_is_ever_gateway_retried``.

The client talks to a real Core through such a gateway: it answers the
statuses a test queues itself and forwards every other request, the
WebSocket included, to Core.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import (
    _MAX_REQUEST_ATTEMPTS,
    HomeAssistantAPIError,
    HomeAssistantClient,
)


class _Gateway:
    """A reverse proxy in front of Core, as nginx or Supervisor ingress is."""

    def __init__(self, core_url: str, session: aiohttp.ClientSession) -> None:
        self._core_url = core_url
        self._session = session
        # Statuses the gateway answers itself, one per REST request.
        self.outages: list[int] = []
        # "METHOD /path" of every REST request that reached the gateway.
        self.requests: list[str] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/websocket", self._websocket)
        app.router.add_route("*", "/{path:.*}", self._rest)
        return app

    async def _rest(self, request: web.Request) -> web.Response:
        self.requests.append(f"{request.method} {request.path}")
        if self.outages:
            return web.Response(status=self.outages.pop(0), text="upstream down")
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
            await asyncio.gather(_relay(caller, core), _relay(core, caller))
        return caller


async def _relay(
    source: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
    target: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
) -> None:
    async for message in source:
        await target.send_str(message.data)
    await target.close()


@pytest.fixture
async def gateway(core_client: HomeAssistantClient) -> AsyncIterator[_Gateway]:
    async with aiohttp.ClientSession() as session:
        yield _Gateway(core_client.base_url, session)


@pytest.fixture
async def client(
    gateway: _Gateway, aiohttp_client: Any, hass_access_token: str
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
        await behind_gateway.close()


def _backoffs(caplog: pytest.LogCaptureFixture) -> int:
    """How many retry backoffs the client logged before sleeping."""
    return sum(
        "retrying in" in record.getMessage()
        for record in caplog.records
        if record.name == "ha_mcp.client.rest_client"
    )


@pytest.fixture(autouse=True)
def _client_warnings(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="ha_mcp.client.rest_client")


@pytest.mark.asyncio
async def test_raw_request_retries_transient_502_then_succeeds(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    gateway: _Gateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hass.states.async_set("light.kitchen", "on")
    gateway.outages = [502]
    states = await client.get_states()
    assert "light.kitchen" in [state["entity_id"] for state in states]
    assert gateway.requests == ["GET /api/states"] * 2
    assert _backoffs(caplog) == 1


@pytest.mark.asyncio
async def test_raw_request_exhausts_retries_then_raises(
    client: HomeAssistantClient, gateway: _Gateway
) -> None:
    gateway.outages = [503] * (_MAX_REQUEST_ATTEMPTS + 1)
    with pytest.raises(HomeAssistantAPIError) as exc:
        await client.get_states()
    assert exc.value.status_code == 503
    assert len(gateway.requests) == _MAX_REQUEST_ATTEMPTS


@pytest.mark.asyncio
async def test_raw_request_does_not_retry_non_gateway_4xx(
    client: HomeAssistantClient,
    gateway: _Gateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Core's own 404 for an entity it does not hold, forwarded by the gateway.
    with pytest.raises(HomeAssistantAPIError) as exc:
        await client.get_entity_state("light.missing")
    assert exc.value.status_code == 404
    assert len(gateway.requests) == 1
    assert _backoffs(caplog) == 0


@pytest.mark.asyncio
async def test_raw_request_success_first_try_no_retry(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    gateway: _Gateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = await client.get_config()
    assert config["location_name"] == hass.config.location_name
    assert gateway.requests == ["GET /api/config"]
    assert _backoffs(caplog) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_reads_still_retry_every_gateway_status(
    hass: HomeAssistant, client: HomeAssistantClient, gateway: _Gateway, status: int
) -> None:
    """Replaying a read costs nothing, so the storm protection stays."""
    hass.states.async_set("light.kitchen", "on")
    gateway.outages = [status]
    states = await client.get_states()
    assert "light.kitchen" in [state["entity_id"] for state in states]
    assert len(gateway.requests) == 2


# Every write method the client sends: POST and DELETE.
_WRITES: dict[str, Callable[[HomeAssistantClient], Awaitable[Any]]] = {
    "POST": lambda client: client.fire_event("ha_mcp_probe"),
    "DELETE": lambda client: client.abort_config_flow("flow-1"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("method", sorted(_WRITES))
async def test_no_write_is_ever_gateway_retried(
    client: HomeAssistantClient,
    gateway: _Gateway,
    caplog: pytest.LogCaptureFixture,
    status: int,
    method: str,
) -> None:
    """None of these statuses proves Home Assistant did not execute the write.

    #1623 retried them on the premise that a gateway 5xx means the request
    never reached the backend. It does not: a replay can fire an event twice,
    run a script twice, or turn a completed DELETE into a misleading 404.
    """
    gateway.outages = [status] * _MAX_REQUEST_ATTEMPTS
    with pytest.raises(HomeAssistantAPIError) as exc:
        await _WRITES[method](client)
    assert exc.value.status_code == status
    assert len(gateway.requests) == 1
    assert gateway.requests[0].startswith(f"{method} ")
    assert _backoffs(caplog) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_the_config_flow_submit_is_not_replayed(
    client: HomeAssistantClient, gateway: _Gateway, status: int
) -> None:
    """The call that motivated the rule.

    HA consumes the flow_id on success, so a replay returns 404 "Invalid flow
    specified" — a definitive-looking 4xx that hides a first attempt which may
    already have committed.
    """
    gateway.outages = [status] * _MAX_REQUEST_ATTEMPTS
    with pytest.raises(HomeAssistantAPIError):
        await client.submit_config_flow_step("flow-1", {"host": "192.0.2.1"})
    assert len(gateway.requests) == 1
