"""Nabu Casa cloudhook relay through the proxy (#2696).

Home Assistant Cloud hands a cloudhook call to Core's webhook component as a
``MockRequest`` (no ``read()``) and sends the cloud only the serialized
response, so the proxy reads the body via ``content`` and buffers an SSE reply
instead of streaming it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.util.aiohttp import MockRequest, serialize_response

from ._proxy import Upstream, start_proxy
from ._relay import LEGACY_OAUTH, WEBHOOK_ID, relay_config
from ._variants import Variant

pytestmark = pytest.mark.usefixtures("component")

SSE_REPLY = b"event: message\ndata: {}\n\n"


async def relay_through_cloud(
    hass: HomeAssistant, body: bytes, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """What Home Assistant Cloud does with a cloudhook call, and what it sends
    back to the cloud."""
    request = MockRequest(
        content=body, mock_source="cloud", method="POST", headers=headers
    )
    response = await webhook.async_handle_webhook(hass, WEBHOOK_ID, request)
    return serialize_response(response)


async def test_cloudhook_relays_the_body_and_buffers_an_sse_reply(
    hass: HomeAssistant, variant: Variant, fs_root: Path, upstream: Upstream
) -> None:
    """A connector on the cloudhook URL gets ha-mcp's whole SSE reply as one
    body, the only form the cloud can carry back."""

    async def answer_with_sse(request: web.Request) -> web.Response:
        return web.Response(body=SSE_REPLY, content_type="text/event-stream")

    upstream.handler = answer_with_sse
    await start_proxy(hass, variant, fs_root, relay_config(upstream))
    body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'

    reply = await relay_through_cloud(
        hass, body, headers={"Content-Type": "application/json"}
    )

    assert [r.body for r in upstream.requests] == [body]
    assert reply["status"] == 200
    assert reply["body"] == SSE_REPLY.decode()
    assert reply["headers"]["Content-Type"] == "text/event-stream"


async def test_cloudhook_reply_that_never_ends_is_cut_off_with_504(
    hass: HomeAssistant,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    component: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscription stream never finishes, and a cloudhook can only send a
    finished body: the proxy gives up and says so instead of hanging."""
    release = asyncio.Event()

    async def stream_forever(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(SSE_REPLY)
        await release.wait()
        return response

    upstream.handler = stream_forever
    monkeypatch.setattr(component.cloudhook, "CLOUDHOOK_REPLY_SECONDS", 0.05)
    await start_proxy(hass, variant, fs_root, relay_config(upstream))

    try:
        reply = await relay_through_cloud(hass, b"{}")
    finally:
        release.set()

    assert reply["status"] == 504


async def test_oauth_gated_cloudhook_is_refused_with_the_reason(
    hass: HomeAssistant, variant: Variant, fs_root: Path, upstream: Upstream
) -> None:
    """The cloud relays only Content-Type back, so an OAuth challenge could
    never reach the client; the user gets told why instead of a bare 401."""
    config = relay_config(upstream, oauth=LEGACY_OAUTH)
    await start_proxy(hass, variant, fs_root, config)

    reply = await relay_through_cloud(hass, b"{}", headers={"Host": "hooks.nabu.casa"})

    assert reply["status"] == 400
    assert "OAuth" in reply["body"]
    assert upstream.requests == []
