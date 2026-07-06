"""HTTP-level smoke tests for the OAuth metadata-discovery endpoints.

The route-existence tests in ``test_oauth.py`` (``TestOAuthRoutes``) only assert
that ``provider.get_routes()`` *lists* the well-known routes. They do not boot the
assembled ASGI app or make a real request, so they cannot catch a regression where
our metadata route is shadowed or replaced by a framework default (or a duplicate
route is mounted for the same path), nor verify the served body.

These tests close that gap: they assemble an OAuth-enabled ASGI app the way the
production server does in the ways that matter for discovery — ``mcp.auth`` set to
our provider and ``stateless_http=True`` — and drive it over HTTP via
``httpx.ASGITransport``. The content discriminator is the *enhanced* metadata our
handler injects — ``response_modes_supported`` and the ``"none"`` token-endpoint
auth method (needed by Claude.ai public PKCE clients): if our handler were replaced
by a framework default those fields would disappear. A separate test asserts each
discovery path resolves to exactly one route, so a shadowing/duplicate route (which,
under Starlette's first-match-wins routing, would not change the served body) is
also caught.
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
def oauth_app(tmp_path, monkeypatch):
    """Assemble an OAuth-enabled ASGI app (FastMCP + our provider).

    Builds the app the way the production server does in the ways that matter for
    metadata discovery: attach the provider to ``mcp.auth`` and build with
    ``stateless_http=True`` (which avoids needing the MCP session lifespan that the
    plain well-known GET routes do not use). The production entrypoint
    (``_run_oauth_server``) reaches the same assembled app via ``run_async``; the
    extra wiring it adds — HA client, landing/settings routes — is irrelevant here.

    Constructing the provider persists an HMAC secret (and any registered
    clients) under ``get_data_dir()``, so redirect it to a temp dir to keep the
    test hermetic — mirrors the ``isolate_data_dir`` fixture in ``test_oauth.py``.
    """
    monkeypatch.setattr("ha_mcp.auth.provider.get_data_dir", lambda: tmp_path)

    # Assemble the app the way production serves it: ha-mcp defaults fastmcp's
    # Host/Origin (DNS-rebinding) guard off (see ha_mcp.transport_security),
    # because it is reached through operator-chosen proxies on arbitrary hosts.
    # Without this, fastmcp >= 3.4.3's on-by-default guard 403s the cross-origin
    # discovery preflight (and 421s a non-loopback Host) before the request
    # reaches our metadata route. The ``hasattr`` check keeps this a no-op on
    # fastmcp < 3.4.3, where the setting field does not exist.
    import fastmcp

    monkeypatch.setenv("FASTMCP_HTTP_HOST_ORIGIN_PROTECTION", "false")
    if hasattr(fastmcp.settings, "http_host_origin_protection"):
        monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", False)

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

    # The standard OAuth 2.1 / OIDC discovery fields we assert are anchored at base_url.
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
    """All discovery paths serve identical metadata (parsed-JSON equality).

    The openid-configuration aliases are served with metadata identical to the
    oauth-authorization-server document; this guards against one alias drifting
    from the canonical body. The canonical body is also re-checked for an enhanced
    field so a *uniform* regression (all paths shadowed at once) still fails here
    rather than passing vacuously — without it the three aliases share one handler
    closure and would always compare equal.
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
    # Non-vacuous guard: confirm the canonical body is actually our enhanced
    # document, so a uniform shadow across all paths trips this test too.
    assert canonical.get("response_modes_supported") == ["query"]
    for path, body in bodies.items():
        assert body == canonical, (
            f"{path} metadata diverged from the canonical document"
        )


def _iter_route_paths(routes):
    """Yield every Route ``path`` in an assembled app, descending into mounts."""
    for route in routes:
        subroutes = getattr(route, "routes", None)
        if subroutes:
            yield from _iter_route_paths(subroutes)
        path = getattr(route, "path", None)
        if path is not None:
            yield path


@pytest.mark.parametrize("discovery_path", DISCOVERY_PATHS)
def test_discovery_path_registered_exactly_once(oauth_app, discovery_path):
    """Each discovery path resolves to exactly one route in the assembled app.

    The body tests prove the *first* matching route is ours; this proves there is
    no second one. Starlette routes first-match-wins, so a duplicate/shadowing
    route mounted by a dependency would not change the served body and would
    otherwise go uncaught — this is the regression class the module guards.
    """
    count = list(_iter_route_paths(oauth_app.routes)).count(discovery_path)
    assert count == 1, (
        f"{discovery_path} is registered {count} time(s) in the assembled app "
        "(expected exactly 1 — a duplicate or shadowing route may exist)"
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
    # cors_middleware configures allow_origins="*", so the preflight echoes "*".
    # Pin it exactly: a regression that narrowed or dropped CORS should fail here.
    assert resp.headers.get("access-control-allow-origin") == "*"
    # The preflight must advertise GET, or the browser blocks the discovery fetch.
    assert "GET" in resp.headers.get("access-control-allow-methods", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("discovery_path", DISCOVERY_PATHS)
async def test_metadata_endpoint_rejects_non_get(oauth_app, discovery_path):
    """Non-GET methods are rejected — the routes advertise only GET/OPTIONS.

    Pins the method contract declared in ``provider.get_routes()`` so a refactor
    that widened the allowed methods (or made the route a catch-all) is caught.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=oauth_app), base_url="http://test"
    ) as client:
        resp = await client.post(discovery_path, json={})

    assert resp.status_code == 405, (
        f"POST {discovery_path} returned {resp.status_code}, expected 405 "
        "(discovery routes should allow only GET/OPTIONS)"
    )
