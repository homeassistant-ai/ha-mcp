"""Unit tests for the none-mode auto-approve OAuth server (issue #1969).

Covers ``custom_components/ha_mcp_tools/oauth_autoapprove.py``: the invisible
``/authorize`` (issues a PKCE code + 302, no UI) and ``/token`` (public-client
PKCE exchange, cosmetic opaque token) views, the ``AutoApproveProvider`` code
lifecycle, and — most importantly — the open-redirect gate layered on top of
:func:`oauth_legacy._is_valid_redirect_uri` (every spec-valid provider callback
is accepted; malformed targets are rejected in place).

Home Assistant / aiohttp are stubbed via ``_embedded_stubs``. ``yarl`` (an
aiohttp dependency also absent here) is stubbed with the tiny
``URL.update_query`` surface ``_redirect_with`` uses — same convention as
``test_oauth_legacy_component.py``.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import secrets
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()


class _FakeURL:
    """Stand-in for ``yarl.URL`` covering only ``update_query`` + ``str()``."""

    def __init__(self, url: str) -> None:
        self._url = url

    def update_query(self, params: dict[str, str]) -> _FakeURL:
        sep = "&" if "?" in self._url else "?"
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return _FakeURL(f"{self._url}{sep}{query}" if query else self._url)

    def with_query(self, params) -> _FakeURL:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return _FakeURL(f"{self._url}?{query}" if query else self._url)

    def __str__(self) -> str:
        return self._url


CORE_TOKEN_BODY = b'{"access_token":"x"}'


class _CoreTokenResponse:
    """Async context manager response returned by the core token stub."""

    content_type = "application/json"

    def __init__(self, body: bytes = CORE_TOKEN_BODY, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return None

    async def read(self):
        return self._body


class _CoreTokenSession:
    """Capture token-forward POSTs and optionally raise a transport error.

    ``body`` and ``status`` are what core answers with — pass a body carrying a
    ``refresh_token`` to exercise the #2248 envelope rewrite, and a non-200
    status to pin that only successes are rewritten.
    """

    def __init__(
        self,
        *,
        error: Exception | None = None,
        body: bytes = CORE_TOKEN_BODY,
        status: int = 200,
    ) -> None:
        self.error = error
        self.body = body
        self.status = status
        self.calls = []

    def post(self, url, *, data, timeout):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return _CoreTokenResponse(self.body, self.status)


if "yarl" not in sys.modules:
    _yarl = ModuleType("yarl")
    _yarl.URL = _FakeURL  # type: ignore[attr-defined]
    sys.modules["yarl"] = _yarl

import custom_components.ha_mcp_tools.oauth_autoapprove as aa  # noqa: E402
from custom_components.ha_mcp_tools import (  # noqa: E402
    oauth_dcr,
    oauth_ha_auth,
    oauth_legacy,
)
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_WEBHOOK,
    DOMAIN,
    OAUTH_BASE,
    WEBHOOK_AUTH_LEGACY,
)

CLAUDE_CLIENT_ID = "https://claude.ai/api/mcp/client_metadata"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
HOST = "ha.example.com"
EXPECTED_ISSUER = f"https://{HOST}{OAUTH_BASE}"


def _pkce_pair() -> tuple[str, str]:
    """A valid (code_verifier, code_challenge) pair per RFC 7636 S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}
    return hass


def _live_hass(provider: aa.AutoApproveProvider | None = None) -> MagicMock:
    """A hass whose live webhook cfg is none-mode auto-approve (provider set)."""
    hass = _make_hass()
    hass.data[DOMAIN] = {
        DATA_WEBHOOK: {
            "webhook_id": "mcp_x",
            "auth_mode": "none",
            "resource_server": None,
            "oauth_provider": None,
            aa.CFG_AUTOAPPROVE_PROVIDER: provider or aa.AutoApproveProvider(),
        }
    }
    return hass


def _get_request(query: dict[str, str]) -> MagicMock:
    request = MagicMock(name="Request")
    request.query = query
    # Host/scheme are what ``mcp_webhook._build_base_url`` reads to derive the
    # RFC 9207 ``iss`` this server stamps on its authorization responses.
    request.headers = {"Host": HOST}
    request.scheme = "https"
    return request


