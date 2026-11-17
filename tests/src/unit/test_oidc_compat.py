"""Regression tests for ha-mcp's FastMCP 3.4.6 OIDC compatibility layer."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from key_value.aio.adapters.pydantic import PydanticAdapter
from key_value.aio.stores.memory import MemoryStore
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from ha_mcp._vendor.fastmcp.server.auth.oauth_proxy.models import (
    JTIMapping,
    RefreshTokenMetadata,
    UpstreamTokenSet,
)
from ha_mcp._vendor.fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from ha_mcp._vendor.fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from ha_mcp._vendor.mcp.server.auth.handlers.register import RegistrationHandler
from ha_mcp._vendor.mcp.server.auth.provider import AuthorizationParams
from ha_mcp._vendor.mcp.shared.auth import OAuthClientInformationFull
from ha_mcp.auth.oidc_compat import HaMcpOIDCProxy

CALLBACK = "http://127.0.0.1:8765/callback"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    """The proxies' client storage, with only external OIDC discovery replaced."""
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
        OIDCProxy,
        "get_oidc_configuration",
        lambda self, config_url, strict, timeout_seconds: configuration,
    )
    return MemoryStore()


def _proxy_settings(store: MemoryStore) -> dict[str, Any]:
    """The entrypoint's proxy settings, persisting into ``store``."""
    return {
        "config_url": "https://idp.example.com/.well-known/openid-configuration",
        "client_id": "ha-mcp",
        "client_secret": "test-client-secret",
        "base_url": "https://mcp.example.com",
        "client_storage": store,
        "jwt_signing_key": "test-jwt-signing-key",
        "require_authorization_consent": "external",
        "enable_cimd": False,
    }


@pytest.fixture
def oidc_proxy(store: MemoryStore) -> HaMcpOIDCProxy:
    """A real proxy, as the OIDC entrypoint builds it."""
    return HaMcpOIDCProxy(**_proxy_settings(store), required_scopes=["openid"])


@pytest.fixture
def plain_proxy(store: MemoryStore) -> OIDCProxy:
    """FastMCP's own proxy over the same storage: what a release without the
    compatibility layer persisted, and what is stored as it is read back."""
    return OIDCProxy(**_proxy_settings(store), required_scopes=["openid"])


