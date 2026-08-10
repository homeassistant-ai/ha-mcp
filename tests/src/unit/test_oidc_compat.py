"""Regression tests for ha-mcp's FastMCP 3.4.6 OIDC compatibility layer."""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastmcp.server.auth.oauth_proxy.models import (
    ProxyDCRClient,
    RefreshTokenMetadata,
)
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from ha_mcp.auth.oidc_compat import HaMcpOIDCProxy


@pytest.fixture
def oidc_proxy(monkeypatch: pytest.MonkeyPatch) -> HaMcpOIDCProxy:
    """Create a real proxy while replacing only external OIDC discovery."""
    configuration = OIDCConfiguration(
        issuer="https://idp.example.com",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token",
        jwks_uri="https://idp.example.com/jwks",
        response_types_supported=["code"],
        subject_types_supported=["public"],
        id_token_signing_alg_values_supported=["RS256"],
        scopes_supported=["openid", "profile", "email", "offline_access"],
    )
    monkeypatch.setattr(
        HaMcpOIDCProxy,
        "get_oidc_configuration",
        lambda self, config_url, strict, timeout_seconds: configuration,
    )
    return HaMcpOIDCProxy(
        config_url="https://idp.example.com/.well-known/openid-configuration",
        client_id="ha-mcp",
        client_secret="test-client-secret",
        base_url="https://mcp.example.com",
        required_scopes=["openid"],
        client_storage=MemoryStore(),
        jwt_signing_key="test-jwt-signing-key",
        require_authorization_consent="external",
        enable_cimd=False,
    )


def _registration_request(scope: str | None) -> Request:
    """Build an MCP dynamic-client-registration request with an optional scope."""
    body = {
        "redirect_uris": ["http://127.0.0.1:8765/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if scope is not None:
        body["scope"] = scope
    encoded = json.dumps(body).encode()

    sent = False

    async def receive() -> dict[str, object]:
        """Yield the encoded registration body once to Starlette."""
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/register",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
        },
        receive,
    )


async def _registered_payload(
    proxy: HaMcpOIDCProxy, scope: str | None
) -> dict[str, object]:
    """Register a client through the MCP handler and return its response payload."""
    assert proxy.client_registration_options is not None
    response = await RegistrationHandler(
        provider=proxy,
        options=proxy.client_registration_options,
    ).handle(_registration_request(scope))
    assert response.status_code == 201, response.body.decode()
    return json.loads(response.body)


def test_subclass_preserves_oidc_proxy_constructor_signature() -> None:
    """Adding compatibility behavior must not narrow FastMCP's constructor API."""
    assert inspect.signature(HaMcpOIDCProxy.__init__) == inspect.signature(
        OIDCProxy.__init__
    )


