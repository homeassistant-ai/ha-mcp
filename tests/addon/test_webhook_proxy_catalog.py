"""Webhook Proxy ``[/readonly]/catalog/<mode>`` forwarding and auth."""

import importlib
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from . import test_webhook_proxy as proxy
from .test_webhook_proxy import _webhook_proxy_variant  # noqa: F401

CATALOG_ROUTE = "/api/webhook/{webhook_id}/catalog/{mode}"


def catalog_webhook_supported() -> bool:
    """Feature-detect the catalog webhook in the CURRENT flavor's source, so
    stable skips until promotion while a dev regression fails."""
    source = os.path.join(
        proxy.PROXY_ADDON_DIR, proxy.CURRENT["component"], "readonly_webhook.py"
    )
    with open(source, encoding="utf-8") as handle:
        return "CatalogWebhookView" in handle.read()


def _catalog_view(mod, hass):
    mod.register_readonly_webhook(hass, "mcp_test", mod._handle_webhook)
    view = next(
        (
            call.args[0]
            for call in hass.http.register_view.call_args_list
            if call.args[0].url == CATALOG_ROUTE
        ),
        None,
    )
    if view is None and not catalog_webhook_supported():
        pytest.skip("catalog webhook has not been promoted to this flavor")
    assert view is not None, "flavor ships the catalog webhook but never routes it"
    return view


def _request(path):
    request = MagicMock()
    request.headers = {}
    request.method = "POST"
    request.path = path
    request.read = AsyncMock(return_value=b"request-body")
    return request


def _hass(**data):
    hass = MagicMock()
    hass.data = {
        proxy._import_mcp_proxy().DOMAIN: {
            "target_url": "http://localhost/private_test",
            "webhook_id": "mcp_test",
            "session": MagicMock(),
            **data,
        }
    }
    return hass


@pytest.mark.parametrize("suffix", ["/catalog/search", "/readonly/catalog/full"])
async def test_catalog_suffix_forwards_to_the_matching_endpoint(suffix):
    mod = proxy._import_mcp_proxy()
    hass = _hass()
    view = _catalog_view(mod, hass)
    session = hass.data[mod.DOMAIN]["session"]
    # Stop at the wire boundary after observing the real forwarding arguments.
    session.request.side_effect = mod.aiohttp.ClientError("test upstream stopped")
    mode = suffix.rsplit("/", 1)[1]
    await view.post(_request(f"/api/webhook/mcp_test{suffix}"), "mcp_test", mode)
    assert session.request.call_args.kwargs["url"] == (
        "http://localhost/private_test" + suffix
    )
    session.request.reset_mock()
    for webhook_id, mode in (("mcp_test", "bogus"), ("unrelated", "search")):
        request = _request(f"/api/webhook/{webhook_id}/catalog/{mode}")
        await view.post(request, webhook_id, mode)
    session.request.assert_not_called()


@pytest.mark.parametrize("mode", ["ha_auth", "legacy"])
async def test_catalog_keeps_bearer_validation(mode):
    mod = proxy._import_mcp_proxy()
    provider = MagicMock()
    provider.validate_bearer.return_value = False
    provider.validate_request_detailed = AsyncMock(return_value=(False, "invalid"))
    hass = _hass(oauth=provider, oauth_mode=mode)
    view = _catalog_view(mod, hass)
    request = _request("/api/webhook/mcp_test/catalog/search")
    rejection = object()
    oauth = importlib.import_module(mod.__name__ + ".oauth")
    with patch.object(oauth, "build_unauthorized_response", return_value=rejection):
        assert await view.post(request, "mcp_test", "search") is rejection
    hass.data[mod.DOMAIN]["session"].request.assert_not_called()
    request.read.assert_not_awaited()