def _registration_request(scope: str | None) -> Request:
    """Build an MCP dynamic-client-registration request with an optional scope."""
    body: dict[str, object] = {
        "redirect_uris": [CALLBACK],
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
    assert oidc_proxy.client_registration_options.valid_scopes is None


def test_setup_rejects_wrong_required_scopes(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """Compatibility setup must fail if the entrypoint scope contract drifts."""
    oidc_proxy.required_scopes = ["profile"]

    with pytest.raises(
        RuntimeError,
        match="HaMcpOIDCProxy requires exactly the openid scope before setup",
    ):
        oidc_proxy.setup_scope_compatibility()


def test_setup_rejects_missing_registration_options(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """Compatibility setup must fail if FastMCP disables DCR."""
    oidc_proxy.client_registration_options = None

    with pytest.raises(
        RuntimeError,
        match="FastMCP OIDCProxy did not enable client registration",
    ):
        oidc_proxy.setup_scope_compatibility()


def test_setup_rejects_wrong_default_scopes(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """Compatibility setup must fail if FastMCP changes the DCR scope default."""
    assert oidc_proxy.client_registration_options is not None
    oidc_proxy.client_registration_options.default_scopes = ["profile"]

    with pytest.raises(
        RuntimeError,
        match="FastMCP OIDCProxy did not preserve the openid default",
    ):
        oidc_proxy.setup_scope_compatibility()


def test_setup_rejects_wrong_upstream_default_scope_string(
    oidc_proxy: HaMcpOIDCProxy,
) -> None:
    """Compatibility setup must fail if FastMCP changes its upstream default."""
    assert oidc_proxy.client_registration_options is not None
    oidc_proxy.update_default_scopes(["profile"])
    oidc_proxy.client_registration_options.default_scopes = ["openid"]

    with pytest.raises(
        RuntimeError,
        match="FastMCP OIDCProxy did not preserve the openid default",
    ):
        oidc_proxy.setup_scope_compatibility()


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
    oidc_proxy: HaMcpOIDCProxy, plain_proxy: OIDCProxy
) -> None:
    """Required scopes survive DCR and a later subset authorization request."""
    oidc_proxy.setup_scope_compatibility()

    payload = await _registered_payload(oidc_proxy, "profile")

    assert payload["scope"] == "openid profile"
    client_id = payload["client_id"]
    assert isinstance(client_id, str)
    persisted = await plain_proxy.get_client(client_id)
    assert persisted is not None
    assert persisted.scope == "openid profile"

    redirect_url = await oidc_proxy.authorize(
        persisted,
        AuthorizationParams(
            state="client-state",
            scopes=["profile"],
            code_challenge="client-code-challenge",
            redirect_uri=AnyUrl(CALLBACK),
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


def _client(client_id: str, scope: str) -> OAuthClientInformationFull:
    """A dynamic-client registration with ``scope``."""
    return OAuthClientInformationFull(
        client_id=client_id, redirect_uris=[AnyUrl(CALLBACK)], scope=scope
    )


@pytest.mark.asyncio
async def test_get_client_migrates_and_persists_legacy_scope_once(
    oidc_proxy: HaMcpOIDCProxy, plain_proxy: OIDCProxy, store: MemoryStore
) -> None:
    """A persisted pre-fix client missing openid is repaired on its first load."""
    oidc_proxy.setup_scope_compatibility()
    await plain_proxy.register_client(_client("legacy-client", "profile email"))

    with patch.object(store, "put", new=AsyncMock(wraps=store.put)) as put_spy:
        migrated = await oidc_proxy.get_client("legacy-client")
        unchanged = await oidc_proxy.get_client("legacy-client")

    assert migrated is not None
    assert migrated.scope == "openid profile email"
    assert unchanged is not None
    assert unchanged.scope == "openid profile email"
    put_spy.assert_awaited_once()
    persisted = await plain_proxy.get_client("legacy-client")
    assert persisted is not None
    assert persisted.scope == "openid profile email"


@pytest.mark.asyncio
async def test_get_client_does_not_rewrite_compliant_persisted_client(
    oidc_proxy: HaMcpOIDCProxy, store: MemoryStore
) -> None:
    """A persisted client that already has openid must remain byte-for-byte scoped."""
    oidc_proxy.setup_scope_compatibility()
    await oidc_proxy.register_client(_client("current-client", "profile openid email"))

    with patch.object(store, "put", new=AsyncMock(wraps=store.put)) as put_spy:
        client = await oidc_proxy.get_client("current-client")

    assert client is not None
    assert client.scope == "profile openid email"
    put_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_client_rejects_unexpected_persisted_client_type(
    oidc_proxy: HaMcpOIDCProxy, plain_proxy: OIDCProxy
) -> None:
    """Persisted migration must fail if FastMCP stops returning its DCR model."""
    await plain_proxy.register_client(_client("unexpected-client", "profile"))
    unexpected = _client("unexpected-client", "profile")

    with (
        patch.object(
            OIDCProxy,
            "get_client",
            new=AsyncMock(return_value=unexpected),
        ),
        pytest.raises(
            RuntimeError,
            match="FastMCP returned an unexpected persisted client type",
        ),
    ):
        await oidc_proxy.get_client("unexpected-client")


async def _access_token_proxy(
    store: MemoryStore, scopes: list[str]
) -> tuple[HaMcpOIDCProxy, str]:
    """A set-up proxy and a real FastMCP JWT it issued over an upstream token
    granted ``scopes``, as a sign-in leaves them in the proxy's storage."""
    now = time.time()
    upstream_token = "upstream-access-token"
    upstream_token_id = "upstream-token-id"
    access_jti = "fastmcp-access-jti"
    proxy = HaMcpOIDCProxy(
        **_proxy_settings(store),
        token_verifier=StaticTokenVerifier(
            tokens={
                upstream_token: {
                    "client_id": "access-client",
                    "scopes": scopes,
                    "expires_at": int(now) + 3600,
                }
            },
            required_scopes=["openid"],
        ),
    )
    proxy.setup_scope_compatibility()
    await PydanticAdapter[UpstreamTokenSet](
        key_value=store,
        pydantic_model=UpstreamTokenSet,
        default_collection="mcp-upstream-tokens",
    ).put(
        key=upstream_token_id,
        value=UpstreamTokenSet(
            upstream_token_id=upstream_token_id,
            access_token=upstream_token,
            refresh_token=None,
            refresh_token_expires_at=None,
            expires_at=now + 3600,
            token_type="Bearer",
            scope=" ".join(scopes),
            client_id="access-client",
            created_at=now,
            raw_token_data={
                "access_token": upstream_token,
                "scope": " ".join(scopes),
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        ),
    )
    await PydanticAdapter[JTIMapping](
        key_value=store,
        pydantic_model=JTIMapping,
        default_collection="mcp-jti-mappings",
    ).put(
        key=access_jti,
        value=JTIMapping(
            jti=access_jti,
            upstream_token_id=upstream_token_id,
            created_at=now,
        ),
    )
    proxy.get_routes(mcp_path="/mcp")
    token = proxy.jwt_issuer.issue_access_token(
        client_id="access-client",
        scopes=scopes,
        jti=access_jti,
        expires_in=3600,
    )
    return proxy, token


@pytest.mark.asyncio
async def test_load_access_token_rejects_legacy_token_missing_openid(
    store: MemoryStore,
) -> None:
    """A live pre-fix access token must fail on its next MCP request."""
    proxy, token = await _access_token_proxy(store, ["profile"])

    loaded = await proxy.load_access_token(token)

    assert loaded is None


@pytest.mark.asyncio
async def test_load_access_token_keeps_compliant_token_working(
    store: MemoryStore,
) -> None:
    """A FastMCP access token with required scopes must remain usable."""
    proxy, token = await _access_token_proxy(store, ["openid", "profile"])

    loaded = await proxy.load_access_token(token)

    assert loaded is not None
    assert loaded.scopes == ["openid", "profile"]


async def _store_refresh_token(
    store: MemoryStore,
    *,
    token: str,
    client_id: str,
    scopes: list[str],
) -> None:
    """Store refresh-token metadata as an issued refresh token leaves it."""
    await PydanticAdapter[RefreshTokenMetadata](
        key_value=store,
        pydantic_model=RefreshTokenMetadata,
        default_collection="mcp-refresh-tokens",
    ).put(
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
    store: MemoryStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A legacy refresh token cannot perpetuate a session without required scopes."""
    oidc_proxy.setup_scope_compatibility()
    client = _client("legacy-client", "openid profile")
    await _store_refresh_token(
        store,
        token="legacy-refresh-token",
        client_id="legacy-client",
        scopes=["profile"],
    )

    with caplog.at_level(logging.DEBUG, logger="ha_mcp.auth.oidc_compat"):
        loaded = await oidc_proxy.load_refresh_token(client, "legacy-refresh-token")

    assert loaded is None
    assert "client_id=legacy-client" in caplog.text
    assert "missing required scopes" in caplog.text
    assert "legacy-refresh-token" not in caplog.text


@pytest.mark.asyncio
async def test_load_refresh_token_keeps_compliant_token_working(
    oidc_proxy: HaMcpOIDCProxy, store: MemoryStore
) -> None:
    """The compatibility guard must not reject refresh tokens that contain openid."""
    oidc_proxy.setup_scope_compatibility()
    client = _client("current-client", "openid profile")
    await _store_refresh_token(
        store,
        token="current-refresh-token",
        client_id="current-client",
        scopes=["openid", "profile"],
    )

    loaded = await oidc_proxy.load_refresh_token(client, "current-refresh-token")

    assert loaded is not None
    assert loaded.scopes == ["openid", "profile"]
