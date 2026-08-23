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

The path-scoped ``OAUTH_BASE`` authorize and token routes dispatch per request
to legacy, ha_auth, or none-autoapprove. Cached clients therefore keep calling
proxy-owned endpoints across mode switches. In none mode the exchange remains
invisible and its access token remains cosmetic:

* ``GET  {OAUTH_BASE}/authorize`` issues a PKCE-bound one-time code and
  immediately 302-redirects back to the client with ``?code=…&state=…`` — no
  page is rendered.
* ``POST {OAUTH_BASE}/token`` exchanges that code (public client, PKCE S256, no
  ``client_secret``) for an opaque access token. The token is *cosmetic* — none
  mode ignores bearers entirely — but is a real random string so a spec-strict
  client is satisfied.

In none mode the secret webhook URL remains the only credential and the OAuth
token grants nothing. By maintainer decision, authorize therefore accepts any
spec-valid HTTPS or RFC 8252 loopback callback; malformed targets still fail in
place. The resulting crafted-link redirect behavior is accepted within that
secret-URL trust model.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

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

# Dedicated session for anonymous CIMD lookups. Keeping it separate prevents a
# slow public metadata host from consuming the authenticated relay pool.
CFG_CIMD_SESSION = "cimd_session"

# TOP-LEVEL hass.data flag recording that the two auto-approve views are bound
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


