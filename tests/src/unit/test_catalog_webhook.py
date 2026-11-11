"""The webhook serves ``[/readonly]/catalog/<mode>`` like the server endpoint.

The webhook is registered through the component's own entry point on a real
``hass``; every request goes through Core's HTTP server and router, and the
relay forwards to a real loopback upstream.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import ANY, AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator
from starlette.types import Message, Receive, Scope, Send

import custom_components.ha_mcp_tools.mcp_webhook as mw
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK,
    DATA_WEBHOOK_ID,
    DOMAIN,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)

from ._readonly_webhook import readonly_webhook_modules as readonly_webhook_modules

WEBHOOK_ID = "mcp_0123456789abcdef0123456789abcdef"
SECRET_PATH = "/private_x"

Register = Callable[[str], Awaitable[None]]


@pytest.fixture
async def upstream(socket_enabled: None) -> AsyncIterator[tuple[TestServer, list[str]]]:
    """A loopback server standing in for the in-process MCP server.

    Returns the server and the request paths it was sent, in order.
    """
    seen: list[str] = []

    async def record(request: web.Request) -> web.Response:
        seen.append(request.path)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", record)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server, seen
    finally:
        await server.close()


@pytest.fixture
async def register(
    hass: HomeAssistant, upstream: tuple[TestServer, list[str]]
) -> AsyncIterator[Register]:
    """Register the real webhook in ``auth_mode``, relaying to ``upstream``."""
    assert await async_setup_component(hass, "webhook", {})
    server, _ = upstream
    assert server.port is not None
    port = server.port

    async def do_register(auth_mode: str) -> None:
        entry = MockConfigEntry(domain=DOMAIN, data={DATA_WEBHOOK_ID: WEBHOOK_ID})
        entry.add_to_hass(hass)
        await mw.async_register_webhook(
            hass,
            entry,
            port=port,
            secret_path=SECRET_PATH,
            auth_mode=auth_mode,
            oauth_client_id="cid",
            oauth_client_secret="secret",
            oauth_signing_key=secrets.token_hex(32),
        )

    try:
        yield do_register
    finally:
        await mw.async_unregister_webhook(hass)


async def test_catalog_suffixes_forward_to_the_matching_endpoint(
    hass: HomeAssistant,
    register: Register,
    upstream: tuple[TestServer, list[str]],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    from custom_components.ha_mcp_tools.readonly_webhook import CatalogWebhookView

    _, seen = upstream
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()
    assert CatalogWebhookView(hass).extra_urls == [
        "/api/webhook/{webhook_id}/readonly/catalog/{mode}"
    ]
    for suffix in ("/catalog/full", "/catalog/search", "/readonly/catalog/search"):
        resp = await client.post(f"/api/webhook/{WEBHOOK_ID}{suffix}", data=b"{}")
        assert resp.status == 200
        assert seen[-1] == f"{SECRET_PATH}{suffix}"
    calls = len(seen)
    for webhook_id, mode in ((WEBHOOK_ID, "bogus"), ("other-webhook", "search")):
        resp = await client.post(f"/api/webhook/{webhook_id}/catalog/{mode}")
        assert resp.status == 404
    assert len(seen) == calls
    await mw.async_unregister_webhook(hass)
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}/catalog/search")
    assert resp.status == 404


async def test_catalog_route_shared_across_installations(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    readonly_webhook_modules,
) -> None:
    embedded, proxy = readonly_webhook_modules
    assert await async_setup_component(hass, "http", {})
    first = AsyncMock(return_value=web.Response(status=200))
    second = AsyncMock(return_value=web.Response(status=200))
    embedded.register_readonly_webhook(hass, "embedded", first)
    proxy.register_readonly_webhook(hass, "proxy", second)
    client = await hass_client_no_auth()

    resp = await client.get("/api/webhook/proxy/readonly/catalog/full")

    assert resp.status == 200
    second.assert_awaited_once_with(hass, "proxy", ANY, suffix="/readonly/catalog/full")
    assert second.await_args is not None
    request: web.Request = second.await_args.args[2]
    assert request.path == "/api/webhook/proxy/readonly/catalog/full"
    embedded.unregister_readonly_webhook(hass, "embedded")
    resp = await client.post("/api/webhook/embedded/catalog/search")
    assert resp.status == 404
    first.assert_not_awaited()


@pytest.mark.parametrize("auth_mode", [WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY])
async def test_catalog_webhook_rejects_a_failed_bearer_with_its_metadata(
    hass: HomeAssistant,
    register: Register,
    upstream: tuple[TestServer, list[str]],
    hass_client_no_auth: ClientSessionGenerator,
    hass_access_token: str,
    auth_mode: str,
) -> None:
    """A catalog URL is gated like the base URL in each OAuth mode, and its 401
    points the client at that suffix's protected-resource document."""
    _, seen = upstream
    await register(auth_mode)
    if auth_mode == WEBHOOK_AUTH_HA:
        accepted = hass_access_token
    else:
        provider: Any = hass.data[DOMAIN][DATA_WEBHOOK]["oauth_provider"]
        accepted = provider.issue_access_token()
    client = await hass_client_no_auth()
    path = f"/api/webhook/{WEBHOOK_ID}/readonly/catalog/search"

    for token in ("forged.token", accepted):
        resp = await client.post(
            path,
            headers={"Host": "example.nabu.casa", "Authorization": f"Bearer {token}"},
        )
        if token == accepted:
            assert resp.status == 200
            assert seen[0].endswith("/readonly/catalog/search")
            continue
        assert resp.status == 401
        assert seen == []
        assert resp.headers["WWW-Authenticate"].endswith(
            f'/api/webhook/{WEBHOOK_ID}/readonly/catalog/search"'
        )


