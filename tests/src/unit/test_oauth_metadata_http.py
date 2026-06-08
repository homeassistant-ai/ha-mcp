"""HTTP-level smoke tests for the OAuth metadata-discovery endpoints.

The route-existence tests in ``test_oauth.py`` (``TestOAuthRoutes``) only assert
that ``provider.get_routes()`` *lists* the well-known routes. They do not boot the
assembled ASGI app or make a real request, so they cannot catch a regression where
a framework change mounts a competing ``/.well-known/openid-configuration`` route
that shadows ours, nor verify the served body.

These tests close that gap: they build the real app the way ``_run_oauth_server``
does (``FastMCP`` with ``mcp.auth`` set, then ``http_app()``) and drive it over
HTTP via ``httpx.ASGITransport``. The discriminator is the *enhanced* metadata our
handler injects — ``response_modes_supported`` and the ``"none"`` token-endpoint
auth method (needed by Claude.ai public PKCE clients). If our handler were ever
shadowed by a framework-supplied variant, those fields would disappear and these
tests would fail.
"""

import httpx
import pytest
from fastmcp import FastMCP

from ha_mcp.auth import HomeAssistantOAuthProvider

BASE_URL = "http://localhost:8086"

# Every discovery path our provider serves the enhanced metadata on:
# - the OAuth 2.1 authorization-server endpoint (the one we replace)
# - the standard OpenID Configuration endpoint (required by ChatGPT)
# - the non-standard /token/... variant (ChatGPT bug workaround)
DISCOVERY_PATHS = [
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/token/.well-known/openid-configuration",
]


@pytest.fixture
def oauth_app():
    """Assemble the real OAuth-enabled ASGI app (FastMCP + our provider).

    Mirrors ``_run_oauth_server``: construct the server, attach the provider to
    ``mcp.auth``, then build the HTTP app. ``stateless_http=True`` avoids needing
    the MCP session lifespan, which the plain well-known GET routes do not require.
    """
    server = FastMCP("test")
    server.auth = HomeAssistantOAuthProvider(base_url=BASE_URL)
    return server.http_app(path="/mcp", stateless_http=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("discovery_path", DISCOVERY_PATHS)
async def test_metadata_endpoint_serves_enhanced_metadata(oauth_app, discovery_path):
    """Each discovery endpoint returns 200 with our enhanced OAuth metadata."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=oauth_app), base_url="http://test"
    ) as client:
        resp = await client.get(discovery_path)

    assert resp.status_code == 200, (
        f"{discovery_path} returned {resp.status_code}, expected 200"
    )
    data = resp.json()

    # Standard OAuth 2.1 / OIDC discovery fields, all anchored at our base_url.
    assert data["issuer"].rstrip("/") == BASE_URL
    assert data["authorization_endpoint"].startswith(BASE_URL)
    assert data["token_endpoint"].startswith(BASE_URL)
    # DCR is enabled by default, so the registration endpoint must be advertised.
    assert data["registration_endpoint"].startswith(BASE_URL)

    # Enhanced fields injected by enhanced_metadata_handler — these are the
    # discriminator proving OUR handler served the response (not a framework
    # default or a shadowing well-known route from a dependency bump).
    assert data["response_modes_supported"] == ["query"]
    assert "none" in data["token_endpoint_auth_methods_supported"], (
        "public-client PKCE 'none' auth method missing — handler was not applied"
    )


@pytest.mark.asyncio
async def test_discovery_endpoints_serve_identical_metadata(oauth_app):
    """All three discovery paths serve byte-identical metadata.

    Per RFC 8414 the openid-configuration aliases must mirror the
    oauth-authorization-server document; this guards against one path drifting
    (e.g. a shadowing route serving a different body on only one alias).
    """
    bodies = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=oauth_app), base_url="http://test"
    ) as client:
        for path in DISCOVERY_PATHS:
            resp = await client.get(path)
            assert resp.status_code == 200
            bodies[path] = resp.json()

    canonical = bodies["/.well-known/oauth-authorization-server"]
    for path, body in bodies.items():
        assert body == canonical, (
            f"{path} metadata diverged from the canonical document"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("discovery_path", DISCOVERY_PATHS)
async def test_metadata_endpoint_allows_cors_preflight(oauth_app, discovery_path):
    """OPTIONS preflight succeeds with CORS allowed (browser OAuth clients need it)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=oauth_app), base_url="http://test"
    ) as client:
        resp = await client.options(
            discovery_path,
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert resp.status_code in (200, 204)
    assert resp.headers.get("access-control-allow-origin") in ("*", "https://claude.ai")