def _authorize_query(**overrides: str) -> dict[str, str]:
    _, challenge = _pkce_pair()
    params = {
        "response_type": "code",
        "client_id": CLAUDE_CLIENT_ID,
        "redirect_uri": CLAUDE_REDIRECT,
        "state": "st-1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    params.update(overrides)
    return params


def _parse_location(location: str) -> tuple[str, dict[str, str]]:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(location)
    flat = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return base, flat


def _module_is(name: str, root: str) -> bool:
    """Return whether a module name is ``root`` or one of its children."""
    return name == root or name.startswith(f"{root}.")


def _mode_cfg(
    mode: str,
    *,
    session: object | None = None,
    cimd_session: object | None = None,
    dcr_key: bytes | None = None,
) -> dict[str, object | None]:
    """Webhook cfg dict seeding exactly one live-mode marker (mirrors
    active_auth_mode's provider-presence checks)."""
    if mode == "legacy":
        return {
            "oauth_provider": oauth_legacy.LegacyOAuthProvider(
                client_id="hamcp-test-client-id-123",
                client_secret="hamcp-test-secret",
                signing_key=b"k" * 32,
                active_mode_getter=lambda: WEBHOOK_AUTH_LEGACY,
            )
        }
    if mode == "ha_auth":
        return {
            "resource_server": object(),
            "session": session,
            "cimd_session": cimd_session,
            oauth_dcr.CFG_DCR_SIGNING_KEY: dcr_key,
        }
    if mode == "none":
        return {aa.CFG_AUTOAPPROVE_PROVIDER: aa.AutoApproveProvider()}
    raise ValueError(f"unknown test mode: {mode}")


@pytest.fixture
async def unified_view_client_factory():
    """Build real aiohttp clients around the two unified scoped OAuth views."""
    # Ensure every component module the view imports lazily is first loaded
    # against the shared stubs. The real aiohttp/yarl modules below are only for
    # this HTTP-level harness and must not leak into the rest of the unit suite.
    for module_name in (
        "custom_components.ha_mcp_tools.mcp_webhook",
        "custom_components.ha_mcp_tools.oauth_ha_auth",
    ):
        importlib.import_module(module_name)

    package_roots = ("aiohttp", "yarl")
    saved_modules = {
        name: module
        for name, module in tuple(sys.modules.items())
        if any(_module_is(name, root) for root in package_roots)
    }
    for name in saved_modules:
        sys.modules.pop(name, None)

    clients = []
    stub_aiohttp = aa.aiohttp
    stub_autoapprove_web = aa.web
    stub_legacy_web = oauth_legacy.web
    try:
        aiohttp = importlib.import_module("aiohttp")
        aiohttp_web = importlib.import_module("aiohttp.web")
        test_utils = importlib.import_module("aiohttp.test_utils")
        aa.aiohttp = aiohttp
        aa.web = aiohttp_web
        oauth_legacy.web = aiohttp_web

        async def factory(
            *,
            mode: str,
            session: object | None = None,
            cimd_session: object | None = None,
            dcr_key: bytes | None = None,
        ):
            cfg = _mode_cfg(
                mode,
                session=session,
                cimd_session=cimd_session,
                dcr_key=dcr_key,
            )

            app = aiohttp_web.Application()
            hass = SimpleNamespace(
                data={DOMAIN: {DATA_WEBHOOK: cfg}},
                http=SimpleNamespace(),
            )

            def register_view(view):
                if hasattr(view, "get"):
                    app.router.add_get(view.url, view.get)
                if hasattr(view, "post"):
                    app.router.add_post(view.url, view.post)

            hass.http.register_view = register_view
            aa.bind_autoapprove_views(hass)

            client = test_utils.TestClient(test_utils.TestServer(app))
            await client.start_server()
            clients.append(client)
            return client

        yield factory
    finally:
        for client in clients:
            await client.close()
        aa.aiohttp = stub_aiohttp
        aa.web = stub_autoapprove_web
        oauth_legacy.web = stub_legacy_web
        for name in tuple(sys.modules):
            if any(_module_is(name, root) for root in package_roots):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


# ---------------------------------------------------------------------------
# AutoApproveProvider
# ---------------------------------------------------------------------------


class TestAutoApproveProvider:
    def test_issue_and_consume_roundtrip(self):
        provider = aa.AutoApproveProvider()
        verifier, challenge = _pkce_pair()
        code = provider.issue_code(CLAUDE_REDIRECT, challenge)
        assert code
        assert provider.consume_code(code, CLAUDE_REDIRECT, verifier) is True

    def test_wrong_verifier_rejected(self):
        provider = aa.AutoApproveProvider()
        _, challenge = _pkce_pair()
        code = provider.issue_code(CLAUDE_REDIRECT, challenge)
        other_verifier, _ = _pkce_pair()
        assert provider.consume_code(code, CLAUDE_REDIRECT, other_verifier) is False

    def test_code_is_one_shot(self):
        provider = aa.AutoApproveProvider()
        verifier, challenge = _pkce_pair()
        code = provider.issue_code(CLAUDE_REDIRECT, challenge)
        assert provider.consume_code(code, CLAUDE_REDIRECT, verifier) is True
        assert provider.consume_code(code, CLAUDE_REDIRECT, verifier) is False

    def test_access_token_is_opaque_and_unique(self):
        t1 = aa.AutoApproveProvider.issue_access_token()
        t2 = aa.AutoApproveProvider.issue_access_token()
        assert isinstance(t1, str) and len(t1) >= 20
        assert t1 != t2


# ---------------------------------------------------------------------------
# AutoApproveAuthorizeView (GET: issue code, 302, no UI)
# ---------------------------------------------------------------------------


class TestAuthorizeView:
    async def test_404_when_not_live(self):
        hass = _make_hass()  # no autoapprove provider in cfg
        view = aa.AutoApproveAuthorizeView(hass)
        resp = await view.get(_get_request(_authorize_query()))
        assert resp.status == 404

    async def test_happy_path_issues_code_and_redirects_no_ui(self):
        provider = aa.AutoApproveProvider()
        hass = _live_hass(provider)
        view = aa.AutoApproveAuthorizeView(hass)
        verifier, challenge = _pkce_pair()
        query = _authorize_query(code_challenge=challenge)
        resp = await view.get(_get_request(query))

        assert resp.status == 302
        base, params = _parse_location(resp.headers["Location"])
        assert base == CLAUDE_REDIRECT
        assert params["state"] == "st-1"
        # RFC 9207 §2: the success response names the issuer that minted it.
        assert params["iss"] == EXPECTED_ISSUER
        # The issued code is real: it consumes with the matching verifier.
        assert provider.consume_code(params["code"], CLAUDE_REDIRECT, verifier) is True

    async def test_iss_equals_the_advertised_none_mode_issuer(self):
        """The redirect's ``iss`` is byte-identical to the ``issuer`` the
        none-mode discovery document advertises for the same request — RFC 9207
        §2 rejects anything else, and the two are built in different modules."""
        from custom_components.ha_mcp_tools import mcp_webhook

        hass = _live_hass()
        view = aa.AutoApproveAuthorizeView(hass)
        request = _get_request(_authorize_query())
        resp = await view.get(request)

        advertised = mcp_webhook._none_mode_authorization_server_document(
            mcp_webhook._build_base_url(request)
        )["issuer"]
        assert advertised == EXPECTED_ISSUER
        _, params = _parse_location(resp.headers["Location"])
        assert params["iss"] == advertised

    async def test_claude_redirect_is_approved(self):
        """Approve the canonical Claude hosted callback."""
        hass = _live_hass()
        view = aa.AutoApproveAuthorizeView(hass)
        resp = await view.get(_get_request(_authorize_query()))
        assert resp.status == 302
        base, _ = _parse_location(resp.headers["Location"])
        assert base == CLAUDE_REDIRECT

    async def test_non_s256_method_rejected(self):
        hass = _live_hass()
        view = aa.AutoApproveAuthorizeView(hass)
        resp = await view.get(
            _get_request(_authorize_query(code_challenge_method="plain"))
        )
        assert resp.status == 400

    async def test_non_code_response_type_rejected(self):
        hass = _live_hass()
        view = aa.AutoApproveAuthorizeView(hass)
        resp = await view.get(_get_request(_authorize_query(response_type="token")))
        assert resp.status == 400

    async def test_malformed_code_challenge_rejected(self):
        hass = _live_hass()
        view = aa.AutoApproveAuthorizeView(hass)
        resp = await view.get(
            _get_request(_authorize_query(code_challenge="too-short"))
        )
        assert resp.status == 400

    async def test_code_challenge_with_trailing_newline_rejected(self):
        hass = _live_hass()
        view = aa.AutoApproveAuthorizeView(hass)
        resp = await view.get(
            _get_request(_authorize_query(code_challenge="a" * 43 + "\n"))
        )
        assert resp.status == 400

    async def test_code_store_at_capacity_redirects_temporarily_unavailable(self):
        provider = aa.AutoApproveProvider()
        provider.issue_code = lambda *a, **k: None  # type: ignore[method-assign]
        hass = _live_hass(provider)
        view = aa.AutoApproveAuthorizeView(hass)
        resp = await view.get(_get_request(_authorize_query()))
        assert resp.status == 302
        _, params = _parse_location(resp.headers["Location"])
        assert params["error"] == "temporarily_unavailable"
        assert params["state"] == "st-1"
        # RFC 9207 §2 covers error responses too, not just the success redirect.
        assert params["iss"] == EXPECTED_ISSUER


# ---------------------------------------------------------------------------
# AutoApproveTokenView (POST: PKCE exchange, public client)
# ---------------------------------------------------------------------------


def _token_request(form: dict[str, str]) -> MagicMock:
    request = MagicMock(name="Request")
    request.post = AsyncMock(return_value=form)
    return request


class TestTokenView:
    async def test_404_when_not_live(self):
        hass = _make_hass()
        view = aa.AutoApproveTokenView(hass)
        resp = await view.post(_token_request({}))
        assert resp.status == 404

    async def test_valid_pkce_exchange_returns_opaque_token_no_secret(self):
        provider = aa.AutoApproveProvider()
        hass = _live_hass(provider)
        verifier, challenge = _pkce_pair()
        code = provider.issue_code(CLAUDE_REDIRECT, challenge)
        view = aa.AutoApproveTokenView(hass)
        # NOTE: no client_secret in the form — public client.
        resp = await view.post(
            _token_request(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CLAUDE_REDIRECT,
                    "code_verifier": verifier,
                }
            )
        )
        assert resp.status == 200
        body = resp.json_body
        assert body["token_type"] == "Bearer"
        assert body["access_token"]
        assert isinstance(body["expires_in"], int)
        # None mode issues no refresh token.
        assert "refresh_token" not in body
        # RFC 6749 §5.1: the token body must not be cached.
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Pragma"] == "no-cache"

    async def test_wrong_verifier_rejected(self):
        provider = aa.AutoApproveProvider()
        hass = _live_hass(provider)
        _, challenge = _pkce_pair()
        code = provider.issue_code(CLAUDE_REDIRECT, challenge)
        wrong_verifier, _ = _pkce_pair()
        view = aa.AutoApproveTokenView(hass)
        resp = await view.post(
            _token_request(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CLAUDE_REDIRECT,
                    "code_verifier": wrong_verifier,
                }
            )
        )
        assert resp.status == 400
        assert resp.json_body["error"] == "invalid_grant"

    async def test_code_is_one_time_at_token_endpoint(self):
        provider = aa.AutoApproveProvider()
        hass = _live_hass(provider)
        verifier, challenge = _pkce_pair()
        code = provider.issue_code(CLAUDE_REDIRECT, challenge)
        view = aa.AutoApproveTokenView(hass)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_REDIRECT,
            "code_verifier": verifier,
        }
        first = await view.post(_token_request(dict(form)))
        assert first.status == 200
        second = await view.post(_token_request(dict(form)))
        assert second.status == 400
        assert second.json_body["error"] == "invalid_grant"

    async def test_missing_params_returns_invalid_request(self):
        provider = aa.AutoApproveProvider()
        hass = _live_hass(provider)
        _, challenge = _pkce_pair()
        code = provider.issue_code(CLAUDE_REDIRECT, challenge)
        view = aa.AutoApproveTokenView(hass)
        resp = await view.post(
            _token_request(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CLAUDE_REDIRECT,
                    # code_verifier omitted
                }
            )
        )
        assert resp.status == 400
        assert resp.json_body["error"] == "invalid_request"

    async def test_unsupported_grant_type_rejected(self):
        hass = _live_hass()
        view = aa.AutoApproveTokenView(hass)
        resp = await view.post(_token_request({"grant_type": "refresh_token"}))
        assert resp.status == 400
        assert resp.json_body["error"] == "unsupported_grant_type"