def _wrap_refresh_token(
    body: bytes,
    signing_key: bytes,
    client_id: str,
    origin: str,
) -> bytes:
    """Wrap a core refresh token with its signed loopback identity."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return body
    if not isinstance(payload, dict) or not isinstance(
        payload.get("refresh_token"), str
    ):
        return body
    from .oauth_dcr import mint_refresh_token_envelope

    payload["refresh_token"] = mint_refresh_token_envelope(
        signing_key,
        client_id,
        payload["refresh_token"],
        origin,
    )
    return json.dumps(payload, separators=(",", ":")).encode()


def _maybe_wrap_refresh_token(
    body: bytes,
    status: int,
    dcr_key: object,
    client_id: str,
    origin: str | None,
) -> bytes:
    """Wrap a successful fixed-loopback refresh token when context is valid."""
    if status != 200 or not isinstance(dcr_key, bytes) or origin is None:
        return body
    return _wrap_refresh_token(body, dcr_key, client_id, origin)


def _refresh_envelope_origin(
    grant_type: str,
    dcr_key: object,
    client_id: str,
    redirect_uri: str,
) -> str | None:
    """Return the runtime origin to bind for a fixed-loopback code exchange."""
    if (
        grant_type != "authorization_code"
        or not isinstance(dcr_key, bytes)
        or not client_id
        or not redirect_uri
    ):
        return None
    from .oauth_dcr import client_redirect_uris, stable_refresh_origin
    from .oauth_indirect import origin_client_id, redirect_matches

    registered = client_redirect_uris(dcr_key, client_id)
    if registered is None:
        return None
    stable = stable_refresh_origin(registered)
    if not stable or not stable.startswith("http://"):
        return None
    if not redirect_matches(registered, redirect_uri):
        return None
    return origin_client_id(redirect_uri)


async def _refresh_translation(
    cimd_session: Any,
    dcr_key: object,
    client_id: str,
    refresh_token: str,
    redirect_uri: str,
) -> tuple[object, str | None, str | None, str | None]:
    """Resolve a refresh identity and unwrap a bound loopback token."""
    if isinstance(dcr_key, bytes):
        from .oauth_dcr import unwrap_refresh_token_envelope

        wrapped = unwrap_refresh_token_envelope(dcr_key, client_id, refresh_token)
        if wrapped is not None:
            core_refresh_token, origin = wrapped
            if redirect_uri:
                from .oauth_indirect import RefreshDisposition, origin_client_id

                if (
                    not _is_valid_redirect_uri(redirect_uri)
                    or origin_client_id(redirect_uri) != origin
                ):
                    return (
                        RefreshDisposition.UNREPRODUCIBLE,
                        None,
                        None,
                        "re-authorize: redirect_uri does not match this refresh "
                        "token's signed loopback origin",
                    )
            return origin, core_refresh_token, origin, None
    from .oauth_indirect import (
        RefreshDisposition,
        resolve_forward_client_id,
        translated_client_id_for_refresh,
    )

    if redirect_uri:
        translated = await resolve_forward_client_id(
            cimd_session, dcr_key, client_id, redirect_uri
        )
        return translated, None, None, None

    translated = await translated_client_id_for_refresh(
        cimd_session,
        dcr_key,
        client_id,
    )
    refresh_origin = (
        translated
        if isinstance(translated, str) and translated.startswith("http://")
        else None
    )
    error_description = (
        "re-authorize: this client's registration has no single reproducible "
        "origin, so a refresh without redirect_uri is unavailable"
        if translated is RefreshDisposition.UNREPRODUCIBLE
        else None
    )
    return translated, None, refresh_origin, error_description


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


class AutoApproveTokenView(HomeAssistantView):
    """Unified scoped token dispatcher for all three proxy modes.

    Public client (no ``client_secret``): the PKCE code_verifier is the only
    proof required. The returned access token is cosmetic (none mode ignores
    bearers), but real and opaque. Only the ``authorization_code`` grant is
    supported — none mode has no refresh cycle.
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
        """Redirect unchanged grants to core or proxy translated identities."""
        from multidict import MultiDict

        from .oauth_dcr import CFG_DCR_SIGNING_KEY
        from .oauth_indirect import (
            RefreshDisposition,
            core_token_base_url,
            resolve_forward_client_id,
        )

        raw_form = await read_form(request)
        if raw_form is None:
            return _json_error("invalid_request", 400)
        form = MultiDict(raw_form)
        grant_type = str(form.get("grant_type", ""))
        client_id = str(form.get("client_id", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        forward_id = client_id
        refresh_origin: str | None = None
        dcr_key = data.get(CFG_DCR_SIGNING_KEY)
        if client_id:
            if grant_type == "refresh_token":
                (
                    translated,
                    core_refresh_token,
                    refresh_origin,
                    refresh_error,
                ) = await _refresh_translation(
                    data.get(CFG_CIMD_SESSION),
                    dcr_key,
                    client_id,
                    str(form.get("refresh_token", "")),
                    redirect_uri,
                )
                if core_refresh_token is not None:
                    form.popall("refresh_token", None)
                    form["refresh_token"] = core_refresh_token
                if translated is RefreshDisposition.UNREPRODUCIBLE:
                    return _json_error(
                        "invalid_grant",
                        400,
                        refresh_error,
                    )
                if translated is not RefreshDisposition.PASSTHROUGH:
                    forward_id = translated
            else:
                forward_id = await resolve_forward_client_id(
                    data.get(CFG_CIMD_SESSION),
                    dcr_key,
                    client_id,
                    redirect_uri,
                )
                refresh_origin = _refresh_envelope_origin(
                    grant_type, dcr_key, client_id, redirect_uri
                )

        if forward_id == client_id:
            return web.Response(
                status=307,
                headers={"Location": "/auth/token", "Cache-Control": "no-store"},
            )

        form.popall("client_id", None)
        form["client_id"] = forward_id
        session = data.get("session")
        if session is None:
            return _json_error("temporarily_unavailable", 503)
        base = core_token_base_url(self._hass)
        try:
            async with session.post(
                f"{base}/auth/token",
                data=form,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as response:
                body = await response.read()
                body = _maybe_wrap_refresh_token(
                    body, response.status, dcr_key, client_id, refresh_origin
                )
                return web.Response(
                    status=response.status,
                    body=body,
                    content_type=response.content_type or "application/json",
                    headers=_TOKEN_RESPONSE_HEADERS,
                )
        except (TimeoutError, aiohttp.ClientError):
            return _json_error("temporarily_unavailable", 503)


def register_autoapprove_views(hass: HomeAssistant) -> None:
    """Bind the two unified scoped views at most once per HA session.

    aiohttp cannot unregister a bound view, so a reload / re-enable / mode switch
    must reuse the already-bound views. They resolve the active mode from
    ``hass.data`` on every request, so the same URLs serve legacy, ha_auth, or
    none-autoapprove without rebinding.
    """
    if hass.data.get(_AUTOAPPROVE_VIEWS_REGISTERED_KEY):
        return
    hass.http.register_view(AutoApproveAuthorizeView(hass))
    hass.http.register_view(AutoApproveTokenView(hass))
    hass.data[_AUTOAPPROVE_VIEWS_REGISTERED_KEY] = True
