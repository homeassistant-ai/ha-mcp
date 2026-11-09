"""The webhook serves ``[/readonly]/catalog/<mode>`` like the server endpoint."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._embedded_helpers import FakeSession, FakeUpstream, make_request
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


def _gate(auth_mode: str, *, accepts: bool) -> dict:
    """The provider ``auth_mode`` installs, answering ``accepts`` for any bearer."""
    if auth_mode == WEBHOOK_AUTH_HA:
        return {
            "resource_server": SimpleNamespace(
                validate_request=AsyncMock(return_value=accepts)
            )
        }
    if auth_mode == WEBHOOK_AUTH_LEGACY:
        return {"oauth_provider": SimpleNamespace(validate_bearer=lambda _r: accepts)}
    return {}


@pytest.mark.parametrize("auth_mode", [WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY])
async def test_catalog_webhook_rejects_a_failed_bearer_with_its_metadata(auth_mode):
    """A catalog URL is gated like the base URL in each OAuth mode, and its 401
    points the client at that suffix's protected-resource document."""
    from custom_components.ha_mcp_tools.readonly_webhook import CatalogWebhookView

    hass = _register_hass()
    for accepts in (False, True):
        session = FakeSession(upstream=FakeUpstream(status=200))
        _store_cfg(
            hass,
            session=session,
            auth_mode=auth_mode,
            **_gate(auth_mode, accepts=accepts),
        )
        mw.register_readonly_webhook(hass, WEBHOOK_ID, mw._async_handle_webhook)
        request = _request("/readonly/catalog/search")
        request.headers = {"Host": "example.nabu.casa"}
        resp = await CatalogWebhookView(hass).post(request, WEBHOOK_ID, "search")
        if accepts:
            assert resp.status == 200
            assert session.calls[0]["url"].endswith("/readonly/catalog/search")
            continue
        assert resp.status == 401
        assert session.calls == []
        assert resp.headers["WWW-Authenticate"].endswith(
            f'/api/webhook/{WEBHOOK_ID}/readonly/catalog/search"'
        )


def test_unsuffixed_target_url_is_forwarded_unchanged(
    readonly_webhook_modules,  # noqa: F811
):
    for module in readonly_webhook_modules:
        url = "http://host/custom-mcp/?key=value"
        assert module.suffixed_url(url, "") == url
        assert (
            module.suffixed_url(url, "/catalog/search")
            == "http://host/custom-mcp/catalog/search?key=value"
        )


def test_read_only_route_cannot_lose_read_only_to_a_suffix(
    readonly_webhook_modules,  # noqa: F811
):
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


def test_server_and_webhooks_accept_the_same_endpoint_suffixes():
    """A suffix the webhook forwards must be one the server routes, and back:
    the two grammars are defined separately and must not drift apart."""
    from custom_components.ha_mcp_tools.readonly_webhook import ENDPOINT_SUFFIXES
    from ha_mcp.http_transport import EndpointSelectors

    selectors = EndpointSelectors(AsyncMock(), "/mcp")
    candidates = {
        *ENDPOINT_SUFFIXES,
        *(
            f"{prefix}/{rest}"
            for prefix in ("", "/readonly")
            for rest in ("catalog", "catalog/bogus", "catalog/full/x", "readonly", "x")
        ),
    }
    served = {s for s in candidates if selectors._selectors(f"/mcp{s}") is not None}
    assert served == set(ENDPOINT_SUFFIXES)