# ---------------------------------------------------------------------------
# bind_autoapprove_views (bind-once guard)
# ---------------------------------------------------------------------------


class TestBindAutoApproveViews:
    def test_first_bind_registers_two_views(self):
        hass = _make_hass()
        hass.http = MagicMock()
        aa.bind_autoapprove_views(hass)
        assert hass.http.register_view.call_count == 2
        assert hass.data.get(aa._AUTOAPPROVE_VIEWS_REGISTERED_KEY) is True

    def test_second_bind_is_a_noop(self):
        # aiohttp cannot rebind a view — a re-enable must reuse the bound pair.
        hass = _make_hass()
        hass.http = MagicMock()
        aa.bind_autoapprove_views(hass)
        aa.bind_autoapprove_views(hass)
        assert hass.http.register_view.call_count == 2


# ---------------------------------------------------------------------------
# Full round-trip through both views
# ---------------------------------------------------------------------------


class TestFullFlow:
    async def test_authorize_then_token_completes_invisibly(self):
        provider = aa.AutoApproveProvider()
        hass = _live_hass(provider)
        authorize = aa.AutoApproveAuthorizeView(hass)
        token = aa.AutoApproveTokenView(hass)
        verifier, challenge = _pkce_pair()

        auth_resp = await authorize.get(
            _get_request(_authorize_query(code_challenge=challenge))
        )
        assert auth_resp.status == 302  # no consent UI rendered
        _, params = _parse_location(auth_resp.headers["Location"])
        code = params["code"]

        token_resp = await token.post(
            _token_request(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CLAUDE_REDIRECT,
                    "code_verifier": verifier,
                }
            )
        )
        assert token_resp.status == 200
        assert token_resp.json_body["access_token"]


