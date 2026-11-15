"""The legacy OAuth surface on a real ``hass``, and the values its tests share.

``legacy_webhook`` registers the webhook through the component's own entry
point, so Core's HTTP server serves the root ``/authorize`` and ``/token``
views; ``make_provider`` builds a provider for the token and code tests that
need no server.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

import custom_components.ha_mcp_tools.mcp_webhook as mw
from custom_components.ha_mcp_tools import oauth_legacy
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK,
    DATA_WEBHOOK_ID,
    DOMAIN,
    OAUTH_BASE,
    WEBHOOK_AUTH_LEGACY,
)

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
REDIRECT_URI = "https://client.example.com/cb"
HOST = "ha.example.com"
EXPECTED_ISSUER = f"https://{HOST}{OAUTH_BASE}"
PUBLIC_HEADERS = {"Host": HOST, "X-Forwarded-Proto": "https"}
WEBHOOK_ID = "mcp_0123456789abcdef0123456789abcdef"


def make_provider(
    *,
    client_id: str = CLIENT_ID,
    client_secret: str = CLIENT_SECRET,
    signing_key: bytes | None = None,
) -> oauth_legacy.LegacyOAuthProvider:
    """A provider for which legacy is the live mode."""
    key = signing_key if signing_key is not None else secrets.token_bytes(32)
    return oauth_legacy.LegacyOAuthProvider(
        client_id, client_secret, key, lambda: WEBHOOK_AUTH_LEGACY
    )


def challenge_for(verifier: str) -> str:
    """The S256 challenge for ``verifier``, whatever its shape."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """A valid ``(code_verifier, code_challenge)`` pair per RFC 7636 S256."""
    verifier = secrets.token_urlsafe(64)
    return verifier, challenge_for(verifier)


def authorize_query(**overrides: str) -> dict[str, str]:
    """The query of a well-formed ``GET /authorize``, with ``overrides`` applied."""
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": "s1",
        "code_challenge": pkce_pair()[1],
        "code_challenge_method": "S256",
        "response_type": "code",
    }
    params.update(overrides)
    return params


def approve_form(challenge: str, **overrides: str) -> dict[str, str]:
    """The consent form a browser posts when the user clicks Allow."""
    form = {
        "action": "approve",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": "xyz",
        "code_challenge": challenge,
    }
    form.update(overrides)
    return form


def query_param(url: str, key: str) -> str:
    """One query parameter of a redirect ``Location``."""
    return parse_qs(urlparse(url).query)[key][0]


@asynccontextmanager
async def legacy_webhook(
    hass: HomeAssistant,
    *,
    client_id: str = CLIENT_ID,
    client_secret: str = CLIENT_SECRET,
) -> AsyncIterator[oauth_legacy.LegacyOAuthProvider]:
    """Register the webhook in legacy mode; yield the provider that serves it."""
    assert await async_setup_component(hass, "webhook", {})
    entry = MockConfigEntry(domain=DOMAIN, data={DATA_WEBHOOK_ID: WEBHOOK_ID})
    await mw.async_register_webhook(
        hass,
        entry,
        port=9584,
        secret_path="/private_x",
        auth_mode=WEBHOOK_AUTH_LEGACY,
        oauth_client_id=client_id,
        oauth_client_secret=client_secret,
        oauth_signing_key=secrets.token_hex(32),
    )
    provider = hass.data[DOMAIN][DATA_WEBHOOK]["oauth_provider"]
    assert isinstance(provider, oauth_legacy.LegacyOAuthProvider)
    try:
        yield provider
    finally:
        await mw.async_unregister_webhook(hass)
