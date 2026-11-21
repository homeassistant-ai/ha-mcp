"""The webhook's OAuth views on Core's own HTTP app, with real Core behind them.

``open_oauth_surface`` registers the webhook the way the component does
(``async_register_webhook``), serves Core's HTTP app, and hands the test a
client for it. In ha_auth mode the views forward to Core's ``/auth/token`` and
``/auth/revoke``, which Core serves from its ``auth`` component: the test gets
codes and refresh tokens from Core and reads what Core did with them.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient
from homeassistant.auth.models import Credentials
from homeassistant.components.auth import create_auth_code
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, MockUser
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.ha_mcp_tools import oauth_ha_auth
from custom_components.ha_mcp_tools.const import OAUTH_BASE
from custom_components.ha_mcp_tools.mcp_webhook import (
    async_register_webhook,
    async_unregister_webhook,
)

from ._core_client import free_port
from ._server_entry import add_server_entry

AUTHORIZE_URL = f"{OAUTH_BASE}/authorize"
TOKEN_URL = f"{OAUTH_BASE}/token"
REVOKE_URL = f"{OAUTH_BASE}/revoke"
DISCOVERY_URL = f"{OAUTH_BASE}/authorization-server"

CLAUDE_CLIENT_ID = "https://claude.ai/api/mcp/client_metadata"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
HOST = "ha.example.com"
# The public address a reverse proxy presents: the issuer is built from it.
PUBLIC_HEADERS = {"Host": HOST, "X-Forwarded-Proto": "https"}

DCR_KEY = b"d" * 32
LEGACY_CLIENT_ID = "hamcp-test-client-id-123"
LEGACY_CLIENT_SECRET = "hamcp-test-secret"


def pkce_pair() -> tuple[str, str]:
    """A valid (code_verifier, code_challenge) pair per RFC 7636 S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_query(**overrides: str) -> dict[str, str]:
    """A valid none-mode authorize query for the Claude callback."""
    _, challenge = pkce_pair()
    params = {
        "response_type": "code",
        "client_id": "anything",
        "redirect_uri": CLAUDE_REDIRECT,
        "state": "st-1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    params.update(overrides)
    return params


def parse_location(location: str) -> tuple[str, dict[str, str]]:
    """Split a redirect's ``Location`` into its URL without query and the query."""
    parsed = urlparse(location)
    flat = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}", flat


def serve_cimd_documents(
    monkeypatch: pytest.MonkeyPatch, documents: dict[str, list[str]]
) -> list[ClientSession]:
    """Answer every client metadata lookup from ``documents`` (no document for
    the rest), so no test reaches the network; the sessions asked through are
    appended to the returned list."""
    sessions: list[ClientSession] = []

    async def fetch(session: ClientSession, client_id: str) -> list[str] | None:
        sessions.append(session)
        return documents.get(client_id)

    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch)
    return sessions


async def register_webhook(
    hass: HomeAssistant, entry: MockConfigEntry, mode: str, dcr_key: bytes | None
) -> None:
    """Register the webhook in ``mode``, as the entry's bring-up does."""
    await async_register_webhook(
        hass,
        entry,
        port=1,
        secret_path="/private_x",
        auth_mode=mode,
        oauth_client_id=LEGACY_CLIENT_ID,
        oauth_client_secret=LEGACY_CLIENT_SECRET,
        oauth_signing_key=secrets.token_bytes(32).hex(),
        dcr_signing_key=None if dcr_key is None else dcr_key.hex(),
    )


@dataclass
class OAuthSurface:
    """One registered webhook, the client for Core's app, and Core's sign-in."""

    hass: HomeAssistant
    client: TestClient
    entry: MockConfigEntry
    user: MockUser
    credential: Credentials
    dcr_key: bytes | None

    def core_code(self, client_id: str) -> str:
        """An authorization code Core issued for ``client_id``."""
        return create_auth_code(self.hass, client_id, self.credential)

    async def core_refresh_token(self, client_id: str) -> str:
        """A refresh token Core minted for ``client_id``."""
        token = await self.hass.auth.async_create_refresh_token(
            self.user, client_id, credential=self.credential
        )
        return token.token

    def client_id_of(self, access_token: str) -> str | None:
        """The client_id Core bound ``access_token``'s grant to."""
        refresh_token = self.hass.auth.async_validate_access_token(access_token)
        return None if refresh_token is None else refresh_token.client_id

    def core_knows(self, refresh_token: str) -> bool:
        """Whether Core still honours ``refresh_token``."""
        return (
            self.hass.auth.async_get_refresh_token_by_token(refresh_token) is not None
        )

    async def switch_mode(self, mode: str) -> None:
        """Unload and register the webhook again in ``mode``, as a reload does."""
        await async_unregister_webhook(self.hass)
        await register_webhook(self.hass, self.entry, mode, self.dcr_key)

    async def exchange_code(
        self, client_id: str, redirect_uri: str, core_client_id: str | None = None
    ) -> dict[str, Any]:
        """Redeem a Core code for ``core_client_id`` (default: ``client_id``)."""
        resp = await self.client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": self.core_code(core_client_id or client_id),
                "client_id": client_id,
                "redirect_uri": redirect_uri,
            },
        )
        assert resp.status == 200, await resp.text()
        body: dict[str, Any] = await resp.json()
        return body

    def cut_off_core(self) -> None:
        """Make Core's own listener refuse the connections the forward opens."""
        assert self.hass.config.api is not None
        self.hass.config.api.port = free_port()


OpenSurface = Callable[..., Awaitable[OAuthSurface]]


@pytest.fixture
async def open_oauth_surface(
    hass: HomeAssistant,
    aiohttp_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
    hass_admin_credential: Credentials,
    hass_access_token: str,
    socket_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[OpenSurface]:
    """Register the webhook in a mode and serve Core's HTTP app for the test.

    ``hass_access_token`` links the admin credential to the admin user, so a
    code issued for that credential redeems into a real user. No client
    publishes a metadata document until the test serves one.
    """
    serve_cimd_documents(monkeypatch, {})

    async def open_surface(
        mode: str,
        *,
        dcr_key: bytes | None = DCR_KEY,
        options: dict[str, Any] | None = None,
    ) -> OAuthSurface:
        assert await async_setup_component(hass, "webhook", {})
        assert await async_setup_component(hass, "auth", {})
        entry = add_server_entry(hass, options=options)
        await register_webhook(hass, entry, mode, dcr_key)
        client = await aiohttp_client(hass.http.app)
        # Core records the address its listener serves on; here that is the
        # test server, which is where the ha_auth forward must go.
        assert hass.config.api is not None
        hass.config.api.port = client.port
        return OAuthSurface(
            hass=hass,
            client=client,
            entry=entry,
            user=hass_admin_user,
            credential=hass_admin_credential,
            dcr_key=dcr_key,
        )

    yield open_surface
    await async_unregister_webhook(hass)
