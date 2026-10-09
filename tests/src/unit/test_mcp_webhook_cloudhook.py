"""Nabu Casa cloudhook relay through the in-process server's webhook (#2696).

Settings → Home Assistant Cloud → Webhooks relays a cloudhook in-process as
HA's ``MockRequest``, which has no ``read()`` and no transport, and the relay
returns only ``response.body`` — so the forwarder must read the body via
``content`` and hand back the SSE reply buffered rather than streamed.
"""

from __future__ import annotations

import asyncio

import pytest

from ._embedded_stubs import FakeSession, FakeUpstream, install

install()

import custom_components.ha_mcp_tools.mcp_webhook as mw  # noqa: E402
from custom_components.ha_mcp_tools import cloudhook  # noqa: E402
from custom_components.ha_mcp_tools.const import WEBHOOK_AUTH_HA  # noqa: E402

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


async def test_cloudhook_reply_that_never_ends_is_cut_off_with_504(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A ``subscriptions/listen`` stream never ends; a cloudhook cannot carry it.
    upstream = FakeUpstream(status=200, headers={"Content-Type": "text/event-stream"})
    upstream.read = lambda: asyncio.sleep(5)  # type: ignore[method-assign]
    session = FakeSession(upstream=upstream)
    hass = _make_hass()
    _store_cfg(hass, session=session)
    monkeypatch.setattr(cloudhook, "REPLY_SECONDS", 0.01)

    request = mw.MockRequest(content=b"{}", mock_source="cloud", method="POST")
    resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, request)

    assert resp.status == 504
    assert "did not finish within" in caplog.text


def _oauth_gated_hass(external_url: str | None) -> tuple:
    session = FakeSession(upstream=FakeUpstream(status=200))
    hass = _make_hass(validate_result=None)
    _store_cfg(
        hass,
        session=session,
        auth_mode=WEBHOOK_AUTH_HA,
        resource_server=mw.ResourceServer(hass, WEBHOOK_ID),
    )
    hass.data[mw.DOMAIN][mw.DATA_WEBHOOK]["external_url"] = external_url
    return hass, session


async def test_cloudhook_without_bearer_and_without_external_url_is_refused() -> None:
    # hooks.nabu.casa cannot serve the discovery documents, so a 401 pointing
    # there would send the client nowhere: say what is needed instead.
    hass, session = _oauth_gated_hass(external_url=None)
    request = mw.MockRequest(
        content=b"{}",
        mock_source="cloud",
        method="POST",
        headers={"Host": "hooks.nabu.casa"},
    )
    resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, request)

    assert resp.status == 400
    assert "External URL" in resp.text
    assert session.calls == []


async def test_cloudhook_without_bearer_gets_the_401_on_the_external_url() -> None:
    hass, session = _oauth_gated_hass(external_url="https://ha.example.com")
    request = mw.MockRequest(
        content=b"{}",
        mock_source="cloud",
        method="POST",
        headers={"Host": "hooks.nabu.casa"},
    )
    resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, request)

    assert resp.status == 401
    assert session.calls == []
    assert (
        f"https://ha.example.com/.well-known/oauth-protected-resource/api/webhook/{WEBHOOK_ID}"
        in resp.headers["WWW-Authenticate"]
    )