class TestRfc9207MetadataAdvertisement:
    """RFC 9207 §3: the none-mode document advertises response issuer support."""

    def test_none_mode_document_advertises_iss_support(self):
        from custom_components.ha_mcp_tools import mcp_webhook

        doc = mcp_webhook._none_mode_authorization_server_document("https://ha.example")
        assert doc["authorization_response_iss_parameter_supported"] is True


async def test_scoped_authorize_serves_legacy_consent_when_legacy_live(
    unified_view_client_factory,
):
    """Legacy mode serves its consent form from the unified scoped route."""
    client = await unified_view_client_factory(mode="legacy")
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        "?response_type=code&client_id=hamcp-test-client-id-123"
        "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
        "&code_challenge=" + "a" * 43 + "&code_challenge_method=S256"
    )
    assert resp.status == 200
    text = await resp.text()
    assert "Authorize MCP Connector" in text
    assert 'action="/api/ha_mcp_tools/oauth/authorize"' in text


async def test_scoped_authorize_redirects_into_core_when_ha_auth_live(
    unified_view_client_factory,
):
    """ha_auth mode redirects the unified route into core's authorize view."""
    client = await unified_view_client_factory(mode="ha_auth")
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        "?response_type=code&client_id=https%3A%2F%2Fclaude.ai"
        "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
        "&code_challenge=" + "a" * 43 + "&code_challenge_method=S256"
        "&state=xyz",
        allow_redirects=False,
    )
    assert resp.status == 302
    # Parse rather than substring-match: yarl legally leaves ':' and '/'
    # unencoded inside query values (RFC 3986 permits them in the query).
    from urllib.parse import parse_qs, urlparse

    location_header = resp.headers["Location"]
    assert location_header.startswith("/auth/authorize?")
    location = urlparse(location_header)
    assert location.path == "/auth/authorize"
    query = parse_qs(location.query)
    # Same-origin fast path: client_id passes through untranslated, and every
    # original parameter is preserved.
    assert query["client_id"] == ["https://claude.ai"]
    assert query["state"] == ["xyz"]
    assert query["redirect_uri"] == ["https://claude.ai/api/mcp/auth_callback"]
    assert query["code_challenge_method"] == ["S256"]


async def test_ha_auth_authorize_uses_isolated_cimd_session(
    unified_view_client_factory, monkeypatch
):
    """Resolve public client metadata outside the webhook forwarding pool."""
    relay_session = object()
    cimd_session = object()
    resolver = AsyncMock(return_value="https://claude.ai")
    monkeypatch.setattr(oauth_ha_auth, "resolve_forward_client_id", resolver)
    client = await unified_view_client_factory(
        mode="ha_auth",
        session=relay_session,
        cimd_session=cimd_session,
    )

    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        "?response_type=code&client_id=https%3A%2F%2Fclaude.ai"
        "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback",
        allow_redirects=False,
    )

    assert resp.status == 302
    assert resolver.await_args.args[0] is cimd_session


async def test_ha_auth_authorize_preserves_repeated_resource_parameters(
    unified_view_client_factory,
):
    """Preserve both RFC 8707 resource values when redirecting into core."""
    client = await unified_view_client_factory(mode="ha_auth")
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        "?response_type=code&client_id=https%3A%2F%2Fclaude.ai"
        "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
        "&code_challenge=" + "a" * 43 + "&code_challenge_method=S256"
        "&resource=https%3A%2F%2Fha.example%2Ffirst"
        "&resource=https%3A%2F%2Fha.example%2Fsecond",
        allow_redirects=False,
    )

    from urllib.parse import parse_qs, urlparse

    assert resp.status == 302
    query = parse_qs(urlparse(resp.headers["Location"]).query)
    assert query["resource"] == [
        "https://ha.example/first",
        "https://ha.example/second",
    ]


async def test_ha_auth_token_forwards_translated_client_id(
    unified_view_client_factory, monkeypatch
):
    """Translate and forward the authorization-code token form to core."""
    session = _CoreTokenSession()
    dcr_key = b"d" * 32
    client_id = oauth_dcr.mint_client_id(dcr_key, ["https://a.example/cb"])
    monkeypatch.setattr(
        oauth_ha_auth,
        "core_token_base_url",
        lambda _hass: "https://core.example",
    )
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=dcr_key
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": client_id,
            "redirect_uri": "https://a.example/cb",
        },
    )

    assert resp.status == 200
    assert await resp.json() == {"access_token": "x"}
    call = session.calls[0]
    assert call["url"] == "https://core.example/auth/token"
    assert call["data"]["grant_type"] == "authorization_code"
    assert call["data"]["redirect_uri"] == "https://a.example/cb"
    assert call["data"]["client_id"] == "https://a.example"


async def test_ha_auth_refresh_forwards_translated_client_id(
    unified_view_client_factory, monkeypatch
):
    """Re-derive and forward the translated client ID for a refresh grant."""
    session = _CoreTokenSession()
    dcr_key = b"d" * 32
    client_id = oauth_dcr.mint_client_id(dcr_key, ["https://a.example/cb"])
    monkeypatch.setattr(
        oauth_ha_auth,
        "core_token_base_url",
        lambda _hass: "https://core.example",
    )
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=dcr_key
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": client_id,
        },
    )

    assert resp.status == 200
    call = session.calls[0]
    assert call["data"]["grant_type"] == "refresh_token"
    assert call["data"]["refresh_token"] == "refresh-1"
    assert call["data"]["client_id"] == "https://a.example"


async def test_ha_auth_authorization_code_without_redirect_uses_default_path(
    unified_view_client_factory, monkeypatch
):
    """Do not mistake a malformed code exchange for a refresh grant."""
    session = _CoreTokenSession()
    dcr_key = b"d" * 32
    client_id = oauth_dcr.mint_client_id(dcr_key, ["https://a.example/cb"])
    monkeypatch.setattr(
        oauth_ha_auth,
        "core_token_base_url",
        lambda _hass: "https://core.example",
    )
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=dcr_key
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": client_id,
        },
        allow_redirects=False,
    )

    assert resp.status == 307
    assert resp.headers["Location"] == "/auth/token"
    assert session.calls == []


