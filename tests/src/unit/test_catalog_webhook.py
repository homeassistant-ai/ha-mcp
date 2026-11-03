"""The webhook serves ``[/readonly]/catalog/<mode>`` like the server endpoint."""

from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import FakeSession, FakeUpstream, make_request
from .test_mcp_webhook import (
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
    WEBHOOK_ID,
    _entry,
    _register_hass,
    _store_cfg,
    mw,
    readonly_webhook_modules,  # noqa: F401
)

CATALOG_ROUTE = "/api/webhook/{webhook_id}/catalog/{mode}"


def _request(suffix: str, webhook_id: str = WEBHOOK_ID):
    request = make_request()
    request.path = f"/api/webhook/{webhook_id}{suffix}"
    return request


async def test_catalog_suffixes_forward_to_the_matching_endpoint(monkeypatch):
    hass = _register_hass()
    session = FakeSession(upstream=FakeUpstream(status=200))
    monkeypatch.setattr(mw.aiohttp, "ClientSession", lambda **kw: session)
    await mw.async_register_webhook(
        hass,
        _entry(),
        port=9584,
        secret_path="/private_x",
        auth_mode=WEBHOOK_AUTH_NONE,
    )
    view = next(
        call.args[0]
        for call in hass.http.register_view.call_args_list
        if call.args[0].url == CATALOG_ROUTE
    )
    assert view.extra_urls == ["/api/webhook/{webhook_id}/readonly/catalog/{mode}"]
    for suffix in ("/catalog/full", "/catalog/search", "/readonly/catalog/search"):
        mode = suffix.rsplit("/", 1)[1]
        assert (await view.post(_request(suffix), WEBHOOK_ID, mode)).status == 200
        assert session.calls[-1]["url"] == f"http://127.0.0.1:9584/private_x{suffix}"
    calls = len(session.calls)
    for webhook_id, mode in ((WEBHOOK_ID, "bogus"), ("other-webhook", "search")):
        request = _request(f"/catalog/{mode}", webhook_id)
        assert (await view.post(request, webhook_id, mode)).status == 404
    assert len(session.calls) == calls
    await mw.async_unregister_webhook(hass)
    request = _request("/catalog/search")
    assert (await view.post(request, WEBHOOK_ID, "search")).status == 404


async def test_catalog_route_shared_across_installations(
    readonly_webhook_modules,  # noqa: F811
):
    embedded, proxy = readonly_webhook_modules
    hass = _register_hass()
    first, second = AsyncMock(), AsyncMock()
    embedded.register_readonly_webhook(hass, "embedded", first)
    proxy.register_readonly_webhook(hass, "proxy", second)
    view = next(
        call.args[0]
        for call in hass.http.register_view.call_args_list
        if call.args[0].url == CATALOG_ROUTE
    )
    request = _request("/readonly/catalog/full", "proxy")
    await view.get(request, "proxy", "full")
    second.assert_awaited_once_with(
        hass, "proxy", request, suffix="/readonly/catalog/full"
    )
    embedded.unregister_readonly_webhook(hass, "embedded")
    request = _request("/catalog/search", "embedded")
    assert (await view.post(request, "embedded", "search")).status == 404
    first.assert_not_awaited()


@pytest.mark.parametrize(
    "auth_mode", [WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY]
)
async def test_catalog_webhook_reuses_auth_gate(auth_mode, monkeypatch):
    from custom_components.ha_mcp_tools.readonly_webhook import CatalogWebhookView

    hass = _register_hass()
    session = FakeSession(upstream=FakeUpstream(status=200))
    _store_cfg(hass, session=session, auth_mode=auth_mode)
    mw.register_readonly_webhook(hass, WEBHOOK_ID, mw._async_handle_webhook)
    view = CatalogWebhookView(hass)
    rejection = mw.web.Response(status=401)
    monkeypatch.setattr(mw, "_check_webhook_auth", AsyncMock(return_value=rejection))
    assert (
        await view.post(_request("/catalog/search"), WEBHOOK_ID, "search") is rejection
    )
    assert session.calls == []
