"""Unit tests for the component-side legacy OAuth 2.1 authorization server
(``custom_components/ha_mcp_tools/oauth_legacy.py``).

Covers the self-issued, HMAC-signed token codec, PKCE (S256) authorization
codes, static client_id/client_secret authentication, the root ``/authorize``
+ ``/token`` views, ``bind_legacy_views`` route-ownership/rebind semantics,
and the credential-minting helper in ``embedded_entry.py`` that provisions
the legacy client_id/client_secret/signing_key on first use.

Home Assistant / aiohttp are stubbed via ``_embedded_stubs`` (same convention
as ``test_embedded_setup.py`` / ``test_embedded_entry.py``). ``yarl`` (an
aiohttp dependency, also not installed in this unit-test environment) is
stubbed here with the tiny ``URL.update_query`` surface
``AuthorizeView._redirect_with`` actually uses.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from ._embedded_stubs import install

install()


class _FakeURL:
    """Stand-in for ``yarl.URL`` covering only ``update_query`` + ``str()`` --
    the surface ``AuthorizeView._redirect_with`` uses to append ``code``/
    ``state``/``error`` query params onto the client's redirect_uri."""

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

import custom_components.ha_mcp_tools.embedded_entry as eentry  # noqa: E402
import custom_components.ha_mcp_tools.oauth_legacy as oauth_legacy  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    OPT_OAUTH_CLIENT_ID,
    OPT_OAUTH_CLIENT_SECRET,
    OPT_OAUTH_REGENERATE,
    WEBHOOK_AUTH_LEGACY,
)

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
REDIRECT_URI = "https://client.example.com/cb"


def _make_provider(
    *,
    client_id: str = CLIENT_ID,
    client_secret: str = CLIENT_SECRET,
    active: bool = True,
    signing_key: bytes | None = None,
) -> oauth_legacy.LegacyOAuthProvider:
    key = signing_key if signing_key is not None else secrets.token_bytes(32)
    mode = WEBHOOK_AUTH_LEGACY if active else "ha_auth"
    return oauth_legacy.LegacyOAuthProvider(client_id, client_secret, key, lambda: mode)