async def test_ha_auth_token_timeout_returns_temporarily_unavailable(
    unified_view_client_factory, monkeypatch
):
    """Map a core token-forward timeout to OAuth temporarily_unavailable.

    Uses a TRANSLATED identity — only translated exchanges are forwarded
    server-side; untranslated ones 307 to core before any session use (see
    test_ha_auth_token_untranslated_redirects_to_core).
    """
    session = _CoreTokenSession(error=TimeoutError())
    dcr_key = b"d" * 32
    client_id = oauth_dcr.mint_client_id(dcr_key, ["https://a.example/cb"])
    monkeypatch.setattr(
        oauth_ha_auth,
        "core_token_base_url",
        lambda _hass: "https://core.example",
    )
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=dcr_key
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": client_id,
            "redirect_uri": "https://a.example/cb",
        },
    )

    assert resp.status == 503
    assert (await resp.json())["error"] == "temporarily_unavailable"


async def test_ha_auth_token_untranslated_redirects_to_core(
    unified_view_client_factory,
):
    """Untranslated exchanges 307 to core's /auth/token on the request origin.

    Core must observe the CLIENT's address (#2213 review by Patch76): its
    wrong-login notifications, ban counters, trusted_networks refresh
    validation, and last_used_ip all key on request.remote — so the exchange
    must not be proxied when no body rewrite is needed. 307 (not 308):
    method+body preserved without the default cacheability that could outlive
    a later auth-mode switch.
    """
    session = _CoreTokenSession()
    client = await unified_view_client_factory(mode="ha_auth", session=session)

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": "https://claude.ai",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        },
        allow_redirects=False,
    )

    assert resp.status == 307
    # RELATIVE reference pinned (#2213 review round 2): an absolute target
    # would put forwarded-header derivation in the credential path.
    assert resp.headers["Location"] == "/auth/token"
    assert resp.headers["Cache-Control"] == "no-store"
    assert session.calls == []  # nothing proxied


async def test_ha_auth_refresh_untranslated_redirects_to_core(
    unified_view_client_factory,
):
    """A plain refresh grant (no translatable identity) also 307s to core."""
    session = _CoreTokenSession()
    client = await unified_view_client_factory(mode="ha_auth", session=session)

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": "https://claude.ai",
        },
        allow_redirects=False,
    )

    assert resp.status == 307
    assert resp.headers["Location"] == "/auth/token"
    assert session.calls == []


async def test_scoped_authorize_still_autoapproves_in_none_mode(
    unified_view_client_factory,
):
    """None mode keeps the existing invisible auto-approve behavior."""
    client = await unified_view_client_factory(mode="none")
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        "?response_type=code&client_id=anything"
        "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
        "&code_challenge=" + "a" * 43 + "&code_challenge_method=S256",
        allow_redirects=False,
    )
    assert resp.status == 302
    assert "code=" in resp.headers["Location"]


async def test_authorize_ignores_resource_parameter(unified_view_client_factory):
    """RFC 8707: clients MUST send ``resource``; the AS must tolerate it."""
    client = await unified_view_client_factory(mode="none")
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        "?response_type=code&client_id=x"
        "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
        "&code_challenge=" + "a" * 43 + "&code_challenge_method=S256"
        "&resource=https%3A%2F%2Fha.example%2Fapi%2Fwebhook%2Fabc",
        allow_redirects=False,
    )

    assert resp.status == 302
    _, params = _parse_location(resp.headers["Location"])
    assert params["code"]


async def test_token_ignores_resource_parameter(unified_view_client_factory):
    """RFC 8707: token requests tolerate the required ``resource`` field."""
    client = await unified_view_client_factory(mode="none")
    verifier = "fixed-verifier-" + "v" * 48
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    authorize_resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        "?response_type=code&client_id=x"
        "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
        f"&code_challenge={challenge}&code_challenge_method=S256"
        "&resource=https%3A%2F%2Fha.example%2Fapi%2Fwebhook%2Fabc",
        allow_redirects=False,
    )
    assert authorize_resp.status == 302
    _, authorize_params = _parse_location(authorize_resp.headers["Location"])
    code = authorize_params["code"]

    token_resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_REDIRECT,
            "code_verifier": verifier,
            "resource": "https://ha.example/api/webhook/abc",
        },
    )
    assert token_resp.status == 200


AUTH_QS = (
    "?response_type=code&client_id=anything"
    "&code_challenge=" + "a" * 43 + "&code_challenge_method=S256&state=s1"
)


async def test_none_mode_any_valid_https_redirect_autoapproves(
    unified_view_client_factory,
):
    """Maintainer decision 2026-08-14: none mode is secret-URL-only trust —
    ChatGPT, Spark, and any other provider auto-approve invisibly, same as
    claude.ai."""
    client = await unified_view_client_factory(mode="none")
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        + AUTH_QS
        + "&redirect_uri=https%3A%2F%2Fchatgpt.example%2Fconnector%2Fcb",
        allow_redirects=False,
    )
    assert resp.status == 302
    assert "code=" in resp.headers["Location"]
    assert "state=s1" in resp.headers["Location"]


async def test_none_mode_token_undecodable_body_returns_400(
    unified_view_client_factory,
):
    """#2219 codex review: aiohttp raises LookupError (not ValueError) when
    Content-Type names an unknown charset. Driven through the real route so
    reverting the guard on THIS call site fails here."""
    client = await unified_view_client_factory(mode="none")

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data=b"grant_type=authorization_code",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=nope"},
    )

    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_request"


async def test_ha_auth_token_undecodable_body_returns_400(
    unified_view_client_factory,
):
    """Same guard on the ha_auth token route (its own call site)."""
    client = await unified_view_client_factory(
        mode="ha_auth", session=_CoreTokenSession()
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data=b"grant_type=refresh_token",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=nope"},
    )

    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_request"


async def test_none_mode_loopback_redirect_autoapproves(unified_view_client_factory):
    """Native/CLI loopback callbacks (RFC 8252) complete invisibly too."""
    client = await unified_view_client_factory(mode="none")
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        + AUTH_QS
        + "&redirect_uri=http%3A%2F%2Flocalhost%3A61264%2Fcallback",
        allow_redirects=False,
    )
    assert resp.status == 302
    assert "code=" in resp.headers["Location"]


async def test_none_mode_malformed_redirect_still_400s(unified_view_client_factory):
    """Reject malformed redirect URIs before issuing a none-mode code."""
    client = await unified_view_client_factory(mode="none")
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        + AUTH_QS
        + "&redirect_uri=https%3A%2F%2Fevil.example%2Fcb%23frag",
    )
    assert resp.status == 400


