"""None-mode auto-approve OAuth authorization server (issue #1969).

In ``none`` webhook auth mode the secret webhook URL *is* the credential, so no
bearer is required and the forwarder always returns 200. But claude.ai's
connector onboarding intermittently front-loads OAuth discovery, and because the
component registers no ``/.well-known`` views in none mode, claude.ai falls
through to Home Assistant *core*'s own origin-root
``/.well-known/oauth-authorization-server`` — which advertises
``client_id_metadata_document_supported`` but omits
``token_endpoint_auth_methods_supported: ["none"]`` and has no
``registration_endpoint``. claude.ai then can neither use CIMD nor do dynamic
client registration and shows "Automatic client registration isn't supported…".

This module is the none-mode fix's authorization-server half: a pair of
path-scoped ``OAUTH_BASE`` endpoints that complete OAuth *invisibly* — no login,
no consent — so a connector that does run discovery resolves against our own
corrected documents (served by :mod:`mcp_webhook`) instead of HA core's broken
root doc, and connects with zero HA login:

* ``GET  {OAUTH_BASE}/authorize`` issues a PKCE-bound one-time code and
  immediately 302-redirects back to the client with ``?code=…&state=…`` — no
  page is rendered.
* ``POST {OAUTH_BASE}/token`` exchanges that code (public client, PKCE S256, no
  ``client_secret``) for an opaque access token. The token is *cosmetic* — none
  mode ignores bearers entirely — but is a real random string so a spec-strict
  client is satisfied.

Both views are gated per request off ``hass.data`` (they 404 unless none mode is
the live webhook auth mode), mirroring the discovery views, so a
``none``\\ ↔\\ ``ha_auth`` switch needs no restart. The PKCE code store and the
redirect-URI floor are reused from :mod:`oauth_legacy` rather than copied.

**Open-redirect defence.** ``/authorize`` 302-redirects to a caller-supplied
``redirect_uri`` on the Home Assistant origin, so an unvalidated target would be
an open redirector. On top of :func:`oauth_legacy._is_valid_redirect_uri`'s
scheme/host/port floor, the redirect must EXACTLY match a known MCP callback
(:data:`_AUTOAPPROVE_REDIRECT_ALLOWLIST`). Anything else is a hard 400 (no
redirect). A "same origin as the client_id" rule was deliberately NOT used: the
client_id is fully attacker-controlled, so ``client_id == redirect_uri origin``
still lets an attacker bounce a victim to any site of their choosing (a real
open redirect on a public HA origin). Properly honouring an arbitrary CIMD
client would require fetching the attacker-supplied client_id URL — an SSRF
vector — so the allowlist is both the safe and the simple choice. Add a client's
callback here to support it.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DATA_WEBHOOK, DOMAIN, OAUTH_BASE
from .oauth_legacy import (
    _PKCE_CHALLENGE_RE,
    _TOKEN_RESPONSE_HEADERS,
    ACCESS_TOKEN_TTL,
    PKCECodeStore,
    _is_valid_redirect_uri,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


# cfg (hass.data[DOMAIN][DATA_WEBHOOK]) key holding the live AutoApproveProvider.
# Present ONLY in none mode with the remote endpoint enabled; its presence is
# how :func:`mcp_webhook.active_auth_mode` recognises the none-autoapprove live
# mode (mirrors the "resource_server"/"oauth_provider" presence keys).
CFG_AUTOAPPROVE_PROVIDER = "autoapprove_provider"

# TOP-LEVEL hass.data flag recording that the two auto-approve views are bound
# for this HA session. Not under DOMAIN so it survives async_unload_entry's
# teardown — aiohttp cannot unregister a bound view until HA restarts, so the
# views (and this ownership flag) must outlive the config entry (mirrors
# mcp_webhook._OAUTH_VIEWS_REGISTERED_KEY).
_AUTOAPPROVE_VIEWS_REGISTERED_KEY = "ha_mcp_tools_oauth_autoapprove_views_registered"

# Known MCP OAuth callback URLs always accepted as a redirect target even when
# the client_id is not a same-origin URL — claude.ai's connector onboarding
# posts its authorization code here. Exact-match only (never a prefix test, so
# ``https://claude.ai/api/mcp/auth_callback.evil.example`` cannot slip through).
_AUTOAPPROVE_REDIRECT_ALLOWLIST = frozenset(
    {
        "https://claude.ai/api/mcp/auth_callback",
    }
)


def _json_not_found() -> web.Response:
    """404 JSON body used when none-autoapprove is not the live mode."""
    return web.json_response({"error": "not_found"}, status=404)


def _json_error(
    error: str, status: int, description: str | None = None
) -> web.Response:
    """OAuth-style JSON error (RFC 6749 §5.2 shape) with no-store headers."""
    body: dict[str, str] = {"error": error}
    if description is not None:
        body["error_description"] = description
    return web.json_response(body, status=status, headers=_TOKEN_RESPONSE_HEADERS)


def _is_valid_autoapprove_redirect(redirect_uri: str) -> bool:
    """Open-redirect gate for the auto-approve ``/authorize`` view.

    Exact-match allowlist only, on top of
    :func:`oauth_legacy._is_valid_redirect_uri`'s scheme/host/port floor. The
    ``client_id`` is NOT consulted: it is attacker-controlled, so validating the
    redirect against it (even "same origin") does not constrain the redirect
    target to a trusted host. See the module docstring.
    """
    return (
        _is_valid_redirect_uri(redirect_uri)
        and redirect_uri in _AUTOAPPROVE_REDIRECT_ALLOWLIST
    )


def _redirect_with(redirect_uri: str, **params: str) -> web.Response:
    """302 to ``redirect_uri`` with ``params`` merged into its query string."""
    # yarl ships with aiohttp and handles existing-query merging + encoding
    # correctly — safer than hand-rolling (matches oauth_legacy.AuthorizeView).
    import yarl

    url = yarl.URL(redirect_uri).update_query(params)
    return web.Response(status=302, headers={"Location": str(url)})


class AutoApproveProvider:
    """None-mode auto-approve authorization-server state.

    Holds only the PKCE code store shared with :mod:`oauth_legacy`; it owns no
    signing key and no client credentials (the token it issues is cosmetic).
    Constructed per registration and stored in ``cfg`` — the views resolve it
    from ``hass.data`` per request, so a reload minting a fresh provider is
    transparent (no bound view captures the old one, unlike legacy mode).
    """

    def __init__(self) -> None:
        self._code_store = PKCECodeStore()

    def issue_code(self, redirect_uri: str, code_challenge: str) -> str | None:
        """Issue a one-shot PKCE-bound authorization code (see PKCECodeStore)."""
        return self._code_store.issue_code(redirect_uri, code_challenge)

    def consume_code(self, code: str, redirect_uri: str, code_verifier: str) -> bool:
        """Verify PKCE S256 + one-shot consume a code (see PKCECodeStore)."""
        return self._code_store.consume_code(code, redirect_uri, code_verifier)

    @staticmethod
    def issue_access_token() -> str:
        """Mint an opaque access token.

        None mode ignores bearers (the secret webhook URL is the credential),
        so this token grants nothing — but it is a real random string, so a
        spec-strict client that stores/echoes it is satisfied.
        """
        return secrets.token_urlsafe(32)


def _active_autoapprove_provider(hass: HomeAssistant) -> AutoApproveProvider | None:
    """The live none-mode auto-approve provider, or None when it is not live.

    Read live from ``hass.data`` (not captured at view construction) so the
    bound views serve only while none-autoapprove is the active mode and 404
    otherwise — mirrors ``mcp_webhook._active_webhook_id``'s per-request gating.
    """
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return None
    cfg = domain_data.get(DATA_WEBHOOK)
    if not isinstance(cfg, dict):
        return None
    provider = cfg.get(CFG_AUTOAPPROVE_PROVIDER)
    return provider if isinstance(provider, AutoApproveProvider) else None


class AutoApproveAuthorizeView(HomeAssistantView):
    """None-mode auto-approve ``/authorize`` — issues a code, 302s, no UI.

    Validates ``response_type=code``, PKCE S256, and the redirect_uri
    open-redirect gate, then issues a PKCE-bound one-time code and redirects
    straight back to the client. No login page and no consent screen render, so
    claude.ai's OAuth flow completes invisibly (issue #1969).
    """

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/authorize"
    name = "ha_mcp_tools:oauth:autoapprove-authorize"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Auto-approve the authorization request or reject with a 400/404."""
        provider = _active_autoapprove_provider(self._hass)
        if provider is None:
            return _json_not_found()

        params = request.query
        response_type = params.get("response_type", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")

        if response_type != "code":
            return _json_error("unsupported_response_type", 400)
        if code_challenge_method != "S256":
            return _json_error(
                "invalid_request", 400, "code_challenge_method must be S256"
            )
        if not _PKCE_CHALLENGE_RE.match(code_challenge):
            return _json_error(
                "invalid_request", 400, "invalid code_challenge (43-char base64url)"
            )
        # SECURITY: an unvalidated redirect_uri would be an open redirector on
        # the HA origin. Reject in-place (never redirect) unless it exactly
        # matches a known MCP callback (client_id is attacker-controlled and is
        # deliberately not consulted — see module docstring).
        if not _is_valid_autoapprove_redirect(redirect_uri):
            return _json_error("invalid_request", 400, "invalid redirect_uri")

        code = provider.issue_code(redirect_uri, code_challenge)
        if code is None:
            # Pending-code store at capacity (abuse guard) — surface per
            # RFC 6749 §4.1.2.1 instead of a silent failure.
            return _redirect_with(
                redirect_uri, error="temporarily_unavailable", state=state
            )
        redirect_params = {"code": code}
        if state:
            redirect_params["state"] = state
        return _redirect_with(redirect_uri, **redirect_params)


class AutoApproveTokenView(HomeAssistantView):
    """None-mode auto-approve ``/token`` — PKCE code → opaque access token.

    Public client (no ``client_secret``): the PKCE code_verifier is the only
    proof required. The returned access token is cosmetic (none mode ignores
    bearers), but real and opaque. Only the ``authorization_code`` grant is
    supported — none mode has no refresh cycle.
    """

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/token"
    name = "ha_mcp_tools:oauth:autoapprove-token"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Exchange a PKCE authorization code for an opaque access token."""
        provider = _active_autoapprove_provider(self._hass)
        if provider is None:
            return _json_not_found()

        form: dict[str, Any] = dict(await request.post())
        if form.get("grant_type", "") != "authorization_code":
            return _json_error("unsupported_grant_type", 400)

        code = str(form.get("code", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        code_verifier = str(form.get("code_verifier", ""))
        if not (code and redirect_uri and code_verifier):
            return _json_error("invalid_request", 400)
        if not provider.consume_code(code, redirect_uri, code_verifier):
            return _json_error("invalid_grant", 400)

        return web.json_response(
            {
                "access_token": provider.issue_access_token(),
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL,
            },
            headers=_TOKEN_RESPONSE_HEADERS,
        )


def bind_autoapprove_views(hass: HomeAssistant) -> None:
    """Bind the two auto-approve views at most once per HA session.

    aiohttp cannot unregister a bound view, so a reload / re-enable / mode
    switch must reuse the already-bound views — they resolve the active
    provider from ``hass.data`` per request (see
    :func:`_active_autoapprove_provider`), so they serve only while
    none-autoapprove is live and 404 otherwise. The guard flag lives at a
    top-level ``hass.data`` key that survives config-entry teardown (mirrors
    :func:`mcp_webhook._register_metadata_views`).
    """
    if hass.data.get(_AUTOAPPROVE_VIEWS_REGISTERED_KEY):
        return
    hass.http.register_view(AutoApproveAuthorizeView(hass))
    hass.http.register_view(AutoApproveTokenView(hass))
    hass.data[_AUTOAPPROVE_VIEWS_REGISTERED_KEY] = True