def _pkce_pair() -> tuple[str, str]:
    """A valid (code_verifier, code_challenge) pair per RFC 7636 S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _authorize_query(**overrides: str) -> dict[str, str]:
    _, challenge = _pkce_pair()
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": "s1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "response_type": "code",
    }
    params.update(overrides)
    return params


def _make_get_request(query: dict[str, str]) -> MagicMock:
    request = MagicMock(name="Request")
    request.query = query
    return request


def _extract_query_param(url: str, key: str) -> str:
    return parse_qs(urlparse(url).query)[key][0]


# ---------------------------------------------------------------------------
# Token codec (issue/validate)
# ---------------------------------------------------------------------------


class TestTokenCodec:
    def test_issue_and_validate_access_token(self):
        provider = _make_provider()
        token = provider.issue_access_token()
        assert provider.validate_access_token(token) is True

    def test_issue_and_validate_refresh_token(self):
        provider = _make_provider()
        token = provider.issue_refresh_token()
        assert provider.validate_refresh_token(token) is True

    def test_access_token_rejected_as_refresh_token(self):
        provider = _make_provider()
        token = provider.issue_access_token()
        assert provider.validate_refresh_token(token) is False

    def test_refresh_token_rejected_as_access_token(self):
        provider = _make_provider()
        token = provider.issue_refresh_token()
        assert provider.validate_access_token(token) is False

    def test_tampered_signature_rejected(self):
        provider = _make_provider()
        token = provider.issue_access_token()
        body, sig = token.rsplit(".", 1)
        tampered_char = "A" if sig[0] != "A" else "B"
        tampered = f"{tampered_char}{sig[1:]}"
        assert provider.validate_access_token(f"{body}.{tampered}") is False

    def test_tampered_body_rejected(self):
        provider = _make_provider()
        token = provider.issue_access_token()
        body, sig = token.rsplit(".", 1)
        assert provider.validate_access_token(f"{body}extra.{sig}") is False

    def test_malformed_token_without_separator_rejected(self):
        provider = _make_provider()
        assert provider.validate_access_token("not-a-real-token") is False

    def test_expired_token_rejected(self):
        provider = _make_provider()
        with patch.object(oauth_legacy.time, "time", return_value=1_000_000.0):
            token = provider.issue_access_token()
        # Real time.time() (module not patched here) is far past the 1-hour
        # TTL computed from the patched issuance time.
        assert provider.validate_access_token(token) is False

    def test_token_from_rotated_client_id_rejected(self):
        # Same signing key, different provider instance bound to a NEW
        # client_id (simulates a credential rotation): the old token must
        # not validate against the new identity.
        key = secrets.token_bytes(32)
        old_provider = _make_provider(client_id="old-client", signing_key=key)
        token = old_provider.issue_access_token()
        new_provider = _make_provider(client_id="new-client", signing_key=key)
        assert new_provider.validate_access_token(token) is False


class TestValidateBearer:
    def test_accepts_valid_bearer_access_token(self):
        provider = _make_provider()
        token = provider.issue_access_token()
        request = MagicMock()
        request.headers = {"Authorization": f"Bearer {token}"}
        assert provider.validate_bearer(request) is True

    def test_rejects_missing_authorization_header(self):
        provider = _make_provider()
        request = MagicMock()
        request.headers = {}
        assert provider.validate_bearer(request) is False

    def test_rejects_non_bearer_scheme(self):
        provider = _make_provider()
        request = MagicMock()
        request.headers = {"Authorization": "Basic abc123"}
        assert provider.validate_bearer(request) is False

    def test_rejects_refresh_token_presented_as_bearer(self):
        provider = _make_provider()
        token = provider.issue_refresh_token()
        request = MagicMock()
        request.headers = {"Authorization": f"Bearer {token}"}
        assert provider.validate_bearer(request) is False


# ---------------------------------------------------------------------------
# PKCE (S256) authorization codes
# ---------------------------------------------------------------------------


class TestPKCECodes:
    def test_consume_with_correct_verifier_succeeds(self):
        provider = _make_provider()
        verifier, challenge = _pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        assert provider.consume_code(code, REDIRECT_URI, verifier) is True

    def test_consume_with_wrong_verifier_fails(self):
        provider = _make_provider()
        verifier, challenge = _pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        other_verifier, _ = _pkce_pair()
        assert provider.consume_code(code, REDIRECT_URI, other_verifier) is False

    def test_code_is_one_shot(self):
        provider = _make_provider()
        verifier, challenge = _pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        assert provider.consume_code(code, REDIRECT_URI, verifier) is True
        assert provider.consume_code(code, REDIRECT_URI, verifier) is False

    def test_expired_code_rejected(self):
        provider = _make_provider()
        verifier, challenge = _pkce_pair()
        with patch.object(oauth_legacy.time, "time", return_value=1_000_000.0):
            code = provider.issue_code(REDIRECT_URI, challenge)
        assert provider.consume_code(code, REDIRECT_URI, verifier) is False

    def test_mismatched_redirect_uri_rejected(self):
        provider = _make_provider()
        verifier, challenge = _pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        assert (
            provider.consume_code(code, "https://other.example.com/cb", verifier)
            is False
        )

    def test_unknown_code_rejected(self):
        provider = _make_provider()
        verifier, _ = _pkce_pair()
        assert provider.consume_code("never-issued", REDIRECT_URI, verifier) is False

    def test_verifier_too_short_rejected(self):
        provider = _make_provider()
        assert provider.consume_code("any-code", REDIRECT_URI, "short") is False

    def test_verifier_with_invalid_characters_rejected(self):
        provider = _make_provider()
        bad_verifier = "a" * 42 + "!"  # right length, disallowed char
        assert provider.consume_code("any-code", REDIRECT_URI, bad_verifier) is False

    def test_issue_code_refuses_new_codes_at_capacity(self):
        # Abuse guard: once the pending-code store is full, issue_code returns
        # None (the /authorize POST maps that to temporarily_unavailable)
        # instead of growing unbounded.
        provider = _make_provider()
        _, challenge = _pkce_pair()
        for _ in range(oauth_legacy.MAX_PENDING_CODES):
            assert provider.issue_code(REDIRECT_URI, challenge) is not None
        assert provider.issue_code(REDIRECT_URI, challenge) is None


# ---------------------------------------------------------------------------
# Client authentication
# ---------------------------------------------------------------------------


class TestAuthenticateClient:
    def test_correct_credentials_accepted(self):
        provider = _make_provider()
        assert provider.authenticate_client(CLIENT_ID, CLIENT_SECRET) is True

    def test_wrong_secret_rejected(self):
        provider = _make_provider()
        assert provider.authenticate_client(CLIENT_ID, "wrong-secret") is False

    def test_wrong_client_id_rejected(self):
        provider = _make_provider()
        assert provider.authenticate_client("wrong-client", CLIENT_SECRET) is False

    def test_missing_client_id_rejected(self):
        provider = _make_provider()
        assert provider.authenticate_client(None, CLIENT_SECRET) is False

    def test_missing_client_secret_rejected(self):
        provider = _make_provider()
        assert provider.authenticate_client(CLIENT_ID, None) is False


class TestExtractClientCreds:
    """``TokenView._extract_client_creds`` -- Basic-auth header vs form body."""

    def test_extracts_from_basic_auth_header(self):
        encoded = base64.b64encode(b"cid:secret").decode()
        request = MagicMock()
        request.headers = {"Authorization": f"Basic {encoded}"}
        cid, secret = oauth_legacy.TokenView._extract_client_creds(request, {})
        assert (cid, secret) == ("cid", "secret")

    def test_extracts_from_form_body_when_no_basic_header(self):
        request = MagicMock()
        request.headers = {}
        cid, secret = oauth_legacy.TokenView._extract_client_creds(
            request, {"client_id": "cid", "client_secret": "secret"}
        )
        assert (cid, secret) == ("cid", "secret")

    def test_malformed_basic_header_returns_none(self):
        request = MagicMock()
        request.headers = {"Authorization": "Basic not-valid-base64!!!"}
        cid, secret = oauth_legacy.TokenView._extract_client_creds(request, {})
        assert (cid, secret) == (None, None)

    def test_basic_header_without_colon_returns_none(self):
        encoded = base64.b64encode(b"no-colon-here").decode()
        request = MagicMock()
        request.headers = {"Authorization": f"Basic {encoded}"}
        cid, secret = oauth_legacy.TokenView._extract_client_creds(request, {})
        assert (cid, secret) == (None, None)

    def test_percent_encoded_basic_creds_are_decoded(self):
        # RFC 6749 §2.3.1: client_secret_basic values are percent-encoded before
        # base64, so a custom credential with reserved characters must decode
        # back (a no-op for the URL-safe generated credentials).
        encoded = base64.b64encode(b"c%40id:p%40ss%2Fword").decode()
        request = MagicMock()
        request.headers = {"Authorization": f"Basic {encoded}"}
        cid, secret = oauth_legacy.TokenView._extract_client_creds(request, {})
        assert (cid, secret) == ("c@id", "p@ss/word")


# ---------------------------------------------------------------------------
# Redirect URI validation
# ---------------------------------------------------------------------------


class TestIsValidRedirectUri:
    def test_https_url_with_host_is_valid(self):
        assert oauth_legacy._is_valid_redirect_uri(REDIRECT_URI) is True

    def test_non_loopback_http_is_rejected(self):
        url = "http://client.example.com/cb"
        assert oauth_legacy._is_valid_redirect_uri(url) is False

    def test_http_loopback_is_valid(self):
        # RFC 8252 §7.3: native/CLI clients receive the code on an http loopback
        # callback. Cover the hostname form and the whole 127.0.0.0/8 + ::1 set.
        assert oauth_legacy._is_valid_redirect_uri("http://localhost:8765/cb") is True
        assert oauth_legacy._is_valid_redirect_uri("http://127.0.0.1:8765/cb") is True
        assert oauth_legacy._is_valid_redirect_uri("http://127.9.9.9/cb") is True
        assert oauth_legacy._is_valid_redirect_uri("http://[::1]:8765/cb") is True

    def test_fragment_is_rejected(self):
        assert oauth_legacy._is_valid_redirect_uri(f"{REDIRECT_URI}#frag") is False

    def test_missing_host_is_rejected(self):
        assert oauth_legacy._is_valid_redirect_uri("https:///cb") is False

    def test_empty_string_is_rejected(self):
        assert oauth_legacy._is_valid_redirect_uri("") is False

    def test_out_of_range_port_is_rejected_not_raised(self):
        # urlparse defers port validation to attribute access, so a crafted
        # ':999999' must be caught in the validator (→ False) rather than escape
        # later as an uncaught ValueError → 500 in _redirect_with.
        assert (
            oauth_legacy._is_valid_redirect_uri("https://x.example.com:999999/cb")
            is False
        )

    def test_non_numeric_port_is_rejected_not_raised(self):
        assert (
            oauth_legacy._is_valid_redirect_uri("https://x.example.com:abc/cb") is False
        )


# ---------------------------------------------------------------------------
# is_active / active_mode_getter
# ---------------------------------------------------------------------------


class TestIsActive:
    def test_true_when_legacy_is_the_live_mode(self):
        provider = _make_provider(active=True)
        assert provider.is_active() is True

    def test_false_when_another_mode_is_live(self):
        provider = _make_provider(active=False)
        assert provider.is_active() is False

    def test_false_when_getter_returns_none(self):
        provider = oauth_legacy.LegacyOAuthProvider(
            CLIENT_ID, CLIENT_SECRET, secrets.token_bytes(32), lambda: None
        )
        assert provider.is_active() is False


# ---------------------------------------------------------------------------
# AuthorizeView (GET consent page, POST approve/deny)
# ---------------------------------------------------------------------------


class TestAuthorizeViewGet:
    async def test_returns_404_when_provider_inactive(self):
        provider = _make_provider(active=False)
        view = oauth_legacy.AuthorizeView(provider)
        response = await view.get(_make_get_request(_authorize_query()))
        assert response.status == 404

    async def test_rejects_wrong_client_id(self):
        provider = _make_provider()
        view = oauth_legacy.AuthorizeView(provider)
        request = _make_get_request(_authorize_query(client_id="someone-else"))
        response = await view.get(request)
        assert response.status == 400

    async def test_rejects_insecure_redirect_uri(self):
        provider = _make_provider()
        view = oauth_legacy.AuthorizeView(provider)
        request = _make_get_request(
            _authorize_query(redirect_uri="http://insecure.example.com/cb")
        )
        response = await view.get(request)
        assert response.status == 400

    async def test_rejects_non_s256_challenge_method(self):
        provider = _make_provider()
        view = oauth_legacy.AuthorizeView(provider)
        request = _make_get_request(_authorize_query(code_challenge_method="plain"))
        response = await view.get(request)
        assert response.status == 400

    async def test_consent_page_shows_redirect_domain_and_escapes_it(self):
        provider = _make_provider()
        view = oauth_legacy.AuthorizeView(provider)
        xss_redirect = 'https://evil.example.com/cb?x="><script>alert(1)</script>'
        request = _make_get_request(_authorize_query(redirect_uri=xss_redirect))
        response = await view.get(request)
        assert response.status == 200
        assert "evil.example.com" in response.text
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;" in response.text


class TestAuthorizeViewPost:
    async def test_approve_issues_a_consumable_code_and_redirects(self):
        provider = _make_provider()
        view = oauth_legacy.AuthorizeView(provider)
        verifier, challenge = _pkce_pair()
        request = MagicMock()
        request.post = AsyncMock(
            return_value={
                "action": "approve",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "state": "xyz",
                "code_challenge": challenge,
            }
        )
        response = await view.post(request)
        assert response.status == 302
        location = response.headers["Location"]
        assert location.startswith(f"{REDIRECT_URI}?")
        assert _extract_query_param(location, "state") == "xyz"
        code = _extract_query_param(location, "code")
        assert provider.consume_code(code, REDIRECT_URI, verifier) is True

    async def test_deny_redirects_with_access_denied_error(self):
        provider = _make_provider()
        view = oauth_legacy.AuthorizeView(provider)
        _, challenge = _pkce_pair()
        request = MagicMock()
        request.post = AsyncMock(
            return_value={
                "action": "deny",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "state": "xyz",
                "code_challenge": challenge,
            }
        )
        response = await view.post(request)
        assert response.status == 302
        location = response.headers["Location"]
        assert _extract_query_param(location, "error") == "access_denied"

    async def test_inactive_provider_returns_404(self):
        provider = _make_provider(active=False)
        view = oauth_legacy.AuthorizeView(provider)
        request = MagicMock()
        request.post = AsyncMock(return_value={})
        response = await view.post(request)
        assert response.status == 404

    async def test_malformed_port_redirect_returns_400_not_500(self):
        # A crafted out-of-range port must be rejected cleanly at validation and
        # never reach yarl in _redirect_with, which would raise → uncaught 500.
        provider = _make_provider()
        view = oauth_legacy.AuthorizeView(provider)
        _, challenge = _pkce_pair()
        request = MagicMock()
        request.post = AsyncMock(
            return_value={
                "action": "approve",
                "client_id": CLIENT_ID,
                "redirect_uri": "https://client.example.com:999999/cb",
                "state": "xyz",
                "code_challenge": challenge,
            }
        )
        response = await view.post(request)
        assert response.status == 400


# ---------------------------------------------------------------------------
# TokenView (POST authorization_code + refresh_token grants)
# ---------------------------------------------------------------------------


class TestTokenViewPost:
    async def test_inactive_provider_returns_not_found(self):
        provider = _make_provider(active=False)
        view = oauth_legacy.TokenView(provider)
        request = MagicMock()
        request.headers = {}
        request.post = AsyncMock(return_value={})
        response = await view.post(request)
        assert response.status == 404
        assert response.json_body == {"error": "not_found"}

    async def test_invalid_client_credentials_returns_401(self):
        provider = _make_provider()
        view = oauth_legacy.TokenView(provider)
        request = MagicMock()
        request.headers = {}
        request.post = AsyncMock(
            return_value={"client_id": CLIENT_ID, "client_secret": "wrong"}
        )
        response = await view.post(request)
        assert response.status == 401
        assert response.json_body["error"] == "invalid_client"

    async def test_authorization_code_grant_returns_valid_tokens(self):
        provider = _make_provider()
        verifier, challenge = _pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        view = oauth_legacy.TokenView(provider)
        request = MagicMock()
        request.headers = {}
        request.post = AsyncMock(
            return_value={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            }
        )
        response = await view.post(request)
        assert response.status == 200
        assert response.json_body["token_type"] == "Bearer"
        access = response.json_body["access_token"]
        refresh = response.json_body["refresh_token"]
        assert provider.validate_access_token(access) is True
        assert provider.validate_refresh_token(refresh) is True
        # RFC 6749 §5.1: the token body carries credentials and must not be cached.
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"

    async def test_authorization_code_grant_rejects_bad_code(self):
        provider = _make_provider()
        view = oauth_legacy.TokenView(provider)
        request = MagicMock()
        request.headers = {}
        request.post = AsyncMock(
            return_value={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": "bogus",
                "redirect_uri": REDIRECT_URI,
                "code_verifier": "x" * 43,
            }
        )
        response = await view.post(request)
        assert response.status == 400
        assert response.json_body["error"] == "invalid_grant"

    async def test_refresh_token_grant_returns_new_tokens(self):
        provider = _make_provider()
        refresh = provider.issue_refresh_token()
        view = oauth_legacy.TokenView(provider)
        request = MagicMock()
        request.headers = {}
        request.post = AsyncMock(
            return_value={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            }
        )
        response = await view.post(request)
        assert response.status == 200
        access = response.json_body["access_token"]
        assert provider.validate_access_token(access) is True
        # The refresh grant carries credentials too — same no-cache headers.
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"

    async def test_unsupported_grant_type_returns_400(self):
        provider = _make_provider()
        view = oauth_legacy.TokenView(provider)
        request = MagicMock()
        request.headers = {}
        request.post = AsyncMock(
            return_value={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "client_credentials",
            }
        )
        response = await view.post(request)
        assert response.status == 400
        assert response.json_body["error"] == "unsupported_grant_type"


# ---------------------------------------------------------------------------
# bind_legacy_views (route ownership + rebind semantics)
# ---------------------------------------------------------------------------


def _make_hass(*, is_running: bool = True) -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}
    hass.is_running = is_running
    return hass


class TestBindLegacyViews:
    def test_first_bind_registers_views_and_stores_provider(self):
        hass = _make_hass(is_running=False)
        provider, _ = oauth_legacy.bind_legacy_views(
            hass, CLIENT_ID, CLIENT_SECRET, secrets.token_bytes(32)
        )
        assert isinstance(provider, oauth_legacy.LegacyOAuthProvider)
        assert hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] == oauth_legacy._DOMAIN
        assert hass.data[oauth_legacy._LEGACY_PROVIDER_KEY] is provider
        assert hass.http.register_view.call_count == 2

    def test_first_bind_at_boot_needs_no_restart(self):
        hass = _make_hass(is_running=False)
        _, restart_needed = oauth_legacy.bind_legacy_views(
            hass, CLIENT_ID, CLIENT_SECRET, secrets.token_bytes(32)
        )
        assert restart_needed is False

    def test_first_bind_mid_session_needs_restart(self):
        hass = _make_hass(is_running=True)
        _, restart_needed = oauth_legacy.bind_legacy_views(
            hass, CLIENT_ID, CLIENT_SECRET, secrets.token_bytes(32)
        )
        assert restart_needed is True

    def test_reload_after_boot_bind_reuses_provider_and_stays_live(self):
        # First bind at boot (is_running False) → live immediately, no restart.
        # A later reload while running reuses the provider and stays live: the
        # pending flag is read from hass.data (False), not recomputed from the
        # now-True hass.is_running.
        hass = _make_hass(is_running=False)
        key = secrets.token_bytes(32)
        provider1, first_restart = oauth_legacy.bind_legacy_views(
            hass, CLIENT_ID, CLIENT_SECRET, key
        )
        assert first_restart is False
        hass.http.register_view.reset_mock()
        hass.is_running = True  # a later reload happens while HA is running

        provider2, restart_needed = oauth_legacy.bind_legacy_views(
            hass, CLIENT_ID, CLIENT_SECRET, key
        )

        assert provider2 is provider1
        assert restart_needed is False
        hass.http.register_view.assert_not_called()

    def test_reload_after_mid_session_bind_stays_restart_pending(self):
        # First bind mid-session (is_running True) → not live until a restart.
        # An unrelated reload before that restart, with unchanged credentials,
        # must KEEP reporting restart-needed (the views are still not live) —
        # otherwise _async_update_legacy_oauth_issue would clear the repair
        # prematurely.
        hass = _make_hass(is_running=True)
        key = secrets.token_bytes(32)
        provider1, first_restart = oauth_legacy.bind_legacy_views(
            hass, CLIENT_ID, CLIENT_SECRET, key
        )
        assert first_restart is True
        hass.http.register_view.reset_mock()

        provider2, restart_needed = oauth_legacy.bind_legacy_views(
            hass, CLIENT_ID, CLIENT_SECRET, key
        )

        assert provider2 is provider1
        assert restart_needed is True
        hass.http.register_view.assert_not_called()

    def test_second_bind_changed_credentials_keeps_old_provider_but_needs_restart(self):
        hass = _make_hass(is_running=True)
        provider1, _ = oauth_legacy.bind_legacy_views(
            hass, CLIENT_ID, CLIENT_SECRET, secrets.token_bytes(32)
        )

        provider2, restart_needed = oauth_legacy.bind_legacy_views(
            hass, CLIENT_ID, "a-new-secret", secrets.token_bytes(32)
        )

        # aiohttp cannot rebind an already-registered view -- the OLD
        # provider stays authoritative until a full HA restart.
        assert provider2 is provider1
        assert restart_needed is True

    def test_raises_route_conflict_when_another_owner_holds_the_routes(self):
        hass = _make_hass()
        hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] = "webhook_proxy"
        with pytest.raises(oauth_legacy.LegacyOAuthRouteConflict):
            oauth_legacy.bind_legacy_views(
                hass, CLIENT_ID, CLIENT_SECRET, secrets.token_bytes(32)
            )

    def test_accepts_hex_string_signing_key(self):
        hass = _make_hass()
        hex_key = secrets.token_bytes(32).hex()
        provider, _ = oauth_legacy.bind_legacy_views(
            hass, CLIENT_ID, CLIENT_SECRET, hex_key
        )
        assert isinstance(provider, oauth_legacy.LegacyOAuthProvider)


# ---------------------------------------------------------------------------
# embedded_entry._ensure_legacy_oauth_secrets (credential provisioning)
# ---------------------------------------------------------------------------


class TestEnsureLegacyOAuthSecrets:
    """Pure dict-mutation helper behind the legacy OAuth credential lifecycle
    -- operates on plain ``entry.data``/``entry.options`` dicts, so no
    hass/entry mocking is needed."""

    def test_first_run_mints_client_id_secret_and_signing_key(self):
        data: dict = {}
        options: dict = {}

        changed = eentry._ensure_legacy_oauth_secrets(data, options)

        assert changed is True
        assert data[DATA_OAUTH_CLIENT_ID].startswith("hamcp-")
        assert data[DATA_OAUTH_CLIENT_SECRET]
        # signing_key is stored as a hex string (entry.data must be JSON
        # serializable) -- must decode cleanly.
        bytes.fromhex(data[DATA_OAUTH_SIGNING_KEY])

    def test_second_run_is_a_noop(self):
        data: dict = {}
        options: dict = {}
        eentry._ensure_legacy_oauth_secrets(data, options)
        snapshot = dict(data)

        changed = eentry._ensure_legacy_oauth_secrets(data, options)

        assert changed is False
        assert data == snapshot

    def test_regenerate_mints_fresh_id_and_secret_but_never_rotates_signing_key(self):
        data = {
            DATA_OAUTH_CLIENT_ID: "hamcp-old",
            DATA_OAUTH_CLIENT_SECRET: "old-secret",
            DATA_OAUTH_SIGNING_KEY: "aa" * 32,
        }
        options = {OPT_OAUTH_REGENERATE: True}

        changed = eentry._ensure_legacy_oauth_secrets(data, options)

        assert changed is True
        assert data[DATA_OAUTH_CLIENT_ID] != "hamcp-old"
        assert data[DATA_OAUTH_CLIENT_SECRET] != "old-secret"
        assert data[DATA_OAUTH_SIGNING_KEY] == "aa" * 32
        assert options[OPT_OAUTH_REGENERATE] is False
        assert options[OPT_OAUTH_CLIENT_ID] == ""
        assert options[OPT_OAUTH_CLIENT_SECRET] == ""

    def test_override_adopts_user_supplied_client_id(self):
        data = {
            DATA_OAUTH_CLIENT_ID: "hamcp-old",
            DATA_OAUTH_CLIENT_SECRET: "s",
            DATA_OAUTH_SIGNING_KEY: "cc" * 32,
        }
        options = {OPT_OAUTH_CLIENT_ID: "my-custom-client-id"}

        changed = eentry._ensure_legacy_oauth_secrets(data, options)

        assert changed is True
        assert data[DATA_OAUTH_CLIENT_ID] == "my-custom-client-id"

    def test_override_adopts_user_supplied_client_secret(self):
        data = {
            DATA_OAUTH_CLIENT_ID: "hamcp-x",
            DATA_OAUTH_CLIENT_SECRET: "old",
            DATA_OAUTH_SIGNING_KEY: "dd" * 32,
        }
        options = {OPT_OAUTH_CLIENT_SECRET: "my-custom-secret"}

        changed = eentry._ensure_legacy_oauth_secrets(data, options)

        assert changed is True
        assert data[DATA_OAUTH_CLIENT_SECRET] == "my-custom-secret"

    def test_matching_override_is_not_reported_as_a_change(self):
        data = {
            DATA_OAUTH_CLIENT_ID: "already-set",
            DATA_OAUTH_CLIENT_SECRET: "s",
            DATA_OAUTH_SIGNING_KEY: "ee" * 32,
        }
        options = {OPT_OAUTH_CLIENT_ID: "already-set"}

        changed = eentry._ensure_legacy_oauth_secrets(data, options)

        assert changed is False