async def test_ha_auth_refresh_pre_envelope_loopback_dcr_gets_invalid_grant(
    unified_view_client_factory,
):
    """A PRE-#2248 loopback-only refresh token still gets a local invalid_grant.

    The bare token names no identity and the registration cannot re-derive the
    ephemeral loopback origin core bound it to, so answer here instead of
    307ing a guaranteed failure into core's failed-login accounting (#2213
    review round 2). One re-authorize mints an envelope-carrying token, which
    test_ha_auth_refresh_envelope_restores_core_token_and_identity covers."""
    session = _CoreTokenSession()
    dcr_key = b"d" * 32
    client_id = oauth_dcr.mint_client_id(
        dcr_key, ["http://localhost/callback", "http://127.0.0.1/callback"]
    )
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=dcr_key
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": client_id,
        },
        allow_redirects=False,
    )

    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_grant"
    assert session.calls == []  # never reached core


async def test_ha_auth_refresh_pre_envelope_multi_origin_dcr_gets_invalid_grant(
    unified_view_client_factory,
):
    """Reject a pre-#2248 Spark refresh locally: no envelope, no re-derivable
    origin. The error names the one-time re-authorize that fixes it."""
    session = _CoreTokenSession()
    dcr_key = b"d" * 32
    client_id = oauth_dcr.mint_client_id(
        dcr_key,
        [
            "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
            "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
        ],
    )
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=dcr_key
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": client_id,
        },
        allow_redirects=False,
    )

    body = await resp.json()
    assert resp.status == 400
    assert body["error"] == "invalid_grant"
    assert "predates the signed identity envelope" in body["error_description"]
    assert session.calls == []  # never reached core


async def test_ha_auth_refresh_pre_envelope_hybrid_gets_local_invalid_grant(
    unified_view_client_factory,
):
    """#2217 review: a hybrid (web + loopback) registration refreshing a
    pre-#2248 token answers invalid_grant locally — the derivation returns
    UNREPRODUCIBLE; core is never contacted with a possibly mismatched
    identity."""
    session = _CoreTokenSession()
    dcr_key = b"d" * 32
    client_id = oauth_dcr.mint_client_id(
        dcr_key, ["http://localhost/callback", "https://a.example/cb"]
    )
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=dcr_key
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": client_id,
        },
        allow_redirects=False,
    )

    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_grant"
    assert session.calls == []


async def test_ha_auth_refresh_pre_envelope_multi_origin_cimd_gets_invalid_grant(
    unified_view_client_factory, monkeypatch
):
    """#2217 review sweep (the consensus defect): a VERIFIED CIMD identity
    with no reproducible origin answers invalid_grant locally on a pre-#2248
    token, exactly like the equivalent DCR blob — previously only blobs hit
    the guard, so these refreshes were 307'd into core's failed-login
    accounting on every token expiry."""

    async def fetch_redirects(_session, _client_id):
        return [
            "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
            "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
        ]

    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch_redirects)
    session = _CoreTokenSession()
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, cimd_session=object()
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": "https://spark.example/client-metadata.json",
        },
        allow_redirects=False,
    )

    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_grant"
    assert session.calls == []  # never reached core


async def test_ha_auth_refresh_with_redirect_uri_translates_like_authorize(
    unified_view_client_factory, monkeypatch
):
    """A refresh that DOES carry a redirect_uri translates from it exactly
    like the authorize/code legs — this is what keeps multi-origin clients
    refreshable even though the redirect-less derivation cannot pick an
    origin for them."""
    session = _CoreTokenSession()
    dcr_key = b"d" * 32
    monkeypatch.setattr(
        oauth_ha_auth,
        "core_token_base_url",
        lambda _hass: "https://core.example",
    )
    redirect = "https://oauth-redirect.googleusercontent.com/r/ha-mcp"
    client_id = oauth_dcr.mint_client_id(
        dcr_key,
        [redirect, "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp"],
    )
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=dcr_key
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": client_id,
            "redirect_uri": redirect,
        },
        allow_redirects=False,
    )

    assert resp.status == 200
    assert len(session.calls) == 1
    assert (
        session.calls[0]["data"]["client_id"]
        == "https://oauth-redirect.googleusercontent.com"
    )


async def test_ha_auth_refresh_uses_isolated_cimd_session(
    unified_view_client_factory, monkeypatch
):
    """The refresh leg resolves CIMD metadata outside the forwarding pool too
    (token-leg twin of the authorize-leg pin)."""
    relay_session = _CoreTokenSession()
    cimd_session = object()
    deriver = AsyncMock(return_value=oauth_ha_auth.RefreshDisposition.PASSTHROUGH)
    monkeypatch.setattr(oauth_ha_auth, "translated_client_id_for_refresh", deriver)
    client = await unified_view_client_factory(
        mode="ha_auth", session=relay_session, cimd_session=cimd_session
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": CLAUDE_CLIENT_ID,
        },
        allow_redirects=False,
    )

    assert resp.status == 307
    assert deriver.await_args.args[0] is cimd_session


# ---------------------------------------------------------------------------
# ha_auth refresh-token envelope (issue #2248)
# ---------------------------------------------------------------------------

DCR_KEY = b"d" * 32
LOOPBACK_CALLBACK = "http://127.0.0.1:19877/mcp/oauth/callback"
CORE_BODY_WITH_REFRESH = (
    b'{"access_token":"core-access","token_type":"Bearer",'
    b'"expires_in":1800,"refresh_token":"core-refresh"}'
)


def _pin_core_token_base(monkeypatch) -> None:
    """Point the server-side forward at a fixed core base URL."""
    monkeypatch.setattr(
        oauth_ha_auth,
        "core_token_base_url",
        lambda _hass: "https://core.example",
    )


