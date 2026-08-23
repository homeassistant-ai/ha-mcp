"""Unified scoped OAuth endpoints for every proxy authentication mode.

When OAuth is OFF the secret webhook URL *is* the credential, so no bearer is
required and the proxy forwards everything with a 200. But claude.ai's connector
onboarding intermittently front-loads OAuth discovery, and because the
integration registers no ``/.well-known`` views when OAuth is off, claude.ai
falls through to Home Assistant *core*'s own origin-root
``/.well-known/oauth-authorization-server`` — which advertises
``client_id_metadata_document_supported`` but omits
``token_endpoint_auth_methods_supported: ["none"]`` and has no
``registration_endpoint``. claude.ai then can neither use CIMD nor do dynamic
client registration and shows "Automatic client registration isn't supported…".

This module owns three path-scoped ``OAUTH_BASE`` routes; authorize and token
dispatch per request to legacy, ha_auth, or none-autoapprove. Cached clients
therefore keep calling proxy-owned endpoints across mode switches. In none mode
the exchange remains invisible and its access token remains cosmetic:

* ``GET  {OAUTH_BASE}/authorize`` issues a PKCE-bound one-time code and
  immediately 302-redirects back to the client with ``?code=…&state=…`` — no
  page is rendered.
* ``POST {OAUTH_BASE}/token`` exchanges that code (public client, PKCE S256, no
  ``client_secret``) for an opaque access token. The token is *cosmetic* — none
  mode ignores bearers entirely — but is a real random string so a spec-strict
  client is satisfied.
* ``POST {OAUTH_BASE}/revoke`` fronts RFC 7009 revocation for ha_auth mode, the
  only mode that hands the client a signed refresh envelope core cannot redeem
  (#2248); it 404s in the other two.

In none mode the secret webhook URL remains the only credential and the OAuth
token grants nothing. By maintainer decision, authorize therefore accepts any
spec-valid HTTPS or RFC 8252 loopback callback; malformed targets still fail in
place. The resulting crafted-link redirect behavior is accepted within that
secret-URL trust model.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .oauth import (
    _PKCE_CHALLENGE_RE,
    _TOKEN_RESPONSE_HEADERS,
    ACCESS_TOKEN_TTL,
    AUTOAPPROVE_PROVIDER_KEY,
    DOMAIN,
    MODE_HA_AUTH,
    MODE_LEGACY,
    MODE_NONE_AUTOAPPROVE,
    OAUTH_BASE,
    PKCECodeStore,
    _addon_alive,
    _build_base_url,
    _is_valid_redirect_uri,
    handle_legacy_authorize_get,
    handle_legacy_authorize_post,
    handle_legacy_token_post,
    read_form,
)

if TYPE_CHECKING:
    from multidict import MultiDict

_LOGGER = logging.getLogger(__name__)

# Dedicated session for anonymous CIMD lookups. Keeping it separate prevents a
# slow public metadata host from consuming the authenticated relay pool.
CFG_CIMD_SESSION = "cimd_session"

# TOP-LEVEL hass.data flag recording that the three auto-approve views are bound
# for this HA session. Not under DOMAIN so it survives async_unload_entry's
# hass.data.pop(DOMAIN) — aiohttp cannot unregister a bound view until HA
# restarts, so the views (and this flag) must outlive the config entry. Suffixed
# with DOMAIN so each flavor gets its own flag (mirrors
# oauth._METADATA_VIEWS_REGISTERED_KEY).
_AUTOAPPROVE_VIEWS_REGISTERED_KEY = (
    f"webhook_proxy_oauth_autoapprove_views_registered_{DOMAIN}"
)


def _issuer(base: str) -> str:
    """Issuer identifier none-mode auto-approve advertises under ``base``.

    Single source for the document's ``issuer`` below, the provider's
    ``authorization_server_url``, and the RFC 9207 ``iss`` authorization-response
    parameter — RFC 9207 §2 requires the redirect's ``iss`` to equal the
    advertised issuer exactly.
    """
    return f"{base}{OAUTH_BASE}"


def authorization_server_document(base: str) -> dict:
    """RFC 8414 authorization-server metadata for none-mode auto-approve.

    Points MCP clients at OUR OWN ``OAUTH_BASE`` ``/authorize`` + ``/token`` (the
    invisible auto-approve endpoints below), NOT HA core's ``/auth/*``. Serving
    this — with ``token_endpoint_auth_methods_supported: ["none"]`` (public PKCE
    client) and ``client_id_metadata_document_supported`` — is the none-mode fix:
    claude.ai's intermittent discovery resolves against this corrected document
    instead of HA core's origin-root ``/.well-known/oauth-authorization-server``,
    which omits the ``"none"`` method and has no ``registration_endpoint`` (issue
    #1969). No refresh grant: the token is cosmetic (none mode ignores bearers),
    so only ``authorization_code`` is advertised.
    """
    return {
        "issuer": _issuer(base),
        # RFC 9207 §3: authorization responses carry ``iss`` (the auto-approve
        # redirects); omission reads as "not supported" to discovery clients.
        "authorization_response_iss_parameter_supported": True,
        "authorization_endpoint": f"{base}{OAUTH_BASE}/authorize",
        "token_endpoint": f"{base}{OAUTH_BASE}/token",
        "registration_endpoint": f"{base}{OAUTH_BASE}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
    }


# RFC 7009 §2.2.1: on a 503 from a revocation endpoint "the client must assume
# the token still exists and may retry after a reasonable delay", and the server
# MAY name that delay. Seconds, as a short transient — a client left to guess may
# simply give up, and an abandoned revocation leaves the session live (#2248).
_REVOKE_RETRY_AFTER = "5"


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


def _validate_autoapprove_authorize(params: Any) -> web.Response | None:
    """Validate the none-mode authorization request without a client allowlist."""
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


def _redirect_with(redirect_uri: str, **params: str) -> web.Response:
    """302 to ``redirect_uri`` with ``params`` merged into its query string."""
    # yarl ships with aiohttp and handles existing-query merging + encoding
    # correctly — safer than hand-rolling (matches oauth.AuthorizeView).
    import yarl

    url = yarl.URL(redirect_uri).update_query(params)
    return web.Response(status=302, headers={"Location": str(url)})


class AutoApproveProvider:
    """None-mode auto-approve authorization-server state + metadata provider.

    Implements the same metadata surface the discovery views need
    (``webhook_id`` / ``resource_url`` / ``authorization_server_url`` /
    ``base_url_for`` — satisfying ``oauth.MetadataProvider``) so the shared views
    can build documents for none mode too, PLUS the PKCE code store shared with
    :mod:`oauth`. It owns NO signing key and NO client credentials (the token it
    issues is cosmetic). Base URLs are host-derived (``public_base_url=None``),
    like ha_auth, so the same install works via any external URL. Constructed per
    setup and stored in ``hass.data[DOMAIN][AUTOAPPROVE_PROVIDER_KEY]``; the views
    resolve it from ``hass.data`` per request, so a reload minting a fresh
    provider is transparent.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        webhook_id: str,
        public_base_url: str | None = None,
    ) -> None:
        self._hass = hass
        self._webhook_id = webhook_id
        self._public_base_url = public_base_url
        self._code_store = PKCECodeStore()

    @property
    def webhook_id(self) -> str:
        return self._webhook_id

    def resource_url(self, base_url: str) -> str:
        return f"{base_url}/api/webhook/{self._webhook_id}"

    def authorization_server_url(self, base_url: str) -> str:
        return _issuer(base_url)

    def base_url_for(self, request: web.Request) -> str:
        return _build_base_url(request, self._public_base_url)

    def issue_code(self, redirect_uri: str, code_challenge: str) -> str | None:
        """Issue a one-shot PKCE-bound authorization code (see PKCECodeStore)."""
        return self._code_store.issue_code(redirect_uri, code_challenge)

    def consume_code(self, code: str, redirect_uri: str, code_verifier: str) -> bool:
        """Verify PKCE S256 + one-shot consume a code (see PKCECodeStore)."""
        return self._code_store.consume_code(code, redirect_uri, code_verifier)

    @staticmethod
    def issue_access_token() -> str:
        """Mint an opaque access token.

        None mode ignores bearers (the secret webhook URL is the credential), so
        this token grants nothing — but it is a real random string, so a
        spec-strict client that stores/echoes it is satisfied.
        """
        return secrets.token_urlsafe(32)


