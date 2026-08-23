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

import logging
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

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
    read_form,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from multidict import MultiDict

_LOGGER = logging.getLogger(__name__)


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

        # Keep the browser hop relative, matching the token leg. Browsers cannot
        # be made to send X-Forwarded-Host, so this is consistency rather than a
        # vulnerability fix.
        target = yarl.URL("/auth/authorize").with_query(params)
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


def _revoke_rewrite(dcr_key: bytes | None, form: MultiDict) -> bool:
    """Swap an envelope back for core's own token on a revocation (#2248).

    Core's ``/auth/token`` also takes ``action=revoke&token=<refresh_token>``
    and answers 200 even for a token it has never seen, so a client revoking
    the envelope we handed it would get a success it did not receive. RFC 7009
    authorizes the BEARER of the token rather than a client identity, so the
    presenter binding is deliberately skipped here. Returns whether the
    exchange must now be proxied (core has to see its own token).
    """
    from .oauth_ha_auth import unwrap_refresh_token

    if dcr_key is None:
        return False
    unwrapped = unwrap_refresh_token(dcr_key, str(form.get("token", "")), None)
    if not isinstance(unwrapped, tuple):
        return False
    core_refresh_token, _forward_id = unwrapped
    form.popall("token", None)
    form["token"] = core_refresh_token
    return True


def _envelope_identity(
    dcr_key: bytes | None, form: MultiDict, client_id: str
) -> tuple[str, bool] | web.Response | None:
    """Read a refresh grant's identity out of its signed envelope (#2248).

    None when the presented token carries no envelope (``ABSENT``) or no DCR
    key is configured — the caller then falls through to the pre-envelope
    derivation. A verified envelope rewrites ``form``'s ``refresh_token`` to
    core's own token and forces the proxy leg, because a 307 would hand core a
    value it cannot redeem. An ``INVALID`` one is answered locally for the same
    reason: core cannot redeem it either, and relaying it would only feed its
    failed-login accounting.
    """
    from .oauth_ha_auth import EnvelopeState, unwrap_refresh_token

    if dcr_key is None:
        return None
    envelope = unwrap_refresh_token(
        dcr_key, str(form.get("refresh_token", "")), client_id
    )
    if isinstance(envelope, tuple):
        core_refresh_token, forward_id = envelope
        form.popall("refresh_token", None)
        form["refresh_token"] = core_refresh_token
        return forward_id, True
    if envelope is EnvelopeState.INVALID:
        _LOGGER.warning(
            "ha_auth refresh: signed envelope failed verification for "
            "client_id %s — the DCR signing key may have changed, or the "
            "token was replayed under another client_id or tampered with",
            client_id,
        )
        return _json_error(
            "invalid_grant",
            400,
            "re-authorize: this refresh token could not be verified against "
            "this server's current signing key",
        )
    return None


async def _pre_envelope_refresh_identity(
    cfg: dict[str, Any], dcr_key: bytes | None, client_id: str
) -> tuple[str, bool] | web.Response:
    """Re-derive the identity of a redirect-less PRE-envelope refresh (#2248).

    Only ``EnvelopeState.ABSENT`` tokens reach here — minted before the
    envelope shipped, or presented while no DCR key is configured.
    """
    from .oauth_ha_auth import RefreshDisposition, translated_client_id_for_refresh

    translated = await translated_client_id_for_refresh(
        cfg.get(CFG_CIMD_SESSION),
        dcr_key,
        client_id,
    )
    if translated is RefreshDisposition.UNREPRODUCIBLE:
        # The token was bound to an origin nothing here can name, so core
        # would reject it; answering locally keeps a guaranteed failure out
        # of core's failed-login accounting. Registration still advertises
        # refresh_token for these clients — one re-authorize mints an
        # envelope-carrying token that refreshes from then on.
        return _json_error(
            "invalid_grant",
            400,
            "re-authorize once: this refresh token predates the signed "
            "identity envelope, and its client's registration names no single "
            "reproducible web origin to re-derive it from",
        )
    if translated is RefreshDisposition.PASSTHROUGH:
        return client_id, False
    return translated, False