@pytest.mark.parametrize(
    ("presented_redirect", "presented_origin"),
    [
        # The port the client registered, then an RFC 8252 runtime port.
        (LOOPBACK_CALLBACK, "http://127.0.0.1:19877"),
        ("http://127.0.0.1:54321/mcp/oauth/callback", "http://127.0.0.1:54321"),
    ],
)
async def test_ha_auth_code_leg_wraps_refresh_token_for_loopback_client(
    unified_view_client_factory, monkeypatch, presented_redirect, presented_origin
):
    """The code leg hands a loopback client an envelope, not core's raw token.

    Core binds the grant to the translated runtime origin, which a
    redirect_uri-less refresh cannot re-derive (the #2248 bug). Recording it in
    the token is what closes that.
    """
    session = _CoreTokenSession(body=CORE_BODY_WITH_REFRESH)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    _pin_core_token_base(monkeypatch)
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": client_id,
            "redirect_uri": presented_redirect,
        },
    )

    assert resp.status == 200
    assert session.calls[0]["data"]["client_id"] == presented_origin
    body = await resp.json()
    assert body["access_token"] == "core-access"
    assert body["expires_in"] == 1800
    assert oauth_ha_auth.unwrap_refresh_token(
        DCR_KEY, body["refresh_token"], client_id
    ) == ("core-refresh", presented_origin)


@pytest.mark.parametrize(
    "redirect_uris",
    [
        pytest.param([LOOPBACK_CALLBACK], id="loopback-only"),
        pytest.param(
            [
                "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
                "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
            ],
            id="multi-origin",
        ),
        pytest.param(
            [LOOPBACK_CALLBACK, "https://a.example/cb"],
            id="hybrid",
        ),
    ],
)
async def test_ha_auth_refresh_envelope_restores_core_token_and_identity(
    unified_view_client_factory, monkeypatch, redirect_uris
):
    """A redirect_uri-less refresh proxies with the envelope's exact pair.

    Every registration shape the pre-#2248 derivation had to reject refreshes
    here: the identity comes out of the envelope, never out of the registered
    list, so there is nothing left to be ambiguous about.
    """
    session = _CoreTokenSession()
    client_id = oauth_dcr.mint_client_id(DCR_KEY, redirect_uris)
    envelope = oauth_ha_auth.wrap_refresh_token(
        DCR_KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )
    _pin_core_token_base(monkeypatch)
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": envelope,
            "client_id": client_id,
        },
        allow_redirects=False,
    )

    assert resp.status == 200
    assert len(session.calls) == 1
    forwarded = session.calls[0]["data"]
    assert forwarded["client_id"] == "http://127.0.0.1:54321"
    assert forwarded["refresh_token"] == "core-refresh"


async def test_ha_auth_refresh_envelope_wins_over_a_presented_redirect_uri(
    unified_view_client_factory, monkeypatch
):
    """The recorded identity beats a redirect the client happens to send.

    Core bound the token to the envelope's client_id; translating the presented
    redirect instead would forward a different identity and fail.
    """
    session = _CoreTokenSession()
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    envelope = oauth_ha_auth.wrap_refresh_token(
        DCR_KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )
    _pin_core_token_base(monkeypatch)
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": envelope,
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:60999/mcp/oauth/callback",
        },
        allow_redirects=False,
    )

    assert resp.status == 200
    assert session.calls[0]["data"]["client_id"] == "http://127.0.0.1:54321"


async def test_ha_auth_refresh_tampered_envelope_is_answered_locally(
    unified_view_client_factory,
):
    """A forged envelope is answered here, not relayed and not re-derived.

    It carries our prefix, so it is INVALID rather than ABSENT: core cannot
    redeem it either, and the pre-envelope derivation would answer a message
    about registration shape that has nothing to do with what happened.
    """
    session = _CoreTokenSession()
    client_id = oauth_dcr.mint_client_id(DCR_KEY, ["https://a.example/cb"])
    envelope = oauth_ha_auth.wrap_refresh_token(
        DCR_KEY, "core-refresh", "https://a.example", client_id
    )
    body, _, signature = envelope.rpartition(".")
    tampered = f"{body}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tampered,
            "client_id": client_id,
        },
        allow_redirects=False,
    )

    payload = await resp.json()
    assert resp.status == 400
    assert payload["error"] == "invalid_grant"
    assert "could not be verified" in payload["error_description"]
    assert session.calls == []


async def test_ha_auth_refresh_envelope_rejected_for_another_client_id(
    unified_view_client_factory,
):
    """An envelope presented under a different client_id is not honoured.

    A single-web-origin registration is used deliberately: its pre-envelope
    derivation would 307 into core, so a plain invalid_grant here can only
    come from the envelope's own verification.
    """
    session = _CoreTokenSession()
    minted_for = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    other_client = oauth_dcr.mint_client_id(DCR_KEY, ["https://a.example/cb"])
    envelope = oauth_ha_auth.wrap_refresh_token(
        DCR_KEY, "core-refresh", "http://127.0.0.1:54321", minted_for
    )
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": envelope,
            "client_id": other_client,
        },
        allow_redirects=False,
    )

    payload = await resp.json()
    assert resp.status == 400
    assert payload["error"] == "invalid_grant"
    assert "could not be verified" in payload["error_description"]
    assert session.calls == []


async def test_ha_auth_non_200_core_response_is_relayed_unwrapped(
    unified_view_client_factory, monkeypatch
):
    """Only a 200 gets its refresh_token wrapped; errors relay byte for byte."""
    session = _CoreTokenSession(body=CORE_BODY_WITH_REFRESH, status=400)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    _pin_core_token_base(monkeypatch)
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": client_id,
            "redirect_uri": LOOPBACK_CALLBACK,
        },
    )

    assert resp.status == 400
    assert (await resp.json())["refresh_token"] == "core-refresh"


# ---------------------------------------------------------------------------
# ha_auth code leg: same-origin CIMD identities that cannot refresh (#2248)
# ---------------------------------------------------------------------------

CIMD_CLIENT_ID = "https://app.example/cimd.json"
CIMD_SAME_ORIGIN_REDIRECT = "https://app.example/cb"
HYBRID_CIMD_REDIRECTS = [CIMD_SAME_ORIGIN_REDIRECT, "https://eu.example/cb"]


def _pin_cimd_document(monkeypatch, redirect_uris: list[str]) -> None:
    """Serve ``redirect_uris`` for every CIMD lookup on both token legs."""

    async def fetch_redirects(_session, _client_id):
        return list(redirect_uris)

    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch_redirects)


