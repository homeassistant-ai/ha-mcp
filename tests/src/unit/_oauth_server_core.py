"""The ``ha-mcp-oauth`` provider, signed in to over HTTP as a connector does.

Every step goes through the provider's own routes: dynamic registration,
``/authorize``, the consent form, ``/token`` and ``/revoke``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.applications import Starlette

from ha_mcp._vendor.fastmcp.server.auth.auth import AccessToken
from ha_mcp._vendor.mcp.server.auth.middleware.auth_context import auth_context_var
from ha_mcp._vendor.mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from ha_mcp.auth import HomeAssistantOAuthProvider

BASE_URL = "http://localhost:8086"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def query(url: str) -> dict[str, list[str]]:
    """The query parameters of a redirect's ``Location``."""
    return parse_qs(urlparse(url).query)


@asynccontextmanager
async def oauth_http(
    provider: HomeAssistantOAuthProvider,
) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client of the provider's routes, redirects left unfollowed."""
    app = Starlette(routes=provider.get_routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as http:
        yield http


@pytest.fixture
def oauth_provider() -> HomeAssistantOAuthProvider:
    """The provider as ``ha-mcp-oauth`` builds it, storing in the test's data dir."""
    return HomeAssistantOAuthProvider(base_url=BASE_URL)


@pytest.fixture
async def oauth_client(
    oauth_provider: HomeAssistantOAuthProvider,
) -> AsyncIterator[httpx.AsyncClient]:
    async with oauth_http(oauth_provider) as http:
        yield http


async def register(http: httpx.AsyncClient, **metadata: Any) -> httpx.Response:
    """Register a public PKCE client (as Claude.ai does) with ``metadata`` on top."""
    body: dict[str, Any] = {
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "connector",
    }
    return await http.post("/register", json=body | metadata)


@dataclass
class Connector:
    """A registered client signing in with PKCE."""

    http: httpx.AsyncClient
    client_id: str
    redirect_uri: str = CALLBACK
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(48))

    async def authorize(self, **params: str) -> httpx.Response:
        digest = hashlib.sha256(self.verifier.encode()).digest()
        query_params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": base64.urlsafe_b64encode(digest).decode().rstrip("="),
            "code_challenge_method": "S256",
            "state": "st-1",
            "scope": "homeassistant",
        }
        return await self.http.get("/authorize", params=query_params | params)

    async def open_consent(self) -> str:
        """Start a sign-in; the transaction id of the consent form it opens."""
        resp = await self.authorize()
        assert resp.status_code == 302, resp.text
        return query(resp.headers["location"])["txn_id"][0]

    async def submit(self, txn_id: str, **form: str) -> httpx.Response:
        return await self.http.post("/consent", data={"txn_id": txn_id} | form)

    async def code_for(self, ha_token: str) -> str:
        resp = await self.submit(await self.open_consent(), ha_token=ha_token)
        assert resp.status_code == 303, resp.text
        return query(resp.headers["location"])["code"][0]

    async def token(self, **form: str) -> httpx.Response:
        return await self.http.post("/token", data={"client_id": self.client_id} | form)

    async def sign_in(self, ha_token: str) -> dict[str, Any]:
        """The token response of a whole sign-in with ``ha_token``."""
        resp = await self.token(
            grant_type="authorization_code",
            code=await self.code_for(ha_token),
            redirect_uri=self.redirect_uri,
            code_verifier=self.verifier,
        )
        assert resp.status_code == 200, resp.text
        tokens: dict[str, Any] = resp.json()
        return tokens

    async def refresh(self, refresh_token: str) -> httpx.Response:
        return await self.token(grant_type="refresh_token", refresh_token=refresh_token)


async def connector(http: httpx.AsyncClient, **metadata: Any) -> Connector:
    """A client registered over DCR."""
    resp = await register(http, **metadata)
    assert resp.status_code == 201, resp.text
    info = resp.json()
    return Connector(http, info["client_id"], info["redirect_uris"][0])


async def issuer(http: httpx.AsyncClient) -> str:
    """The issuer the provider's metadata document advertises."""
    resp = await http.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200, resp.text
    served: str = resp.json()["issuer"]
    return served


@contextmanager
def signed_in(access_token: AccessToken) -> Iterator[None]:
    """Run as the request the SDK's auth middleware let through with this token."""
    reset = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)
