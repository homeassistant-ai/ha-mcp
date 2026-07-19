"""Unit tests for the none-mode auto-approve OAuth server (issue #1969).

Covers ``custom_components/ha_mcp_tools/oauth_autoapprove.py``: the invisible
``/authorize`` (issues a PKCE code + 302, no UI) and ``/token`` (public-client
PKCE exchange, cosmetic opaque token) views, the ``AutoApproveProvider`` code
lifecycle, and — most importantly — the open-redirect gate layered on top of
:func:`oauth_legacy._is_valid_redirect_uri` (a strict exact-match allowlist of
known MCP callbacks).

Home Assistant / aiohttp are stubbed via ``_embedded_stubs``. ``yarl`` (an
aiohttp dependency also absent here) is stubbed with the tiny
``URL.update_query`` surface ``_redirect_with`` uses — same convention as
``test_oauth_legacy_component.py``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

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

    def __str__(self) -> str:
        return self._url


if "yarl" not in sys.modules:
    _yarl = ModuleType("yarl")
    _yarl.URL = _FakeURL  # type: ignore[attr-defined]
    sys.modules["yarl"] = _yarl

import custom_components.ha_mcp_tools.oauth_autoapprove as aa  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_WEBHOOK,
    DOMAIN,
)

CLAUDE_CLIENT_ID = "https://claude.ai/api/mcp/client_metadata"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


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


# ---------------------------------------------------------------------------
# Open-redirect gate
# ---------------------------------------------------------------------------


class TestRedirectValidation:
    def test_allowlisted_claude_callback_is_valid(self):
        # The known MCP callback is accepted.
        assert aa._is_valid_autoapprove_redirect(CLAUDE_REDIRECT) is True

    def test_same_origin_non_allowlisted_is_rejected(self):
        # SECURITY: client_id is attacker-controlled, so "redirect shares the
        # client_id origin" is NOT a trust anchor — an attacker sets both to a
        # site they own and bounces a victim there (open redirect). Only the
        # allowlist is trusted, so a non-allowlisted target is rejected.
        assert aa._is_valid_autoapprove_redirect("https://myclient.example/cb") is False

    def test_cross_origin_redirect_is_rejected(self):
        # SECURITY: the classic open-redirect / phishing target.
        assert aa._is_valid_autoapprove_redirect("https://evil.example/steal") is False

    def test_non_allowlisted_https_target_is_rejected(self):
        assert (
            aa._is_valid_autoapprove_redirect("https://somewhere.example/cb") is False
        )

    def test_redirect_failing_scheme_floor_is_rejected(self):
        # http non-loopback fails the oauth_legacy floor before the allowlist check.
        assert (
            aa._is_valid_autoapprove_redirect("http://claude.ai/api/mcp/auth_callback")
            is False
        )


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
        query = _authorize_query(
            code_challenge=challenge
        )  # allowlisted CLAUDE_REDIRECT
        resp = await view.get(_get_request(query))

        assert resp.status == 302
        base, params = _parse_location(resp.headers["Location"])
        assert base == CLAUDE_REDIRECT
        assert params["state"] == "st-1"
        # The issued code is real: it consumes with the matching verifier.
        assert provider.consume_code(params["code"], CLAUDE_REDIRECT, verifier) is True

    async def test_allowlisted_claude_redirect_is_approved(self):
        hass = _live_hass()
        view = aa.AutoApproveAuthorizeView(hass)
        resp = await view.get(_get_request(_authorize_query()))
        assert resp.status == 302
        base, _ = _parse_location(resp.headers["Location"])
        assert base == CLAUDE_REDIRECT

    async def test_cross_origin_redirect_is_400_not_redirect(self):
        # SECURITY: never 302 to an unvalidated target — answer 400 in place.
        hass = _live_hass()
        view = aa.AutoApproveAuthorizeView(hass)
        query = _authorize_query(redirect_uri="https://evil.example/steal")
        resp = await view.get(_get_request(query))
        assert resp.status == 400
        assert "Location" not in resp.headers

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
