"""Unified scoped OAuth authorization endpoints (issue #1969).

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

This module owns the pair of path-scoped ``OAUTH_BASE`` endpoints. In none mode
they complete OAuth *invisibly* — no login, no consent — so a connector that
does run discovery resolves against our own corrected documents (served by
:mod:`mcp_webhook`) instead of HA core's broken root doc, and connects with zero
HA login:

* ``GET  {OAUTH_BASE}/authorize`` issues a PKCE-bound one-time code and
  immediately 302-redirects back to the client with ``?code=…&state=…`` — no
  page is rendered.
* ``POST {OAUTH_BASE}/token`` exchanges that code (public client, PKCE S256, no
  ``client_secret``) for an opaque access token. The token is *cosmetic* — none
  mode ignores bearers entirely — but is a real random string so a spec-strict
  client is satisfied.

Both views dispatch per request from ``hass.data`` to the live legacy, ha_auth,
or none-mode provider (and 404 when no remote OAuth mode is live), mirroring the
discovery views so mode switches need no restart. The none-mode PKCE code store
and redirect-URI floor are reused from :mod:`oauth_legacy` rather than copied.

**Open-redirect policy.** In none mode THE SECRET WEBHOOK URL IS THE MAIN AND
ONLY FORM OF SECURITY. The OAuth surface exists purely for client compatibility;
its tokens grant nothing. ``/authorize`` therefore serves every provider and
302-redirects to any spec-valid ``redirect_uri``; malformed targets still hard
400 under :func:`oauth_legacy._is_valid_redirect_uri`. This makes the Home
Assistant origin usable as a crafted-link redirector, an accepted risk in the
secret-URL trust model. An exact-match callback allowlist shipped in PR #1976
in July 2026; it was retired on 2026-08-14 by maintainer decision to serve every
provider.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DATA_WEBHOOK, DOMAIN, OAUTH_BASE
from .oauth_legacy import (
    _PKCE_CHALLENGE_RE,
    _TOKEN_RESPONSE_HEADERS,
    ACCESS_TOKEN_TTL,
    PKCECodeStore,
    _is_valid_redirect_uri,
    _issuer_for,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


# cfg (hass.data[DOMAIN][DATA_WEBHOOK]) key holding the live AutoApproveProvider.
# Present ONLY in none mode with the remote endpoint enabled; its presence is
# how :func:`mcp_webhook.active_auth_mode` recognises the none-autoapprove live
# mode (mirrors the "resource_server"/"oauth_provider" presence keys).
CFG_AUTOAPPROVE_PROVIDER = "autoapprove_provider"

# Dedicated aiohttp session for anonymous CIMD fetches in ha_auth mode.
# Keeping it separate from the relay session prevents slow public metadata
# endpoints from consuming the pool used by authenticated MCP forwarding.
CFG_CIMD_SESSION = "cimd_session"

# TOP-LEVEL hass.data flag recording that the two unified scoped views are bound
# for this HA session. Not under DOMAIN so it survives async_unload_entry's
# teardown — aiohttp cannot unregister a bound view until HA restarts, so the
# views (and this ownership flag) must outlive the config entry (mirrors
# mcp_webhook._OAUTH_VIEWS_REGISTERED_KEY).
_AUTOAPPROVE_VIEWS_REGISTERED_KEY = "ha_mcp_tools_oauth_autoapprove_views_registered"


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


def _webhook_cfg(hass: HomeAssistant) -> dict[str, Any] | None:
    """The live webhook cfg dict, or None when the entry is not set up."""
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return None
    cfg = domain_data.get(DATA_WEBHOOK)
    return cfg if isinstance(cfg, dict) else None


def _active_autoapprove_provider(hass: HomeAssistant) -> AutoApproveProvider | None:
    """The live none-mode auto-approve provider, or None when it is not live.

    Read live from ``hass.data`` (not captured at view construction) so the
    bound views serve only while none-autoapprove is the active mode and 404
    otherwise — mirrors ``mcp_webhook._active_webhook_id``'s per-request gating.
    """
    cfg = _webhook_cfg(hass)
    if cfg is None:
        return None
    provider = cfg.get(CFG_AUTOAPPROVE_PROVIDER)
    return provider if isinstance(provider, AutoApproveProvider) else None


def _validate_autoapprove_authorize(params: Any) -> web.Response | None:
    """Validate the none-mode /authorize query; a 400 Response, or None if OK.

    Maintainer decision 2026-08-14 (supersedes the #1969-era exact-match
    allowlist): none mode's ONLY credential is the secret webhook URL, so the
    auto-approve flow completes invisibly for ANY spec-valid redirect — the
    token it yields is cosmetic and grants nothing. The HA origin being usable
    as a crafted-link redirector via this anonymous endpoint is an accepted
    trade within that trust model. The spec floor (_is_valid_redirect_uri:
    https or RFC 8252 loopback, valid port, no fragment) still hard-400s
    malformed targets without redirecting.
    """
    if params.get("response_type", "") != "code":
        return _json_error("unsupported_response_type", 400)
    if params.get("code_challenge_method", "") != "S256":
        return _json_error("invalid_request", 400, "code_challenge_method must be S256")
    if not _PKCE_CHALLENGE_RE.fullmatch(params.get("code_challenge", "")):
        return _json_error(
            "invalid_request", 400, "invalid code_challenge (43-char base64url)"
        )
    if not _is_valid_redirect_uri(params.get("redirect_uri", "")):
        return _json_error("invalid_request", 400, "invalid redirect_uri")
    return None


class AutoApproveAuthorizeView(HomeAssistantView):
    """Unified scoped ``/authorize`` dispatcher for every remote auth mode.

    Legacy mode serves the shared consent flow, ha_auth redirects into core,
    and none mode validates PKCE plus the redirect gate before issuing a code
    and redirecting invisibly (issue #1969).

    ACCEPTED RISK (issue #1978): this endpoint is anonymous by design — none
    mode requires zero HA login — so it consults neither the webhook id nor a
    client identity. Anyone who knows the HA origin can therefore fill the
    shared pending-code store (``MAX_PENDING_CODES``) with S256 challenges bound
    to the public claude.ai callback, at which point a *brand-new* connector's
    handshake gets ``temporarily_unavailable`` until those codes expire
    (``AUTH_CODE_TTL``, 5 min). Accepted because it is self-healing, exposes no
    data, and grants no access: completing the flow needs the PKCE verifier the
    attacker never has, and the issued token is cosmetic (none mode ignores
    bearers). The webhook URL itself keeps forwarding throughout — only the rare
    OAuth-discovery fallback for a *first* connect is briefly delayed.
    """

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/authorize"
    name = "ha_mcp_tools:oauth:autoapprove-authorize"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Dispatch the authorization request to the active mode."""
        cfg = _webhook_cfg(self._hass)
        if cfg is None:
            return _json_not_found()
        legacy_provider = cfg.get("oauth_provider")
        if legacy_provider is not None:
            from .oauth_legacy import handle_legacy_authorize_get

            return await handle_legacy_authorize_get(legacy_provider, request)
        if cfg.get("resource_server") is not None:
            return await self._ha_auth_authorize(cfg, request)
        provider = cfg.get(CFG_AUTOAPPROVE_PROVIDER)
        if not isinstance(provider, AutoApproveProvider):
            return _json_not_found()

        params = request.query
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")

        err = _validate_autoapprove_authorize(params)
        if err is not None:
            return err

        # RFC 9207: every authorization response — success or error — names the
        # issuer that produced it, so a client registered with several
        # authorization servers cannot be fed a response minted by another one.
        iss = _issuer_for(request)

        code = provider.issue_code(redirect_uri, code_challenge)
        if code is None:
            # Pending-code store at capacity (abuse guard) — surface per
            # RFC 6749 §4.1.2.1 instead of a silent failure.
            return _redirect_with(
                redirect_uri, error="temporarily_unavailable", state=state, iss=iss
            )
        redirect_params = {"code": code, "iss": iss}
        if state:
            redirect_params["state"] = state
        return _redirect_with(redirect_uri, **redirect_params)

    async def _ha_auth_authorize(
        self, cfg: dict[str, Any], request: web.Request
    ) -> web.Response:
        """302 the browser into core's /auth/authorize (ha_auth indirection).

        The user logs in on core's own page exactly as before; only the URL the
        client learned is ours. client_id is upgraded via CIMD/DCR validation
        when possible, else passed through untouched (core stays the authority).
        """
        from multidict import MultiDict

        from .oauth_dcr import CFG_DCR_SIGNING_KEY
        from .oauth_ha_auth import resolve_forward_client_id

        # MultiDict copy: repeated OAuth params (e.g. RFC 8707 ``resource``)
        # must survive the forward — a plain dict() collapses them.
        params = MultiDict(request.query)
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        forward_id = await resolve_forward_client_id(
            cfg.get(CFG_CIMD_SESSION),
            cfg.get(CFG_DCR_SIGNING_KEY),
            client_id,
            redirect_uri,
        )
        if forward_id != client_id:
            params.popall("client_id", None)
            params["client_id"] = forward_id
        import yarl

        from .mcp_webhook import _build_base_url

        # Request-host-derived base is correct HERE (unlike the token forward):
        # this is a BROWSER redirect back through the origin the user is
        # already on, not a server-side request.
        target = yarl.URL(f"{_build_base_url(request)}/auth/authorize").with_query(
            params
        )
        return web.Response(status=302, headers={"Location": str(target)})

    async def post(self, request: web.Request) -> web.Response:
        """Handle a legacy-mode consent submission on the scoped route."""
        cfg = _webhook_cfg(self._hass)
        if cfg is None:
            return _json_not_found()
        legacy_provider = cfg.get("oauth_provider")
        if legacy_provider is None:
            return _json_not_found()
        from .oauth_legacy import handle_legacy_authorize_post

        return await handle_legacy_authorize_post(legacy_provider, request)


class AutoApproveTokenView(HomeAssistantView):
    """Unified scoped ``/token`` dispatcher for every remote auth mode.

    Legacy mode uses the shared credentialed token handlers, ha_auth forwards
    into core, and none mode exchanges a PKCE code as a public client for a
    cosmetic opaque token (none mode ignores bearers and has no refresh cycle).
    """

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/token"
    name = "ha_mcp_tools:oauth:autoapprove-token"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Dispatch the token request to the active mode."""
        cfg = _webhook_cfg(self._hass)
        if cfg is None:
            return _json_not_found()
        legacy_provider = cfg.get("oauth_provider")
        if legacy_provider is not None:
            from .oauth_legacy import handle_legacy_token_post

            return await handle_legacy_token_post(legacy_provider, request)
        if cfg.get("resource_server") is not None:
            return await self._ha_auth_token(cfg, request)
        provider = cfg.get(CFG_AUTOAPPROVE_PROVIDER)
        if not isinstance(provider, AutoApproveProvider):
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

    async def _ha_auth_token(
        self, cfg: dict[str, Any], request: web.Request
    ) -> web.Response:
        """Route the token exchange to core: 307 by default, proxy if translating.

        Untranslated identities are 307-redirected to core's own /auth/token so
        core sees the client's real address (its wrong-login notifications, ban
        counters, trusted_networks refresh validation, and last_used_ip all key
        on request.remote — #2213 review). Only translated identities (the body
        must be rewritten) are forwarded server-side; the translation matches
        the authorize leg, and the redirect_uri-less refresh grant re-derives
        web-origin translations from the registered list (ephemeral loopback
        clients re-authorize).
        """
        from multidict import MultiDict

        from .oauth_dcr import CFG_DCR_SIGNING_KEY
        from .oauth_ha_auth import (
            core_token_base_url,
            resolve_forward_client_id,
            translated_client_id_for_refresh,
        )

        form = MultiDict(await request.post())
        client_id = str(form.get("client_id", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        forward_id = client_id
        if client_id:
            if redirect_uri:
                forward_id = await resolve_forward_client_id(
                    cfg.get(CFG_CIMD_SESSION),
                    cfg.get(CFG_DCR_SIGNING_KEY),
                    client_id,
                    redirect_uri,
                )
            else:
                # refresh_token grant: no redirect_uri on the wire — re-derive
                # the translation from the registered list alone.
                translated = await translated_client_id_for_refresh(
                    cfg.get(CFG_CIMD_SESSION),
                    cfg.get(CFG_DCR_SIGNING_KEY),
                    client_id,
                )
                if translated is not None:
                    forward_id = translated
                else:
                    from .oauth_dcr import client_redirect_uris

                    dcr_key = cfg.get(CFG_DCR_SIGNING_KEY)
                    if (
                        dcr_key is not None
                        and client_redirect_uris(dcr_key, client_id) is not None
                    ):
                        # A verifiable DCR identity with NO stable web origin
                        # (loopback-only registration): the refresh is doomed —
                        # the token was bound to the ephemeral loopback origin
                        # we cannot re-derive. Answer here instead of 307ing a
                        # guaranteed failure into core, whose @log_invalid_auth
                        # would notify and count it as a failed login (#2213
                        # review round 2). The client re-authorizes.
                        return _json_error(
                            "invalid_grant",
                            400,
                            "re-authorize: loopback-registered clients cannot "
                            "refresh (the token is bound to an ephemeral "
                            "loopback origin)",
                        )
        if forward_id == client_id:
            # No body rewrite needed, so don't proxy: 307 the client into
            # core's own /auth/token on the same public origin it just used.
            # Core then observes the CLIENT's address, which it uses for more
            # than logging (#2213 review by Patch76): process_wrong_login
            # notifications and login_attempts_threshold ban counters on
            # failed exchanges, trusted_networks refresh-token validation,
            # and the profile's last_used_ip. 307 rather than 308: both
            # preserve method+body, but a 308 is cacheable by default and
            # could teach the client a core URL that outlives a later
            # auth-mode switch — the exact stickiness this PR removes.
            # RELATIVE Location (#2213 review round 2): an absolute target
            # would be derived from unvalidated forwarded headers, turning a
            # header a peer controls into the URL the client POSTS the grant
            # to. A relative reference resolves against the origin the client
            # actually used and keeps header derivation out of the credential
            # path entirely (RFC 9110 permits relative Location).
            return web.Response(
                status=307,
                headers={
                    "Location": "/auth/token",
                    "Cache-Control": "no-store",
                },
            )
        # Translated identity (cross-origin CIMD / DCR blob): the body must be
        # rewritten, so the exchange is forwarded server-side. Core records
        # this server's address for these rare clients — accepted residual,
        # noted in the PR.
        form.popall("client_id", None)
        form["client_id"] = forward_id
        session = cfg.get("session")
        if session is None:
            return _json_error("temporarily_unavailable", 503)
        base = core_token_base_url(self._hass)
        try:
            async with session.post(
                f"{base}/auth/token",
                data=form,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                body = await resp.read()
                return web.Response(
                    status=resp.status,
                    body=body,
                    content_type=resp.content_type or "application/json",
                    headers=_TOKEN_RESPONSE_HEADERS,
                )
        except (TimeoutError, aiohttp.ClientError):
            return _json_error("temporarily_unavailable", 503)


def bind_autoapprove_views(hass: HomeAssistant) -> None:
    """Bind the two unified OAuth views at most once per HA session.

    aiohttp cannot unregister a bound view, so a reload / re-enable / mode
    switch must reuse the already-bound views — they resolve the active
    mode/provider from ``hass.data`` per request (see :func:`_webhook_cfg`), so
    the same paths dispatch to legacy, ha_auth, or none-autoapprove without
    rebinding. The guard flag lives at a top-level ``hass.data`` key that
    survives config-entry teardown (mirrors
    :func:`mcp_webhook._register_metadata_views`).
    """
    if hass.data.get(_AUTOAPPROVE_VIEWS_REGISTERED_KEY):
        return
    # Set the flag only AFTER both views register (issue #1978): see
    # mcp_webhook._register_metadata_views. Marking the bundle bound before
    # /token registers would let a later setup assign its mode provider and
    # advertise OAuth with an unbound /token — a 404 on the token exchange. The
    # flag must mean the full bundle succeeded; a partial bind leaves it unset.
    hass.http.register_view(AutoApproveAuthorizeView(hass))
    hass.http.register_view(AutoApproveTokenView(hass))
    hass.data[_AUTOAPPROVE_VIEWS_REGISTERED_KEY] = True
