"""What the webhook relays to the in-process server and what it hands back (#1527).

The component's own webhook is registered on a real ``hass``; every request goes
through Core's HTTP server and the relay forwards to a real loopback upstream.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

import custom_components.ha_mcp_tools.mcp_webhook as mw
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK,
    DOMAIN,
    WEBHOOK_AUTH_NONE,
)

from ._garbled_upstream import garbled_upstream
from ._mcp_webhook_core import (
    SECRET_PATH,
    WEBHOOK_PATH,
    Register,
    Upstream,
)
from ._mcp_webhook_core import (
    register as register,
)
from ._mcp_webhook_core import (
    upstream as upstream,
)

SSE = {"Content-Type": "text/event-stream"}


async def test_post_reaches_the_server_with_its_method_body_and_path(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()
    body = b'{"jsonrpc":"2.0","method":"initialize"}'

    resp = await client.post(
        WEBHOOK_PATH, data=body, headers={"Content-Type": "application/json"}
    )

    assert resp.status == 200
    assert await resp.json() == {"ok": 1}
    [seen] = upstream.requests
    assert (seen.method, seen.path, seen.body) == ("POST", SECRET_PATH, body)
    # A client must not buffer or re-encode the relayed reply.
    assert resp.headers["Cache-Control"] == "no-cache, no-transform"
    assert resp.headers["Content-Encoding"] == "identity"


async def test_get_reaches_the_server_as_a_get(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()

    resp = await client.get(WEBHOOK_PATH)

    assert resp.status == 200
    [seen] = upstream.requests
    assert (seen.method, seen.body) == ("GET", b"")


async def test_caller_credentials_and_host_never_reach_the_server(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """The server acts with its own token: a caller's cookie or bearer must not
    leak to it, while the headers MCP needs pass through untouched."""
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()

    await client.post(
        WEBHOOK_PATH,
        data=b"{}",
        headers={
            "Host": "example.nabu.casa",
            "Cookie": "session=secret",
            "Authorization": "Bearer should-not-forward",
            "Mcp-Session-Id": "sess-42",
            "Content-Type": "application/json",
            "X-Custom": "keep-me",
        },
    )

    [seen] = upstream.requests
    forwarded = {name.lower(): value for name, value in seen.headers.items()}
    assert forwarded["host"] != "example.nabu.casa"
    assert "cookie" not in forwarded
    assert "authorization" not in forwarded
    assert forwarded["mcp-session-id"] == "sess-42"
    assert forwarded["content-type"] == "application/json"
    assert forwarded["x-custom"] == "keep-me"


async def test_none_mode_never_challenges_a_forged_bearer(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """#1969: none mode advertises an auto-approve authorization server, but the
    secret webhook URL is the credential, so the forwarder must never answer 401
    to the cosmetic OAuth token a client sends."""
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()

    resp = await client.post(
        WEBHOOK_PATH, data=b"{}", headers={"Authorization": "Bearer forged-or-cosmetic"}
    )

    assert resp.status == 200
    assert len(upstream.requests) == 1


async def test_session_id_the_server_assigns_reaches_the_client(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    async def assign_session(request: web.Request) -> web.StreamResponse:
        return web.json_response({}, headers={"Mcp-Session-Id": "srv-77"})

    upstream.handler = assign_session
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()

    resp = await client.post(WEBHOOK_PATH, data=b"{}")

    assert resp.headers["Mcp-Session-Id"] == "srv-77"


async def test_html_from_the_server_is_relabelled_so_a_browser_cannot_run_it(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    async def html(request: web.Request) -> web.StreamResponse:
        return web.Response(
            body=b"<script>evil()</script>", content_type="text/html", charset="utf-8"
        )

    upstream.handler = html
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()

    resp = await client.post(WEBHOOK_PATH, data=b"{}")

    assert resp.headers["Content-Type"] == "application/json"


async def test_plain_text_landing_page_stays_readable_through_the_webhook(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """A browser that GETs the endpoint is shown the server's plain-text 405;
    relabelling it as JSON would render it as a mangled blob."""

    async def landing(request: web.Request) -> web.StreamResponse:
        return web.Response(
            status=405,
            body=b"HA-MCP server is up and running!",
            content_type="text/plain",
            charset="utf-8",
        )

    upstream.handler = landing
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()

    resp = await client.get(WEBHOOK_PATH)

    assert resp.status == 405
    assert resp.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert await resp.read() == b"HA-MCP server is up and running!"


async def test_event_stream_reaches_the_client_while_the_server_still_writes(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """A buffered relay would hold the first event until the stream ends, which
    never happens for a long-lived MCP stream."""
    first, second = b"event: message\ndata: 1\n\n", b"data: 2\n\n"
    release = asyncio.Event()

    async def stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={**SSE, "Mcp-Session-Id": "stream-sess"})
        await response.prepare(request)
        await response.write(first)
        await release.wait()
        await response.write(second)
        await response.write_eof()
        return response

    upstream.handler = stream
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()

    resp = await client.post(WEBHOOK_PATH, data=b"{}")
    try:
        assert resp.headers["Content-Type"] == "text/event-stream"
        assert resp.headers["X-Accel-Buffering"] == "no"
        assert resp.headers["Content-Encoding"] == "identity"
        assert resp.headers["Cache-Control"] == "no-cache, no-transform"
        assert resp.headers["Mcp-Session-Id"] == "stream-sess"
        assert await asyncio.wait_for(resp.content.readany(), 5) == first
    finally:
        release.set()
    assert await asyncio.wait_for(resp.read(), 5) == second


async def test_stream_cut_by_the_server_is_logged_and_ended_cleanly(
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once the 200 is sent a dropped upstream cannot become a 502: the client
    gets the bytes that arrived and a finished stream, and the log says how far
    it got."""
    chunk = b"data: 1\n\n"

    async def drop(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers=SSE)
        await response.prepare(request)
        await response.write(chunk)
        assert request.transport is not None
        request.transport.abort()
        return response

    upstream.handler = drop
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()

    resp = await client.post(WEBHOOK_PATH, data=b"{}")

    assert resp.status == 200
    assert await resp.read() == chunk
    assert f"dropped mid-stream after {len(chunk)} bytes" in caplog.text


