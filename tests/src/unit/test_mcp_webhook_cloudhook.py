"""Nabu Casa cloudhook relay through the in-process server's webhook (#2696).

Settings → Home Assistant Cloud → Webhooks relays a cloudhook in-process as
HA's ``MockRequest``, which has no ``read()`` and no transport, and the relay
returns only ``response.body`` — so the forwarder must read the body via
``content`` and hand back the SSE reply buffered rather than streamed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from homeassistant.components.webhook import async_handle_webhook
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.aiohttp import MockRequest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import cloudhook
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK_ID,
    DOMAIN,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)
from custom_components.ha_mcp_tools.mcp_webhook import (
    async_register_webhook,
    async_unregister_webhook,
)

WEBHOOK_ID = "mcp_0123456789abcdef0123456789abcdef"
SECRET_PATH = "/private_aaaaaaaaaaaaaaaa"


@pytest.fixture
async def webhook_hass(hass: HomeAssistant) -> AsyncIterator[HomeAssistant]:
    """A real ``hass`` with Core's webhook integration; the relay is closed after."""
    assert await async_setup_component(hass, "webhook", {})
    yield hass
    await async_unregister_webhook(hass)


async def _register(
    hass: HomeAssistant, upstream: Any, auth_mode: str = WEBHOOK_AUTH_NONE
) -> None:
    """Register the real ingress webhook, relaying to the real ``upstream`` server."""
    entry = MockConfigEntry(domain=DOMAIN, data={DATA_WEBHOOK_ID: WEBHOOK_ID})
    entry.add_to_hass(hass)
    await async_register_webhook(
        hass,
        entry,
        port=upstream.port,
        secret_path=SECRET_PATH,
        auth_mode=auth_mode,
    )


async def test_cloudhook_relay_reads_body_and_buffers_sse(
    webhook_hass: HomeAssistant, aiohttp_server: Any, socket_enabled: None
) -> None:
    chunks = [b"event: message\ndata: 1\n\n", b"data: 2\n\n"]
    received: list[bytes] = []

    async def upstream_handler(request: web.Request) -> web.StreamResponse:
        received.append(await request.read())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for chunk in chunks:
            await response.write(chunk)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post(SECRET_PATH, upstream_handler)
    upstream = await aiohttp_server(app)
    await _register(webhook_hass, upstream)

    body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    request = MockRequest(
        content=body,
        mock_source="cloud",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    resp = await async_handle_webhook(webhook_hass, WEBHOOK_ID, request)

    assert received == [body]
    assert isinstance(resp, web.Response)
    assert resp.status == 200
    assert resp.body == b"".join(chunks)
    assert resp.headers["Content-Type"] == "text/event-stream"


async def test_cloudhook_reply_that_never_ends_is_cut_off_with_504(
    webhook_hass: HomeAssistant,
    aiohttp_server: Any,
    socket_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A ``subscriptions/listen`` stream never ends; a cloudhook cannot carry it.
    release = asyncio.Event()

    async def upstream_handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b"event: message\ndata: 1\n\n")
        await release.wait()
        return response

    app = web.Application()
    app.router.add_post(SECRET_PATH, upstream_handler)
    upstream = await aiohttp_server(app)
    await _register(webhook_hass, upstream)
    monkeypatch.setattr(cloudhook, "REPLY_SECONDS", 0.01)

    request = MockRequest(content=b"{}", mock_source="cloud", method="POST")
    try:
        resp = await async_handle_webhook(webhook_hass, WEBHOOK_ID, request)
    finally:
        release.set()

    assert resp.status == 504
    assert "did not finish within" in caplog.text


async def test_oauth_gated_cloudhook_is_refused_with_the_reason(
    webhook_hass: HomeAssistant, aiohttp_server: Any, socket_enabled: None
) -> None:
    # Home Assistant Cloud relays only Content-Type back, so the 401's
    # WWW-Authenticate challenge could never reach the client.
    received: list[bytes] = []

    async def upstream_handler(request: web.Request) -> web.Response:
        received.append(await request.read())
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post(SECRET_PATH, upstream_handler)
    upstream = await aiohttp_server(app)
    await _register(webhook_hass, upstream, auth_mode=WEBHOOK_AUTH_HA)

    request = MockRequest(
        content=b"{}",
        mock_source="cloud",
        method="POST",
        headers={"Host": "hooks.nabu.casa"},
    )
    resp = await async_handle_webhook(webhook_hass, WEBHOOK_ID, request)

    assert resp.status == 400
    assert resp.text is not None
    assert "auth mode 'none'" in resp.text
    assert received == []