def _active_autoapprove_provider(hass: HomeAssistant) -> AutoApproveProvider | None:
    """The live none-mode auto-approve provider, or None when it is not live.

    Read live from ``hass.data`` (not captured at view construction) so the bound
    views serve only while none-autoapprove is the active mode and 404 otherwise
    — mirrors ``oauth._active_oauth_mode``'s per-request gating.
    """
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return None
    provider = domain_data.get(AUTOAPPROVE_PROVIDER_KEY)
    return provider if isinstance(provider, AutoApproveProvider) else None


def _domain_data(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return the live proxy data used for per-request mode dispatch."""
    data = hass.data.get(DOMAIN)
    return data if isinstance(data, dict) else None


class AutoApproveAuthorizeView(HomeAssistantView):
    """Unified scoped authorization dispatcher for all three proxy modes.

    Validates ``response_type=code``, PKCE S256, and the redirect_uri
    open-redirect gate, then issues a PKCE-bound one-time code and redirects
    straight back to the client. No login page and no consent screen render, so
    claude.ai's OAuth flow completes invisibly (issue #1969).

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
    name = "mcp_proxy_dev:oauth:autoapprove-authorize"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Dispatch an authorization request to the currently active mode."""
        if not await _addon_alive(self._hass):
            return _json_not_found()
        data = _domain_data(self._hass)
        if data is None:
            return _json_not_found()
        mode = data.get("oauth_mode")
        if mode == MODE_LEGACY:
            provider = data.get("oauth")
            if provider is None:
                return _json_not_found()
            return await handle_legacy_authorize_get(provider, request)
        if mode == MODE_HA_AUTH:
            return await self._ha_auth_authorize(data, request)
        if mode != MODE_NONE_AUTOAPPROVE:
            return _json_not_found()
        provider = data.get(AUTOAPPROVE_PROVIDER_KEY)
        if not isinstance(provider, AutoApproveProvider):
            return _json_not_found()
        return self._autoapprove_authorize(provider, request)

    def _autoapprove_authorize(
        self, provider: AutoApproveProvider, request: web.Request
    ) -> web.Response:
        """Issue a code invisibly for any structurally valid none-mode request."""
        params = request.query
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        if error := _validate_autoapprove_authorize(params):
            return error

        # RFC 9207: every authorization response — success or error — names the
        # issuer that produced it, so a client registered with several
        # authorization servers cannot be fed a response minted by another one.
        iss = _issuer(provider.base_url_for(request))

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
        self, data: dict[str, Any], request: web.Request
    ) -> web.Response:
        """Redirect the browser into core after CIMD or DCR translation."""
        from multidict import MultiDict

        from .oauth_dcr import CFG_DCR_SIGNING_KEY
        from .oauth_indirect import resolve_forward_client_id

        params = MultiDict(request.query)
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        forward_id = await resolve_forward_client_id(
            data.get(CFG_CIMD_SESSION),
            data.get(CFG_DCR_SIGNING_KEY),
            client_id,
            redirect_uri,
        )
        if forward_id != client_id:
            params.popall("client_id", None)
            params["client_id"] = forward_id

        import yarl

        target = yarl.URL("/auth/authorize").with_query(params)
        return web.Response(status=302, headers={"Location": str(target)})

    async def post(self, request: web.Request) -> web.Response:
        """Handle legacy consent submissions on the scoped authorize route."""
        if not await _addon_alive(self._hass):
            return _json_not_found()
        data = _domain_data(self._hass)
        if data is None or data.get("oauth_mode") != MODE_LEGACY:
            return _json_not_found()
        provider = data.get("oauth")
        if provider is None:
            return _json_not_found()
        return await handle_legacy_authorize_post(provider, request)


def _revoke_rewrite(dcr_key: bytes | None, form: MultiDict) -> bool:
    """Swap an envelope back for core's own token on a revocation (#2248).

    Core takes a revocation two ways — ``action=revoke&token=…`` on
    ``/auth/token`` (the IndieAuth 6.3.5 shape, which core marks deprecated in
    favour of the view below but keeps for backwards compat) and the RFC 7009
    ``/auth/revoke`` view — and BOTH answer 200 even for a token they have
    never seen, so a client revoking the envelope we handed it would get a
    success it did not receive. The single place that knows how a revocation
    token is unwrapped, shared by the token view's ``action=revoke`` branch and
    :class:`AutoApproveRevokeView`. RFC 7009 authorizes the BEARER of the token
    rather than a client identity, so the presenter binding is deliberately
    skipped here. Returns whether the request must now be proxied (core has to
    see its own token).
    """
    from .oauth_indirect import unwrap_refresh_token

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
    from .oauth_indirect import EnvelopeState, unwrap_refresh_token

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
    data: dict[str, Any], dcr_key: bytes | None, client_id: str
) -> tuple[str, bool] | web.Response:
    """Re-derive the identity of a redirect-less PRE-envelope refresh (#2248).

    Only ``EnvelopeState.ABSENT`` tokens reach here — minted before the
    envelope shipped, or presented while no DCR key is configured.
    """
    from .oauth_indirect import RefreshDisposition, translated_client_id_for_refresh

    translated = await translated_client_id_for_refresh(
        data.get(CFG_CIMD_SESSION),
        dcr_key,
        client_id,
    )
    if translated is RefreshDisposition.UNREPRODUCIBLE:
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
    data: dict[str, Any], dcr_key: bytes | None, client_id: str
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
    from .oauth_indirect import RefreshDisposition, translated_client_id_for_refresh

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
        data.get(CFG_CIMD_SESSION),
        dcr_key,
        client_id,
    )
    return disposition is RefreshDisposition.UNREPRODUCIBLE


async def _forward_to_core(
    hass: HomeAssistant, data: dict[str, Any], path: str, form: MultiDict
) -> tuple[int, bytes, str] | web.Response:
    """POST ``form`` to core's ``path`` server-side; its raw answer or a 503.

    Shared by the token and revocation legs (#2248): both forward a rewritten
    credential-bearing form to core over the relay session and map the same
    transport failures, and only the token leg rewrites what comes back.
    """
    from .oauth_indirect import core_token_base_url

    session = data.get("session")
    if session is None:
        _LOGGER.warning(
            "ha_auth %s forward: the entry has no relay session "
            "(half-initialised setup); answering 503",
            path,
        )
        return _json_error(
            "temporarily_unavailable", 503, "token forwarding is not available"
        )
    base = core_token_base_url(hass)
    try:
        async with session.post(
            f"{base}{path}",
            data=form,
            timeout=aiohttp.ClientTimeout(total=25),
        ) as response:
            return (
                response.status,
                await response.read(),
                response.content_type or "application/json",
            )
    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning(
            "ha_auth %s forward to %s failed: %s",
            path,
            base,
            type(err).__name__,
        )
        return _json_error(
            "temporarily_unavailable", 503, "core did not answer the token request"
        )


class AutoApproveTokenView(HomeAssistantView):
    """Unified scoped token dispatcher for all three proxy modes.

    Public client (no ``client_secret``): the PKCE code_verifier is the only
    proof required. The returned access token is cosmetic (none mode ignores
    bearers), but real and opaque. In none mode only the
    ``authorization_code`` grant is supported — that mode has no refresh cycle.
    """

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/token"
    name = "mcp_proxy_dev:oauth:autoapprove-token"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Dispatch a token request to the currently active mode."""
        if not await _addon_alive(self._hass):
            return _json_not_found()
        data = _domain_data(self._hass)
        if data is None:
            return _json_not_found()
        mode = data.get("oauth_mode")
        if mode == MODE_LEGACY:
            provider = data.get("oauth")
            if provider is None:
                return _json_not_found()
            return await handle_legacy_token_post(provider, request)
        if mode == MODE_HA_AUTH:
            return await self._ha_auth_token(data, request)
        if mode != MODE_NONE_AUTOAPPROVE:
            return _json_not_found()
        provider = data.get(AUTOAPPROVE_PROVIDER_KEY)
        if not isinstance(provider, AutoApproveProvider):
            return _json_not_found()
        return await self._autoapprove_token(provider, request)

    async def _autoapprove_token(
        self, provider: AutoApproveProvider, request: web.Request
    ) -> web.Response:
        """Exchange a none-mode code for a cosmetic bearer under PKCE proof."""
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
        self, data: dict[str, Any], request: web.Request
    ) -> web.Response:
        """Redirect unchanged grants to core or proxy translated identities.

        Every proxied 200 that is a grant response comes back with its
        ``refresh_token`` wrapped in the signed envelope that makes the next
        refresh resolvable (#2248). A revocation is proxied too when its
        ``token`` was one of those envelopes, but nothing is wrapped on the
        way back.
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
        dcr_key = data.get(CFG_DCR_SIGNING_KEY)
        client_id = str(form.get("client_id", ""))
        resolved = await self._ha_auth_forward_identity(data, form, client_id, dcr_key)
        if isinstance(resolved, web.Response):
            return resolved
        forward_id, proxy_required = resolved
        if forward_id == client_id and not proxy_required:
            return web.Response(
                status=307,
                headers={"Location": "/auth/token", "Cache-Control": "no-store"},
            )

        form.popall("client_id", None)
        form["client_id"] = forward_id
        return await self._proxy_token_to_core(
            data, form, forward_id, client_id, dcr_key
        )

    async def _ha_auth_forward_identity(
        self,
        data: dict[str, Any],
        form: MultiDict,
        client_id: str,
        dcr_key: bytes | None,
    ) -> tuple[str, bool] | web.Response:
        """Resolve the client_id to present to core, plus whether proxying is forced.

        Returns a ready ``web.Response`` when the grant must be answered
        locally. Mutates ``form`` wherever the wire value is one of ours and
        core must receive its own: the envelope in a refresh grant, and the
        envelope in a revocation's ``token``.

        Envelope first (#2248) — a wrapped token names the client_id core bound
        it to, so the identity is READ rather than re-derived, and the exchange
        must be proxied (a 307 would hand core an envelope it cannot redeem).
        Everything else keeps the pre-#2248 behavior.
        """
        from .oauth_indirect import resolve_forward_client_id

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
            return await _pre_envelope_refresh_identity(data, dcr_key, client_id)
        forward_id = await resolve_forward_client_id(
            data.get(CFG_CIMD_SESSION),
            dcr_key,
            client_id,
            redirect_uri,
        )
        if grant_type == "authorization_code" and forward_id == client_id:
            return client_id, await _code_leg_forces_proxy(data, dcr_key, client_id)
        return forward_id, False

    async def _proxy_token_to_core(
        self,
        data: dict[str, Any],
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
        from .oauth_indirect import rewrite_token_response_body

        forwarded = await _forward_to_core(self._hass, data, "/auth/token", form)
        if isinstance(forwarded, web.Response):
            return forwarded
        status, body, content_type = forwarded
        # Revocation answers 200 with an EMPTY body, so there is nothing to
        # rewrite and a warning would be pure noise.
        if status == 200 and dcr_key is not None and form.get("action") != "revoke":
            body = rewrite_token_response_body(dcr_key, body, forward_id, client_id)
        return web.Response(
            status=status,
            body=body,
            content_type=content_type,
            headers=_TOKEN_RESPONSE_HEADERS,
        )


class AutoApproveRevokeView(HomeAssistantView):
    """Scoped RFC 7009 revocation endpoint, served in ha_auth mode only (#2248).

    Core's own ``POST /auth/revoke`` answers 200 for a token it has never seen
    (RFC 7009 §2.2, which core's ``RevokeTokenView`` cites verbatim), so a
    client that posts the signed envelope the proxy handed it straight to core
    gets a silent no-op and keeps a live session. Fronting revocation here is
    what lets the envelope be swapped for core's own token first. Legacy and
    none mode never mint an envelope, so this route 404s there — the envelope
    is the whole reason it exists.

    ANONYMOUS BY DESIGN, like core's own revocation view (``requires_auth =
    False``, ``cors_allowed = True``, mirrored here): RFC 7009 authorizes the
    BEARER of the token, not a client identity. That grants no new reach. A
    caller who cannot present a valid HMAC-signed envelope never causes an
    outbound request at all — an unrecognised or forged token 307s and this
    proxy makes no call — so the forwarding path is reachable only by someone
    already holding the credential they are asking us to destroy.
    """

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/revoke"
    name = "mcp_proxy_dev:oauth:autoapprove-revoke"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Unwrap an envelope and forward, or 307 the revocation into core."""
        if not await _addon_alive(self._hass):
            return _json_not_found()
        data = _domain_data(self._hass)
        if data is None or data.get("oauth_mode") != MODE_HA_AUTH:
            return _json_not_found()
        from multidict import MultiDict

        from .oauth_dcr import CFG_DCR_SIGNING_KEY

        raw_form = await read_form(request)
        if raw_form is None:
            return _json_error("invalid_request", 400)
        # str()-coerced MultiDict for the same reason the token view builds
        # one: bytes/FileField values from a multipart body would raise a
        # TypeError inside the outgoing serializer (#2219).
        form: MultiDict = MultiDict(
            (key, str(value)) for key, value in raw_form.items()
        )
        if not _revoke_rewrite(data.get(CFG_DCR_SIGNING_KEY), form):
            # Nothing of ours in the body, so no rewrite is needed: 307 the
            # client into core's own /auth/revoke, which then observes the
            # CLIENT's address. Relative Location for the token view's reason
            # — an absolute one would derive the credential target from
            # unvalidated forwarded headers.
            return web.Response(
                status=307,
                headers={"Location": "/auth/revoke", "Cache-Control": "no-store"},
            )
        forwarded = await _forward_to_core(self._hass, data, "/auth/revoke", form)
        if isinstance(forwarded, web.Response):
            # The helper's only Response is the 503, which RFC 7009 §2.2.1
            # gives a specific meaning here: the token is still live. Name the
            # retry delay rather than leaving the client to invent one.
            forwarded.headers["Retry-After"] = _REVOKE_RETRY_AFTER
            return forwarded
        status, body, content_type = forwarded
        # Relayed as-is: core answers a revocation with an empty 200, so there
        # is never a refresh_token to wrap on the way back.
        return web.Response(
            status=status,
            body=body,
            content_type=content_type,
            headers=_TOKEN_RESPONSE_HEADERS,
        )


def register_autoapprove_views(hass: HomeAssistant) -> None:
    """Bind the three unified scoped views at most once per HA session.

    aiohttp cannot unregister a bound view, so a reload / re-enable / mode switch
    must reuse the already-bound views. They resolve the active mode from
    ``hass.data`` on every request, so the same URLs serve legacy, ha_auth, or
    none-autoapprove without rebinding. The flag is set only after ALL THREE
    register, so a partial bind never reads as a bound bundle.
    """
    if hass.data.get(_AUTOAPPROVE_VIEWS_REGISTERED_KEY):
        return
    hass.http.register_view(AutoApproveAuthorizeView(hass))
    hass.http.register_view(AutoApproveTokenView(hass))
    hass.http.register_view(AutoApproveRevokeView(hass))
    hass.data[_AUTOAPPROVE_VIEWS_REGISTERED_KEY] = True
