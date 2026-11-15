"""The in-process server's webhook on a real ``hass``, relaying to a real upstream.

``upstream`` is a loopback HTTP server standing in for the in-process MCP
server, ``register`` registers the component's own webhook towards it, and
every request goes through Core's HTTP server and webhook router.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

import custom_components.ha_mcp_tools.mcp_webhook as mw
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK_ID,
    DOMAIN,
    WEBHOOK_AUTH_LEGACY,
)
from custom_components.ha_mcp_tools.oauth_legacy import LegacyOAuthProvider

WEBHOOK_ID = "mcp_0123456789abcdef0123456789abcdef"
SECRET_PATH = "/private_aaaaaaaaaaaaaaaa"
OAUTH_CLIENT_ID = "cid"
OAUTH_CLIENT_SECRET = "secret"
OAUTH_SIGNING_KEY = secrets.token_hex(32)
PUBLIC_HEADERS = {"Host": "abc.ui.nabu.casa", "X-Forwarded-Proto": "https"}
WEBHOOK_PATH = f"/api/webhook/{WEBHOOK_ID}"
READONLY_PATH = f"{WEBHOOK_PATH}/readonly"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
Register = Callable[..., Awaitable[bool]]


@dataclass
class Received:
    """One request the upstream was sent."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class Upstream:
    """A loopback server recording each request; ``handler`` answers it."""

    def __init__(self) -> None:
        self.requests: list[Received] = []
        self.handler: Handler = _ok
        self.server: TestServer

    @property
    def port(self) -> int:
        assert self.server.port is not None
        return self.server.port

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            Received(
                request.method,
                request.path,
                dict(request.headers),
                await request.read(),
            )
        )
        return await self.handler(request)


async def _ok(request: web.Request) -> web.StreamResponse:
    return web.json_response({"ok": 1})


@pytest.fixture
async def upstream(socket_enabled: None) -> AsyncIterator[Upstream]:
    """A loopback server standing in for the in-process MCP server."""
    up = Upstream()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", up.handle)
    up.server = TestServer(app)
    await up.server.start_server()
    try:
        yield up
    finally:
        await up.server.close()


@pytest.fixture
async def register(hass: HomeAssistant, upstream: Upstream) -> AsyncIterator[Register]:
    """Register the real webhook relaying to ``upstream``; unregistered after.

    ``register(auth_mode, **options)`` returns whether a restart is needed; the
    options replace the defaults of ``async_register_webhook``.
    """
    assert await async_setup_component(hass, "webhook", {})

    async def do_register(
        auth_mode: str, *, webhook_id: str = WEBHOOK_ID, **options: Any
    ) -> bool:
        entry = MockConfigEntry(domain=DOMAIN, data={DATA_WEBHOOK_ID: webhook_id})
        entry.add_to_hass(hass)
        arguments: dict[str, Any] = {
            "port": upstream.port,
            "secret_path": SECRET_PATH,
            "auth_mode": auth_mode,
            "oauth_client_id": OAUTH_CLIENT_ID,
            "oauth_client_secret": OAUTH_CLIENT_SECRET,
            "oauth_signing_key": OAUTH_SIGNING_KEY,
            **options,
        }
        return await mw.async_register_webhook(hass, entry, **arguments)

    try:
        yield do_register
    finally:
        await mw.async_unregister_webhook(hass)


def fail_view_registration(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, nth: int
) -> None:
    """Make Core's router refuse the ``nth`` view the component binds.

    The read-only and catalog aliases bind first, then the discovery bundle.
    """
    bind = hass.http.register_view
    bound: list[object] = []

    def register_view(view: Any) -> None:
        bound.append(view)
        if len(bound) == nth:
            raise RuntimeError("router frozen")
        bind(view)

    monkeypatch.setattr(hass.http, "register_view", register_view)


def leave_stale_endpoint(hass: HomeAssistant) -> AsyncMock:
    """Core's webhook at ``WEBHOOK_ID``, as a crashed unload would leave it."""
    stale = AsyncMock(return_value=web.Response(status=204))
    webhook.async_register(hass, DOMAIN, "stale", WEBHOOK_ID, stale)
    return stale


def legacy_provider() -> LegacyOAuthProvider:
    """A provider holding the registered legacy credentials, to mint tokens."""
    return LegacyOAuthProvider(
        OAUTH_CLIENT_ID,
        OAUTH_CLIENT_SECRET,
        bytes.fromhex(OAUTH_SIGNING_KEY),
        lambda: WEBHOOK_AUTH_LEGACY,
    )
