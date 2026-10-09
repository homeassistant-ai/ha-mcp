"""Nabu Casa cloudhook relay through the in-process server's webhook (#2696).

Settings → Home Assistant Cloud → Webhooks relays a cloudhook in-process as
HA's ``MockRequest``, which has no ``read()`` and no transport, and the relay
returns only ``response.body`` — so the forwarder must read the body via
``content`` and hand back the SSE reply buffered rather than streamed.
"""

from __future__ import annotations

from ._embedded_stubs import FakeSession, FakeUpstream, install

install()

import custom_components.ha_mcp_tools.mcp_webhook as mw  # noqa: E402

from .test_mcp_webhook import WEBHOOK_ID, _make_hass, _store_cfg  # noqa: E402


async def test_cloudhook_relay_reads_body_and_buffers_sse() -> None:
    chunks = [b"event: message\ndata: 1\n\n", b"data: 2\n\n"]
    upstream = FakeUpstream(
        status=200,
        headers={"Content-Type": "text/event-stream"},
        body=b"".join(chunks),
        chunks=chunks,
    )
    session = FakeSession(upstream=upstream)
    hass = _make_hass()
    _store_cfg(hass, session=session)

    body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    request = mw.MockRequest(
        content=body,
        mock_source="cloud",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, request)

    assert session.calls[0]["data"] == body
    assert isinstance(resp, mw.web.Response)
    assert resp.status == 200
    assert resp.body == b"".join(chunks)
    assert resp.headers["Content-Type"] == "text/event-stream"
