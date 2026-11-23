"""The sidebar panel's HTTP surface and a real settings server behind its proxy."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.ha_mcp_tools import ui_panel
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK_ID,
    DOMAIN,
)
from custom_components.ha_mcp_tools.mcp_webhook import (
    WEBHOOK_AUTH_NONE,
    async_register_webhook,
    async_unregister_webhook,
)

# The HTTP surface the boot page, the session endpoint and the proxy share.
_BASE = "/api/ha_mcp_tools/ui"
_SESSION = f"{_BASE}/session"
_APP = f"{_BASE}/app/"
_SECRET = "/private_abc"

_Responder = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass
class _Seen:
    """What the settings server received from the proxy."""

    method: str
    path_qs: str
    headers: dict[str, str]
    body: bytes


@dataclass
class _SettingsServer:
    """The in-process server's settings routes, on a real loopback listener."""

    server: TestServer
    seen: list[_Seen] = field(default_factory=list)
    respond: _Responder | None = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.seen.append(
            _Seen(
                request.method,
                request.path_qs,
                {key.lower(): value for key, value in request.headers.items()},
                await request.read(),
            )
        )
        if self.respond is not None:
            return await self.respond(request)
        return web.json_response({"ok": True})


@pytest.fixture
async def settings_server(
    aiohttp_server: Callable[[web.Application], Awaitable[TestServer]],
    socket_enabled: None,
) -> _SettingsServer:
    holder: list[_SettingsServer] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        return await holder[0].handle(request)

    app = web.Application()
    app.router.add_route("*", _SECRET + "/{tail:.*}", handler)
    server = _SettingsServer(await aiohttp_server(app))
    holder.append(server)
    return server


@pytest.fixture
async def http_ready(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})


@pytest.fixture
async def panel_views(hass: HomeAssistant, http_ready: None) -> None:
    await ui_panel.async_register_ui_panel(hass)


@pytest.fixture
async def running_server(
    hass: HomeAssistant, panel_views: None, settings_server: _SettingsServer
) -> AsyncIterator[_SettingsServer]:
    """The server is up: its forwarding config is stored the way setup stores it."""
    port = settings_server.server.port
    assert port is not None
    await _forward_to(hass, port)
    yield settings_server
    await async_unregister_webhook(hass)


async def _forward_to(hass: HomeAssistant, port: int) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={DATA_WEBHOOK_ID: "wh-ui-panel"})
    await async_register_webhook(
        hass,
        entry,
        port=port,
        secret_path=_SECRET,
        auth_mode=WEBHOOK_AUTH_NONE,
        register_endpoint=False,
    )


async def _mint(
    hass_client: ClientSessionGenerator, access_token: str | None = None
) -> dict[str, str]:
    """Sign in as the user and return the session cookie the browser would hold."""
    client = await hass_client(access_token) if access_token else await hass_client()
    resp = await client.post(_SESSION)
    assert resp.status == 200
    return {name: morsel.value for name, morsel in resp.cookies.items()}


async def _proxy_get(
    hass_client_no_auth: ClientSessionGenerator,
    cookies: dict[str, str] | None,
    headers: dict[str, str] | None = None,
) -> tuple[TestClient, int]:
    client = await hass_client_no_auth()
    resp = await client.get(_APP + "settings", cookies=cookies, headers=headers)
    return client, resp.status


@pytest.fixture
async def admin_cookies(
    running_server: _SettingsServer, hass_client: ClientSessionGenerator
) -> dict[str, str]:
    return await _mint(hass_client)