async def test_ha_auth_code_leg_proxies_a_hybrid_cimd_same_origin_exchange(
    unified_view_client_factory, monkeypatch
):
    """A hybrid CIMD identity presenting its same-origin redirect gets wrapped.

    The authorize leg's same-origin fast path returns the client_id without
    fetching, so this exchange used to 307 and hand back core's RAW refresh
    token — whose redirect-less refresh then derived UNREPRODUCIBLE (two web
    origins) and answered invalid_grant forever, under a message claiming
    re-authorizing would help. The code leg now pays that one fetch, proxies,
    and records the identity in the token instead.
    """
    session = _CoreTokenSession(body=CORE_BODY_WITH_REFRESH)
    _pin_cimd_document(monkeypatch, HYBRID_CIMD_REDIRECTS)
    _pin_core_token_base(monkeypatch)
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, cimd_session=object(), dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": CIMD_CLIENT_ID,
            "redirect_uri": CIMD_SAME_ORIGIN_REDIRECT,
        },
    )

    assert resp.status == 200
    assert len(session.calls) == 1  # proxied, not 307'd
    assert session.calls[0]["data"]["client_id"] == CIMD_CLIENT_ID
    body = await resp.json()
    assert oauth_ha_auth.unwrap_refresh_token(
        DCR_KEY, body["refresh_token"], CIMD_CLIENT_ID
    ) == ("core-refresh", CIMD_CLIENT_ID)


async def test_ha_auth_refresh_of_a_hybrid_cimd_envelope_keeps_the_client_id(
    unified_view_client_factory, monkeypatch
):
    """The refresh leg of that exchange proxies the untranslated client_id.

    The envelope names the identity core bound the grant to — here the
    client_id itself — so the derivation that would answer UNREPRODUCIBLE is
    never consulted.
    """
    session = _CoreTokenSession()
    _pin_cimd_document(monkeypatch, HYBRID_CIMD_REDIRECTS)
    _pin_core_token_base(monkeypatch)
    envelope = oauth_ha_auth.wrap_refresh_token(
        DCR_KEY, "core-refresh", CIMD_CLIENT_ID, CIMD_CLIENT_ID
    )
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, cimd_session=object(), dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": envelope,
            "client_id": CIMD_CLIENT_ID,
        },
        allow_redirects=False,
    )

    assert resp.status == 200
    forwarded = session.calls[0]["data"]
    assert forwarded["client_id"] == CIMD_CLIENT_ID
    assert forwarded["refresh_token"] == "core-refresh"


async def test_ha_auth_code_leg_still_307s_a_same_origin_only_cimd_client(
    unified_view_client_factory, monkeypatch
):
    """A CIMD document with only same-origin redirects keeps the 307.

    Its redirect-less refresh re-derives PASSTHROUGH, so there is nothing to
    record and core must keep seeing the client's own address.
    """
    session = _CoreTokenSession(body=CORE_BODY_WITH_REFRESH)
    _pin_cimd_document(monkeypatch, [CIMD_SAME_ORIGIN_REDIRECT])
    _pin_core_token_base(monkeypatch)
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, cimd_session=object(), dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": CIMD_CLIENT_ID,
            "redirect_uri": CIMD_SAME_ORIGIN_REDIRECT,
        },
        allow_redirects=False,
    )

    assert resp.status == 307
    assert resp.headers["Location"] == "/auth/token"
    assert session.calls == []


async def test_ha_auth_refresh_leg_rewraps_a_rotated_core_refresh_token(
    unified_view_client_factory, monkeypatch
):
    """A refresh that rotates core's token comes back wrapped again.

    ``rewrite_token_response_body`` runs on EVERY forwarded 200, not just the
    code leg, so a core that starts rotating refresh tokens does not hand the
    client a bare one it could never refresh with.
    """
    session = _CoreTokenSession(
        body=b'{"access_token":"core-access-2","refresh_token":"core-refresh-2"}'
    )
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    envelope = oauth_ha_auth.wrap_refresh_token(
        DCR_KEY, "core-refresh-1", "http://127.0.0.1:54321", client_id
    )
    _pin_core_token_base(monkeypatch)
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": envelope,
            "client_id": client_id,
        },
        allow_redirects=False,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["access_token"] == "core-access-2"
    assert oauth_ha_auth.unwrap_refresh_token(
        DCR_KEY, body["refresh_token"], client_id
    ) == ("core-refresh-2", "http://127.0.0.1:54321")


async def test_ha_auth_refresh_without_a_dcr_key_forwards_instead_of_raising(
    unified_view_client_factory,
):
    """ha_auth with no DCR signing key skips the envelope check entirely.

    Registration mints the key, so ha_auth can be live before any client has
    registered. The envelope path must simply not run rather than fail.
    """
    session = _CoreTokenSession()
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=None
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "core-opaque-refresh-token",
            "client_id": CLAUDE_CLIENT_ID,
        },
        allow_redirects=False,
    )

    assert resp.status == 307
    assert resp.headers["Location"] == "/auth/token"
    assert session.calls == []


# ---------------------------------------------------------------------------
# ha_auth revocation (RFC 7009 action=revoke, issue #2248)
# ---------------------------------------------------------------------------


async def test_ha_auth_revoke_unwraps_the_envelope_before_forwarding(
    unified_view_client_factory, monkeypatch
):
    """Core must revoke ITS token, not the envelope we handed the client.

    ``/auth/token`` answers 200 to ``action=revoke`` even for a token it has
    never seen, so forwarding the envelope would report success while leaving
    the session live. Core's real revoke answer is an empty 200; the stub
    returns a token-shaped body instead to pin that the response rewrite is
    skipped on this path rather than wrapping something that is not a grant.
    """
    session = _CoreTokenSession(body=CORE_BODY_WITH_REFRESH)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    envelope = oauth_ha_auth.wrap_refresh_token(
        DCR_KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )
    _pin_core_token_base(monkeypatch)
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={"action": "revoke", "token": envelope},
        allow_redirects=False,
    )

    assert resp.status == 200
    assert session.calls[0]["data"]["token"] == "core-refresh"
    assert (await resp.json())["refresh_token"] == "core-refresh"


async def test_ha_auth_revoke_of_a_plain_token_is_unchanged(
    unified_view_client_factory,
):
    """A revoke carrying no envelope 307s to core exactly as it did before."""
    session = _CoreTokenSession()
    client = await unified_view_client_factory(
        mode="ha_auth", session=session, dcr_key=DCR_KEY
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={"action": "revoke", "token": "core-opaque-refresh-token"},
        allow_redirects=False,
    )

    assert resp.status == 307
    assert resp.headers["Location"] == "/auth/token"
    assert session.calls == []