async def _code_leg_forces_proxy(
    cfg: dict[str, Any], dcr_key: bytes | None, client_id: str
) -> bool:
    """Whether an UNTRANSLATED code exchange must still be proxied (#2248).

    The authorize leg's same-origin fast path returns the client_id without
    fetching its CIMD document, which is right for that leg — but it means a
    hybrid identity (redirects across two web origins, one of them same-origin
    with the client_id) reaches core untranslated and gets core's RAW refresh
    token back. Its redirect-less refresh then lands in
    ``translated_client_id_for_refresh``, which DOES fetch, sees the split
    origins and answers UNREPRODUCIBLE — a local invalid_grant forever, under
    a message promising that re-authorizing fixes it.

    So the code leg pays the one CIMD fetch the fast path skipped: an
    unreproducible identity is proxied instead of 307'd, its ``refresh_token``
    comes back wrapped with forward id == client_id, and the refresh leg
    proxies that same pair to core, which accepts it. A failed fetch or a
    reproducible identity degrades to PASSTHROUGH and the 307, exactly as
    before. Without a DCR key there is nothing to sign the envelope with, so
    proxying would buy nothing.
    """
    from .oauth_ha_auth import RefreshDisposition, translated_client_id_for_refresh

    if dcr_key is None:
        return False
    try:
        if urlparse(client_id).scheme != "https":
            return False
    except ValueError:
        # Malformed client_id: core stays the authority (the authorize leg
        # makes the same call on an anonymous view).
        return False
    disposition = await translated_client_id_for_refresh(
        cfg.get(CFG_CIMD_SESSION),
        dcr_key,
        client_id,
    )
    return disposition is RefreshDisposition.UNREPRODUCIBLE


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

        raw_form = await read_form(request)
        if raw_form is None:
            return _json_error("invalid_request", 400)
        form: dict[str, Any] = dict(raw_form)
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
        the authorize leg, and every forwarded 200 that is a grant response
        comes back with its ``refresh_token`` wrapped in the signed envelope
        that makes the next refresh resolvable (#2248).
        """
        from multidict import MultiDict

        from .oauth_dcr import CFG_DCR_SIGNING_KEY

        raw_form = await read_form(request)
        if raw_form is None:
            return _json_error("invalid_request", 400)
        # str()-coerce every value: request.post() also yields bytes and
        # FileField on a multipart body, and those reach the outgoing
        # session.post(data=form) serializer, which raises TypeError — an
        # anonymous 500 (#2219 codex review). Repeated keys are preserved.
        form: MultiDict = MultiDict(
            (key, str(value)) for key, value in raw_form.items()
        )
        dcr_key = cfg.get(CFG_DCR_SIGNING_KEY)
        client_id = str(form.get("client_id", ""))
        resolved = await self._ha_auth_forward_identity(cfg, form, client_id, dcr_key)
        if isinstance(resolved, web.Response):
            return resolved
        forward_id, proxy_required = resolved
        if forward_id == client_id and not proxy_required:
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
        # The body must be rewritten, so the exchange is forwarded server-side:
        # a translated identity (cross-origin CIMD / DCR blob), an unwrapped
        # envelope, a revocation whose token was one, or a code leg whose
        # untranslated identity could not otherwise refresh (#2248). Core
        # records this server's address for these rare clients — accepted
        # residual, noted in the PR.
        form.popall("client_id", None)
        form["client_id"] = forward_id
        return await self._proxy_token_to_core(
            cfg, form, forward_id, client_id, dcr_key
        )

    async def _ha_auth_forward_identity(
        self,
        cfg: dict[str, Any],
        form: MultiDict,
        client_id: str,
        dcr_key: bytes | None,
    ) -> tuple[str, bool] | web.Response:
        """Resolve the client_id to present to core, plus whether proxying is forced.

        Returns a ready ``web.Response`` instead when the grant must be
        answered locally. Mutates ``form`` wherever the wire value is one of
        ours and core must receive its own: the envelope in a refresh grant,
        and the envelope in a revocation's ``token``.

        Envelope first (#2248). A refresh token we wrapped names the client_id
        core bound it to, so the identity is READ rather than re-derived, and
        the exchange must be proxied — a 307 would hand core an envelope it
        cannot redeem. Anything else keeps the pre-#2248 behavior: a refresh
        carrying a redirect_uri translates from that redirect exactly like the
        authorize leg, a redirect-less refresh re-derives from the registered
        list, and a verified registration with no reproducible origin is
        answered locally rather than 307'd into a guaranteed core failure.
        """
        from .oauth_ha_auth import resolve_forward_client_id

        grant_type = str(form.get("grant_type", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        if form.get("action") == "revoke":
            # RFC 7009 revocation carries no grant_type; the only rewrite it
            # needs is the envelope swap, and everything else 307s as before.
            return client_id, _revoke_rewrite(dcr_key, form)
        if grant_type == "refresh_token":
            envelope = _envelope_identity(dcr_key, form, client_id)
            if envelope is not None:
                return envelope
        if not client_id:
            return client_id, False
        if grant_type == "refresh_token" and not redirect_uri:
            return await _pre_envelope_refresh_identity(cfg, dcr_key, client_id)
        # Authorization-code exchanges — and refreshes that DO carry a
        # redirect_uri — use the presented redirect, exactly like the authorize
        # leg. With no redirect_uri, validation leaves the client_id untouched
        # for core to reject authoritatively.
        forward_id = await resolve_forward_client_id(
            cfg.get(CFG_CIMD_SESSION),
            dcr_key,
            client_id,
            redirect_uri,
        )
        if grant_type == "authorization_code" and forward_id == client_id:
            return client_id, await _code_leg_forces_proxy(cfg, dcr_key, client_id)
        return forward_id, False

    async def _proxy_token_to_core(
        self,
        cfg: dict[str, Any],
        form: MultiDict,
        forward_id: str,
        client_id: str,
        dcr_key: bytes | None,
    ) -> web.Response:
        """POST the rewritten token form to core and relay its response.

        A 200 has its ``refresh_token`` wrapped before it leaves (#2248) so the
        client's next refresh carries the identity core bound this grant to.
        Every other status — and a body with nothing to wrap — is relayed
        byte-for-byte.
        """
        from .oauth_ha_auth import core_token_base_url, rewrite_token_response_body

        session = cfg.get("session")
        if session is None:
            _LOGGER.warning(
                "ha_auth token forward: the entry has no relay session "
                "(half-initialised setup); answering 503"
            )
            return _json_error(
                "temporarily_unavailable", 503, "token forwarding is not available"
            )
        base = core_token_base_url(self._hass)
        try:
            async with session.post(
                f"{base}/auth/token",
                data=form,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                body = await resp.read()
                # Revocation answers 200 with an EMPTY body, so there is
                # nothing to rewrite and a warning would be pure noise.
                if (
                    resp.status == 200
                    and dcr_key is not None
                    and form.get("action") != "revoke"
                ):
                    body = rewrite_token_response_body(
                        dcr_key, body, forward_id, client_id
                    )
                return web.Response(
                    status=resp.status,
                    body=body,
                    content_type=resp.content_type or "application/json",
                    headers=_TOKEN_RESPONSE_HEADERS,
                )
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning(
                "ha_auth token forward to %s failed: %s",
                base,
                type(err).__name__,
            )
            return _json_error(
                "temporarily_unavailable", 503, "core did not answer the token request"
            )


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