def test_setup_keeps_openid_required_and_default_without_dcr_allow_list(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """Setup must decouple DCR validation without broadening advertised defaults."""
    assert oidc_proxy.client_registration_options is not None
    assert oidc_proxy.client_registration_options.valid_scopes == ["openid"]

    oidc_proxy.setup_scope_compatibility()

    assert oidc_proxy.required_scopes == ["openid"]
    assert oidc_proxy.client_registration_options.default_scopes == ["openid"]
    assert oidc_proxy._default_scope_str == "openid"
    assert oidc_proxy.client_registration_options.valid_scopes is None


def test_protected_resource_metadata_advertises_only_openid(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """Optional provider scopes must not make MCP clients request every scope."""
    oidc_proxy.setup_scope_compatibility()
    app = Starlette(routes=oidc_proxy.get_routes(mcp_path="/mcp"))

    with TestClient(app) as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.json()["scopes_supported"] == ["openid"]


@pytest.mark.asyncio
async def test_dcr_accepts_explicit_optional_oidc_scopes(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """An MCP client may request optional scopes in addition to required openid."""
    oidc_proxy.setup_scope_compatibility()

    payload = await _registered_payload(
        oidc_proxy, "openid profile email offline_access"
    )

    assert payload["scope"] == "openid profile email offline_access"


@pytest.mark.asyncio
async def test_dcr_and_authorize_add_openid_to_optional_only_scope(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """Required scopes survive DCR and a later subset authorization request."""
    oidc_proxy.setup_scope_compatibility()

    payload = await _registered_payload(oidc_proxy, "profile")

    assert payload["scope"] == "openid profile"
    client_id = payload["client_id"]
    assert isinstance(client_id, str)
    persisted = await oidc_proxy._client_store.get(key=client_id)
    assert persisted is not None
    assert persisted.scope == "openid profile"

    redirect_url = await oidc_proxy.authorize(
        persisted,
        AuthorizationParams(
            state="client-state",
            scopes=["profile"],
            code_challenge="client-code-challenge",
            redirect_uri=AnyUrl("http://127.0.0.1:8765/callback"),
            redirect_uri_provided_explicitly=True,
        ),
    )

    assert parse_qs(urlparse(redirect_url).query)["scope"] == ["openid profile"]


@pytest.mark.asyncio
async def test_dcr_without_scope_still_defaults_to_openid(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """Decoupling valid scopes must not remove FastMCP's openid default."""
    oidc_proxy.setup_scope_compatibility()

    payload = await _registered_payload(oidc_proxy, None)

    assert payload["scope"] == "openid"


@pytest.mark.asyncio
async def test_get_client_migrates_and_persists_legacy_scope_once(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """A persisted pre-fix client missing openid is repaired on its first load."""
    oidc_proxy.setup_scope_compatibility()
    await oidc_proxy._client_store.put(
        key="legacy-client",
        value=ProxyDCRClient(
            client_id="legacy-client",
            redirect_uris=[AnyUrl("http://127.0.0.1:8765/callback")],
            scope="profile email",
            token_endpoint_auth_method="none",
        ),
    )

    original_put = oidc_proxy._client_store.put
    with patch.object(
        oidc_proxy._client_store,
        "put",
        new=AsyncMock(wraps=original_put),
    ) as put_spy:
        migrated = await oidc_proxy.get_client("legacy-client")
        unchanged = await oidc_proxy.get_client("legacy-client")

    assert migrated is not None
    assert migrated.scope == "openid profile email"
    assert unchanged is not None
    assert unchanged.scope == "openid profile email"
    put_spy.assert_awaited_once()
    persisted = await oidc_proxy._client_store.get(key="legacy-client")
    assert persisted is not None
    assert persisted.scope == "openid profile email"


@pytest.mark.asyncio
async def test_get_client_does_not_rewrite_compliant_persisted_client(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """A persisted client that already has openid must remain byte-for-byte scoped."""
    oidc_proxy.setup_scope_compatibility()
    await oidc_proxy.register_client(
        OAuthClientInformationFull(
            client_id="current-client",
            redirect_uris=["http://127.0.0.1:8765/callback"],
            scope="profile openid email",
        )
    )

    original_put = oidc_proxy._client_store.put
    with patch.object(
        oidc_proxy._client_store,
        "put",
        new=AsyncMock(wraps=original_put),
    ) as put_spy:
        client = await oidc_proxy.get_client("current-client")

    assert client is not None
    assert client.scope == "profile openid email"
    put_spy.assert_not_awaited()


async def _store_refresh_token(
    proxy: HaMcpOIDCProxy,
    *,
    token: str,
    client_id: str,
    scopes: list[str],
) -> None:
    """Store refresh-token metadata for compatibility-guard tests."""
    await proxy._refresh_token_store.put(
        key=hashlib.sha256(token.encode()).hexdigest(),
        value=RefreshTokenMetadata(
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + 3600,
            created_at=time.time(),
        ),
    )


@pytest.mark.asyncio
async def test_load_refresh_token_rejects_legacy_token_missing_openid(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """A legacy refresh token cannot perpetuate a session without required scopes."""
    oidc_proxy.setup_scope_compatibility()
    client = OAuthClientInformationFull(
        client_id="legacy-client",
        redirect_uris=["http://127.0.0.1:8765/callback"],
        scope="openid profile",
    )
    await _store_refresh_token(
        oidc_proxy,
        token="legacy-refresh-token",
        client_id="legacy-client",
        scopes=["profile"],
    )

    loaded = await oidc_proxy.load_refresh_token(client, "legacy-refresh-token")

    assert loaded is None


@pytest.mark.asyncio
async def test_load_refresh_token_keeps_compliant_token_working(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """The compatibility guard must not reject refresh tokens that contain openid."""
    oidc_proxy.setup_scope_compatibility()
    client = OAuthClientInformationFull(
        client_id="current-client",
        redirect_uris=["http://127.0.0.1:8765/callback"],
        scope="openid profile",
    )
    await _store_refresh_token(
        oidc_proxy,
        token="current-refresh-token",
        client_id="current-client",
        scopes=["openid", "profile"],
    )

    loaded = await oidc_proxy.load_refresh_token(client, "current-refresh-token")

    assert loaded is not None
    assert loaded.scopes == ["openid", "profile"]
