"""The webhook relay's connection pool and the in-process server it forwards to."""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.components.webhook import async_generate_path
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest import LogCaptureFixture
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator

import custom_components.ha_mcp_tools.mcp_webhook as mw
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK,
    DATA_WEBHOOK_ID,
    DOMAIN,
    SERVER_KEEPALIVE_SECONDS,
    WEBHOOK_AUTH_NONE,
)

WEBHOOK_ID = "mcp_0123456789abcdef0123456789abcdef"


async def _register_relay(
    hass: HomeAssistant, port: int, secret_path: str
) -> aiohttp.ClientSession:
    """Register the real relay webhook towards ``port``; return its session."""
    assert await async_setup_component(hass, "webhook", {})
    entry = MockConfigEntry(domain=DOMAIN, data={DATA_WEBHOOK_ID: WEBHOOK_ID})
    entry.add_to_hass(hass)
    await mw.async_register_webhook(
        hass,
        entry,
        port=port,
        secret_path=secret_path,
        auth_mode=WEBHOOK_AUTH_NONE,
    )
    session: aiohttp.ClientSession = hass.data[DOMAIN][DATA_WEBHOOK]["session"]
    return session


async def test_relay_never_reuses_a_connection_the_server_has_closed(
    hass: HomeAssistant,
    socket_enabled: None,
) -> None:
    """A pooled connection the in-process server had already timed out made
    the next request fail with a connection reset and a 502."""

    async def peer_port(request: web.Request) -> web.Response:
        transport = request.transport
        assert transport is not None
        return web.Response(text=str(transport.get_extra_info("peername")[1]))

    app = web.Application()
    app.router.add_get("/private_x", peer_port)
    server = TestServer(app)
    await server.start_server()
    assert server.port is not None
    try:
        session = await _register_relay(hass, server.port, "/private_x")
        url = f"http://127.0.0.1:{server.port}/private_x"

        async with session.get(url) as resp:
            first = await resp.text()
        async with session.get(url) as resp:
            immediate = await resp.text()
        # Control: right after a request the pooled connection is reused.
        assert immediate == first

        # Just before the in-process server (uvicorn keep-alive, not this
        # test server's) would close it, the relay must already have dropped it.
        await asyncio.sleep(SERVER_KEEPALIVE_SECONDS - 0.5)
        async with session.get(url) as resp:
            later = await resp.text()
        assert later != first
    finally:
        await mw.async_unregister_webhook(hass)
        await server.close()


async def test_a_full_pool_logs_no_secret_path(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: LogCaptureFixture,
) -> None:
    """aiohttp's pool-acquire timeout names the target URL; its path is the
    secret path, so the 502 log line must carry only the error type."""
    release = asyncio.Event()

    async def hang(request: web.Request) -> web.Response:
        await release.wait()
        return web.Response(text="late")

    app = web.Application()
    app.router.add_route("*", "/p_s3cr3t", hang)
    server = TestServer(app)
    await server.start_server()
    assert server.port is not None
    held: asyncio.Task[None] | None = None
    try:
        await _register_relay(hass, server.port, "/p_s3cr3t")
        # A one-connection pool that is already busy, so the next relayed
        # request times out waiting for a connection.
        small = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(connect=0.3),
            connector=aiohttp.TCPConnector(limit=1),
        )
        await hass.data[DOMAIN][DATA_WEBHOOK]["session"].close()
        hass.data[DOMAIN][DATA_WEBHOOK]["session"] = small
        url = f"http://127.0.0.1:{server.port}/p_s3cr3t"

        async def hold() -> None:
            async with small.get(url) as resp:
                await resp.read()

        held = asyncio.create_task(hold())
        await asyncio.sleep(0.2)

        client = await hass_client_no_auth()
        resp = await client.post(async_generate_path(WEBHOOK_ID), data=b"{}")

        assert resp.status == 502
        assert "upstream request failed" in caplog.text
        assert "p_s3cr3t" not in caplog.text
    finally:
        release.set()
        if held is not None:
            await asyncio.gather(held, return_exceptions=True)
        await mw.async_unregister_webhook(hass)
        await server.close()