def test_unsuffixed_target_url_is_forwarded_unchanged(
    readonly_webhook_modules,
) -> None:
    for module in readonly_webhook_modules:
        url = "http://host/custom-mcp/?key=value"
        assert module.suffixed_url(url, "") == url
        assert (
            module.suffixed_url(url, "/catalog/search")
            == "http://host/custom-mcp/catalog/search?key=value"
        )


def test_read_only_route_cannot_lose_read_only_to_a_suffix(
    readonly_webhook_modules,
) -> None:
    """A handler called with ``read_only=True`` and a catalog suffix forwards to
    the read-only variant: read-only fails closed rather than being dropped."""
    for module in readonly_webhook_modules:
        url = "http://host/mcp"
        assert module.forward_url(url, read_only=True) == f"{url}/readonly"
        for suffix in ("/catalog/full", "/readonly/catalog/full"):
            assert (
                module.forward_url(url, suffix, read_only=True)
                == f"{url}/readonly/catalog/full"
            )
        assert module.forward_url(url, "/catalog/full") == f"{url}/catalog/full"


async def test_server_and_webhooks_accept_the_same_endpoint_suffixes() -> None:
    """A suffix the webhook forwards must be one the server routes, and back:
    the two grammars are defined separately and must not drift apart."""
    from custom_components.ha_mcp_tools.readonly_webhook import ENDPOINT_SUFFIXES
    from ha_mcp.http_transport import EndpointSelectors

    paths: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        paths.append(scope["path"])

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        pass

    selectors = EndpointSelectors(app, "/mcp")
    candidates = {
        *ENDPOINT_SUFFIXES,
        *(
            f"{prefix}/{rest}"
            for prefix in ("", "/readonly")
            for rest in ("catalog", "catalog/bogus", "catalog/full/x", "readonly", "x")
        ),
    }
    served: set[str] = set()
    for suffix in candidates:
        await selectors({"type": "http", "path": f"/mcp{suffix}"}, receive, send)
        # A selector suffix reaches the MCP app on its own endpoint path.
        if paths[-1] == "/mcp":
            served.add(suffix)
    assert served == set(ENDPOINT_SUFFIXES)