async def test_failed_relay_is_a_502_that_names_no_secret_path(
    register: Register,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The relay's error quotes the target URL, whose path is the credential;
    neither the reply nor the log may repeat it."""
    async with garbled_upstream() as port:
        await register(WEBHOOK_AUTH_NONE, port=port)
        client = await hass_client_no_auth()

        resp = await client.post(WEBHOOK_PATH, data=b"{}")

    assert resp.status == 502
    assert "upstream request failed" in caplog.text
    assert SECRET_PATH not in caplog.text
    assert SECRET_PATH not in await resp.text()


async def test_unexpected_relay_error_is_a_500_not_a_crash(
    register: Register,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mw, "buffered_response", AsyncMock(side_effect=RuntimeError("boom"))
    )
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()

    resp = await client.post(WEBHOOK_PATH, data=b"{}")

    assert resp.status == 500


async def test_relay_is_bounded_by_idleness_never_by_elapsed_time(
    hass: HomeAssistant, register: Register
) -> None:
    """An MCP response stream may stay open indefinitely (the upcoming spec's
    ``subscriptions/listen``); a wall-clock bound would cut a healthy one, while
    an idle bound and a finite connect bound still stop a dead peer and a pool
    exhausted by long-lived streams."""
    await register(WEBHOOK_AUTH_NONE)
    session: aiohttp.ClientSession = hass.data[DOMAIN][DATA_WEBHOOK]["session"]

    timeout = session.timeout
    assert timeout.total is None
    assert timeout.sock_read is not None
    assert timeout.sock_connect is not None
    assert timeout.connect is not None
