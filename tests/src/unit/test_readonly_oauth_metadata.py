"""Read-only webhook discovery preserves base metadata and shared auth.

The webhook is registered through the component's own entry point on a real
``hass`` and every request goes through Core's HTTP server and router.
"""

import secrets
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import pytest
from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator

import custom_components.ha_mcp_tools.mcp_webhook as mw
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK_ID,
    DOMAIN,
    OAUTH_BASE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)

WEBHOOK_ID = "mcp_0123456789abcdef0123456789abcdef"
SUFFIXES = ["", "/readonly", "/catalog/full", "/readonly/catalog/search"]
PUBLIC_HEADERS = {"Host": "abc.ui.nabu.casa", "X-Forwarded-Proto": "https"}

Registered = Callable[[str, str], Coroutine[Any, Any, TestClient]]


@pytest.fixture
async def registered(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> AsyncIterator[Registered]:
    """Register the webhook on the real ``hass``; return a client for it."""
    assert await async_setup_component(hass, "webhook", {})

    async def register(mode: str, webhook_id: str) -> TestClient:
        entry = MockConfigEntry(domain=DOMAIN, data={DATA_WEBHOOK_ID: webhook_id})
        await mw.async_register_webhook(
            hass,
            entry,
            port=9584,
            secret_path="/private_x",
            auth_mode=mode,
            oauth_client_id="cid",
            oauth_client_secret="secret",
            oauth_signing_key=secrets.token_hex(32),
        )
        return await hass_client_no_auth()

    try:
        yield register
    finally:
        await mw.async_unregister_webhook(hass)


class TestReadonlyDiscovery:
    @pytest.mark.parametrize("webhook_id", [WEBHOOK_ID, "readonly"])
    @pytest.mark.parametrize("suffix", SUFFIXES)
    @pytest.mark.parametrize(
        "mode", [WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY, WEBHOOK_AUTH_NONE]
    )
    async def test_readonly_resource_identity(
        self,
        hass: HomeAssistant,
        registered: Registered,
        suffix: str,
        mode: str,
        webhook_id: str,
    ) -> None:
        client = await registered(mode, webhook_id)
        path = f"/.well-known/oauth-protected-resource/api/webhook/{webhook_id}{suffix}"
        response = await client.get(path, headers=PUBLIC_HEADERS)
        assert response.status == 200
        assert await response.json() == {
            "resource": f"https://abc.ui.nabu.casa/api/webhook/{webhook_id}{suffix}",
            "authorization_servers": [f"https://abc.ui.nabu.casa{OAUTH_BASE}"],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/homeassistant-ai/ha-mcp",
        }
        stale = await client.get(
            f"/.well-known/oauth-protected-resource/api/webhook/stale_id{suffix}",
            headers=PUBLIC_HEADERS,
        )
        assert stale.status == 404
        await mw.async_unregister_webhook(hass)
        assert (await client.get(path, headers=PUBLIC_HEADERS)).status == 404

    @pytest.mark.parametrize("suffix", [*SUFFIXES, "/catalog/bogus"])
    @pytest.mark.parametrize("mode", [WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY])
    async def test_readonly_discovery_identity_in_auth_challenge(
        self, registered: Registered, suffix: str, mode: str
    ) -> None:
        client = await registered(mode, WEBHOOK_ID)
        response = await client.post(
            f"/api/webhook/{WEBHOOK_ID}{suffix}", headers=PUBLIC_HEADERS
        )
        if suffix == "/catalog/bogus":
            # Core's router answers an unknown catalog mode before any auth gate,
            # so no challenge can name it.
            assert response.status == 404
            assert "WWW-Authenticate" not in response.headers
            return
        assert response.status == 401
        assert response.headers["WWW-Authenticate"] == (
            'Bearer realm="HA-MCP", resource_metadata="https://abc.ui.nabu.casa'
            "/.well-known/oauth-protected-resource/api/webhook/"
            f"{WEBHOOK_ID}{suffix}"
            '"'
        )
