"""Read-only webhook discovery preserves base metadata and shared auth."""

import pytest

from ._embedded_stubs import make_request
from .test_mcp_webhook import (
    DATA_WEBHOOK,
    DOMAIN,
    OAUTH_BASE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
    WEBHOOK_ID,
    _live_hass,
    _none_live_hass,
    mw,
)


class TestReadonlyDiscovery:
    @pytest.mark.parametrize("webhook_id", [WEBHOOK_ID, "readonly"])
    @pytest.mark.parametrize("suffix", ["", "/readonly"])
    @pytest.mark.parametrize(
        "mode", [WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY, WEBHOOK_AUTH_NONE]
    )
    async def test_readonly_resource_identity(self, suffix, mode, webhook_id):
        hass = (
            _none_live_hass(webhook_id)
            if mode == WEBHOOK_AUTH_NONE
            else _live_hass(mode, webhook_id)
        )
        view = mw._WellKnownProtectedResourceView(hass)
        path = f"/.well-known/oauth-protected-resource/api/webhook/{webhook_id}{suffix}"
        request = make_request(headers={"Host": "abc.ui.nabu.casa"})
        request.path = path
        routes = [view.url, *getattr(view, "extra_urls", [])]
        assert path in [route.format(webhook_id=webhook_id) for route in routes]
        response = await view.get(request, webhook_id=webhook_id)
        assert response.status == 200
        assert response.json_body == {
            "resource": f"https://abc.ui.nabu.casa/api/webhook/{webhook_id}{suffix}",
            "authorization_servers": [f"https://abc.ui.nabu.casa{OAUTH_BASE}"],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/homeassistant-ai/ha-mcp",
        }
        assert (await view.get(request, webhook_id="stale_id")).status == 404
        hass.data.clear()
        assert (await view.get(request, webhook_id=webhook_id)).status == 404

    @pytest.mark.parametrize("suffix", ["", "/readonly"])
    @pytest.mark.parametrize("mode", [WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY])
    async def test_readonly_discovery_identity_in_auth_challenge(self, suffix, mode):
        hass = _live_hass(mode)
        request = make_request(headers={"Host": "abc.ui.nabu.casa"})
        request.path = f"/api/webhook/{WEBHOOK_ID}{suffix}"
        response = await mw._check_webhook_auth(
            request, hass.data[DOMAIN][DATA_WEBHOOK]
        )
        assert response.status == 401
        assert response.headers["WWW-Authenticate"] == (
            'Bearer realm="HA-MCP", resource_metadata="https://abc.ui.nabu.casa'
            f'/.well-known/oauth-protected-resource/api/webhook/{WEBHOOK_ID}{suffix}"'
        )
