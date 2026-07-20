"""Webhook ingress for the in-process ha-mcp server (issue #1527).

Ported from the proven webhook-proxy add-on (``mcp_proxy``): an HA webhook
(``/api/webhook/<id>``) forwards MCP traffic to the loopback server and streams
the response back, so the server is reachable through Nabu Casa remote UI (or any
reverse proxy) with the webhook id as the shared secret.

Three auth postures, chosen in the options flow:

* ``none`` — the secret webhook URL *is* the credential (matches the add-on's
  default). No bearer is required and the forwarder always returns 200. It still
  serves our own corrected RFC 8414 / RFC 9728 discovery documents plus an
  invisible auto-approve authorization server (:mod:`oauth_autoapprove`), so
  claude.ai's intermittent OAuth discovery resolves against us — not HA core's
  broken origin-root doc — and connects with no HA login (issue #1969).
* ``ha_auth`` — Home Assistant core is the OAuth authorization server. This
  module serves the RFC 8414 / RFC 9728 discovery documents (so claude.ai /
  ChatGPT can sign in with the user's HA account) and validates inbound bearer
  tokens via ``hass.auth``. There is no bespoke authorization-server code here —
  every protocol step is HA core's own ``/auth/*``.
* ``legacy`` — this module (via :mod:`oauth_legacy`) is its own OAuth 2.1
  authorization server with a static client_id/secret, for MCP clients (Google
  Gemini Spark) that need a credential to paste rather than an HA sign-in.

The forwarding handler mirrors ``mcp_proxy._handle_webhook`` exactly (hop-by-hop
header stripping, the SSE streaming branch with anti-buffering headers, the
content-type whitelist, ``Mcp-Session-Id`` propagation, and the 502/500 error
mapping); the ``ha_auth`` bearer check + discovery documents mirror the add-on's
``auth_native.py`` + the ``ha_auth`` subset of ``oauth.py``; the ``legacy``
provider + its root ``/authorize`` + ``/token`` views live in
:mod:`oauth_legacy`, ported from the ``legacy`` subset of the add-on's
``oauth.py``. The seven RFC 8414 / RFC 9728 discovery views below are shared by
``ha_auth``, ``legacy``, and ``none`` (which serves a distinct auto-approve
authorization-server document pointing at :mod:`oauth_autoapprove`'s endpoints)
— see :func:`active_auth_mode`.
"""

from __future__ import annotations

import inspect
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.webhook import async_register, async_unregister
from homeassistant.core import HomeAssistant

from .const import (
    DATA_WEBHOOK,
    DATA_WEBHOOK_ID,
    DOMAIN,
    OAUTH_BASE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)
from .oauth_autoapprove import (
    CFG_AUTOAPPROVE_PROVIDER,
    AutoApproveProvider,
    bind_autoapprove_views,
)
from .oauth_legacy import (
    AUTHORIZE_PATH,
    OAUTH_ROUTE_OWNER_KEY,
    TOKEN_PATH,
    LegacyOAuthProvider,
    LegacyOAuthRouteConflict,
    bind_legacy_views,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Human-readable webhook name shown in the HA webhook registry.
_WEBHOOK_NAME = "HA-MCP in-process server"

# Hop-by-hop / sensitive request headers never forwarded upstream (identical set
# to mcp_proxy). ``authorization`` is stripped because the server authenticates
# to HA with its own provisioned token, not the caller's bearer.
_STRIPPED_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "cookie",
        "authorization",
    }
)

# Content-Types the forwarded response may carry as-is; anything else is coerced
# to JSON to prevent HTML injection / XSS through the proxy. ``text/plain`` is
# safe (a browser never executes it) and lets the server's friendly landing page
# — a plain-text 405 shown when a browser GETs the endpoint — render as text
# instead of a mislabeled JSON blob. ``text/html`` and friends stay coerced.
_ALLOWED_CONTENT_TYPES = ("application/json", "text/event-stream", "text/plain")

# Long timeout for streamed MCP responses (matches mcp_proxy).
_CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_connect=10, sock_read=300)

# TOP-LEVEL hass.data flag recording that the ha_auth discovery views are bound
# for this HA session. Deliberately NOT under DOMAIN so it survives
# async_unload_entry's teardown — aiohttp cannot unregister an HTTP view until HA
# restarts, so the views (and this ownership flag) must outlive the config entry.
_OAUTH_VIEWS_REGISTERED_KEY = "ha_mcp_tools_oauth_metadata_views_registered"


# ---------------------------------------------------------------------------
# ha_auth resource server (HA core is the OAuth authorization server)
# ---------------------------------------------------------------------------


def _build_base_url(request: web.Request) -> str:
    """Build the public base URL from the request (host-derived).

    ha_auth is always host-derived so the SAME install works via the Nabu Casa
    cloud URL AND any other external URL. Reads ``X-Forwarded-Proto/Host`` as
    sent: HA's forwarded middleware only validates proxy headers when
    ``X-Forwarded-For`` is present, so these can reach us raw. A peer can
    thereby only shape the discovery/WWW-Authenticate URLs in its OWN
    response (no cross-user vector), which is within SECURITY.md's
    local-network trust model; treat stricter proxy validation as optional
    hardening.
    """
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{scheme}://{host}"


def _authorization_server_document(base: str) -> dict[str, Any]:
    """RFC 8414 authorization-server metadata pointing at HA core's OAuth.

    Advertises HA core's own ``/auth/authorize`` + ``/auth/token`` as a public
    client (``token_endpoint_auth_methods_supported: ["none"]``) and
    ``client_id_metadata_document_supported`` so clients present a URL-shaped
    ``client_id`` (CIMD) that HA core's long-standing IndieAuth handling accepts —
    the user never pastes a credential. No ``registration_endpoint``: HA offers no
    dynamic client registration; CIMD replaces it.
    """
    return {
        "issuer": f"{base}{OAUTH_BASE}",
        "authorization_endpoint": f"{base}/auth/authorize",
        "token_endpoint": f"{base}/auth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
    }


class ResourceServer:
    """ha_auth resource server: bearer validation + discovery URL building.

    Owns no signing key, no client credentials, and binds no root views — HA core
    is the authorization server. Held by the discovery views and the webhook
    handler.
    """

    def __init__(self, hass: HomeAssistant, webhook_id: str) -> None:
        """Bind to the HA instance and this install's webhook id."""
        self._hass = hass
        self._webhook_id = webhook_id

    @property
    def webhook_id(self) -> str:
        """This install's private webhook id."""
        return self._webhook_id

    async def validate_request(self, request: web.Request) -> bool:
        """Return True iff the request carries a Bearer token HA core accepts.

        A missing/malformed ``Authorization`` header is rejected without touching
        the validator. ``hass.auth.async_validate_access_token`` is a synchronous
        ``@callback`` in HA core; it is awaited defensively in case a future
        release makes it a coroutine, and any raise is treated as unauthorized so
        a crafted token yields a 401 challenge rather than a 500.
        """
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        token = header[7:].strip()
        if not token:
            return False
        try:
            result = self._hass.auth.async_validate_access_token(token)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            _LOGGER.debug(
                "ha_auth: bearer validation raised; treating as unauthorized",
                exc_info=True,
            )
            return False
        if result is None:
            return False
        # ADMIN-ONLY: the server performs every Home Assistant operation with
        # its own provisioned ADMIN token, so accepting any valid login would
        # grant every household member admin-equivalent control. Require an
        # active, human, administrator account (mirrors the settings panel).
        user = getattr(result, "user", None)
        if user is None:
            return False
        if getattr(user, "system_generated", False):
            return False
        if not getattr(user, "is_active", False):
            return False
        return bool(getattr(user, "is_admin", False))


# ---------------------------------------------------------------------------
# RFC 8414 / RFC 9728 discovery views (ha_auth + legacy modes)
# ---------------------------------------------------------------------------


def _active_webhook_cfg(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return the live webhook forwarding cfg dict, or None if not set up."""
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return None
    cfg = domain_data.get(DATA_WEBHOOK)
    return cfg if isinstance(cfg, dict) else None


def active_auth_mode(hass: HomeAssistant) -> str | None:
    """Return the OAuth-relevant auth mode of the live webhook registration.

    ``WEBHOOK_AUTH_HA``, ``WEBHOOK_AUTH_LEGACY``, or ``WEBHOOK_AUTH_NONE`` (the
    none-mode auto-approve surface, issue #1969), or None when no discovery
    surface is live. Checked via PROVIDER PRESENCE, not the raw configured
    ``auth_mode`` string, so local-only mode (remote webhook disabled by
    option — ``register_endpoint=False`` in ``async_register_webhook``)
    correctly reports None even when ``webhook_auth`` is set: no provider is
    constructed for a webhook that was never registered, so there is nothing to
    advertise or authenticate against. Read live from hass.data (not captured at view/provider
    construction time) so the SAME registered/bound instances serve whichever
    mode is active now — mirrors the add-on's ``_active_oauth_mode``. Used by
    the discovery views below AND by ``LegacyOAuthProvider.is_active`` (via
    the getter passed into :func:`oauth_legacy.bind_legacy_views`) so the
    root ``/authorize``/``/token`` views, which aiohttp can never unbind, 404
    once the operator switches away from legacy (or to local-only mode)
    without a restart. The webhook forwarder's own bearer gate
    (``_async_handle_webhook``) reads ``cfg["resource_server"]`` /
    ``cfg["oauth_provider"]`` directly instead of through this function — it
    already has ``cfg`` in hand and needs the provider OBJECT, not just the
    mode name.
    """
    cfg = _active_webhook_cfg(hass)
    if cfg is None:
        return None
    if cfg.get("resource_server") is not None:
        return WEBHOOK_AUTH_HA
    if cfg.get("oauth_provider") is not None:
        return WEBHOOK_AUTH_LEGACY
    if cfg.get(CFG_AUTOAPPROVE_PROVIDER) is not None:
        return WEBHOOK_AUTH_NONE
    return None


def _active_webhook_id(hass: HomeAssistant) -> str | None:
    """Webhook id of the live registration, gated the same as the AS document
    (None whenever :func:`active_auth_mode` is None) so the protected-resource
    document 404s in exactly the same cases."""
    if active_auth_mode(hass) is None:
        return None
    cfg = _active_webhook_cfg(hass)
    return cfg.get("webhook_id") if cfg is not None else None


def _json_not_found() -> web.Response:
    """404 JSON body used by stale-but-bound discovery views."""
    return web.json_response({"error": "not_found"}, status=404)


def _protected_resource_document(webhook_id: str, base: str) -> dict[str, Any]:
    """RFC 9728 protected-resource document for ``webhook_id`` under ``base``.

    Identical shape in both OAuth modes — only the authorization-server
    document (below) differs by mode.
    """
    return {
        "resource": f"{base}/api/webhook/{webhook_id}",
        "authorization_servers": [f"{base}{OAUTH_BASE}"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/homeassistant-ai/ha-mcp",
    }


def _legacy_authorization_server_document(base: str) -> dict[str, Any]:
    """RFC 8414 authorization-server metadata for legacy mode's own root
    ``/authorize`` + ``/token`` views (see :mod:`oauth_legacy`)."""
    return {
        "issuer": f"{base}{OAUTH_BASE}",
        "authorization_endpoint": f"{base}{AUTHORIZE_PATH}",
        "token_endpoint": f"{base}{TOKEN_PATH}",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
        ],
    }


def _none_mode_authorization_server_document(base: str) -> dict[str, Any]:
    """RFC 8414 authorization-server metadata for none mode's auto-approve server.

    Points at OUR OWN ``OAUTH_BASE`` ``/authorize`` + ``/token`` (the invisible
    auto-approve endpoints in :mod:`oauth_autoapprove`), NOT HA core's
    ``/auth/*``. Serving this — with ``token_endpoint_auth_methods_supported:
    ["none"]`` (public PKCE client) and ``client_id_metadata_document_supported``
    — is the none-mode fix: claude.ai's intermittent discovery resolves against
    this corrected document instead of HA core's origin-root
    ``/.well-known/oauth-authorization-server``, which omits the ``"none"`` auth
    method and has no ``registration_endpoint`` (issue #1969). No refresh grant:
    the token is cosmetic (none mode ignores bearers), so only
    ``authorization_code`` is advertised.
    """
    return {
        "issuer": f"{base}{OAUTH_BASE}",
        "authorization_endpoint": f"{base}{OAUTH_BASE}/authorize",
        "token_endpoint": f"{base}{OAUTH_BASE}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
    }


class _ProtectedResourceMetadataView(HomeAssistantView):
    """RFC 9728 Protected Resource Metadata."""

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/protected-resource"
    name = "ha_mcp_tools:oauth:protected-resource"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Serve the protected-resource document for the bearer-gated modes only.

        SECURITY (#1976 review): this ANONYMOUS, fixed (guessable) path exposes
        ``resource: <base>/api/webhook/<id>``. In none mode the webhook id is the
        SOLE credential, so serving it here would leak it to any unauthenticated
        GET. Serve only for ``ha_auth``/``legacy`` (where the id is not a secret
        and the 401 ``WWW-Authenticate`` pointer legitimately directs a client
        here); 404 otherwise. The PATH-SCOPED well-known view still serves in none
        mode — its caller must already know the id (it is a route parameter).
        """
        if active_auth_mode(self._hass) not in (WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY):
            return _json_not_found()
        webhook_id = _active_webhook_id(self._hass)
        if webhook_id is None:
            return _json_not_found()
        return web.json_response(
            _protected_resource_document(webhook_id, _build_base_url(request))
        )


class _AuthorizationServerMetadataView(HomeAssistantView):
    """RFC 8414 Authorization Server Metadata.

    Mode-aware: ha_auth points at HA core's own ``/auth/*``; legacy points at
    this module's root ``/authorize``/``/token`` views.
    """

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/authorization-server"
    name = "ha_mcp_tools:oauth:authorization-server"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Serve the AS document (or 404 when no OAuth mode is live)."""
        mode = active_auth_mode(self._hass)
        if mode is None:
            return _json_not_found()
        base = _build_base_url(request)
        if mode == WEBHOOK_AUTH_LEGACY:
            return web.json_response(_legacy_authorization_server_document(base))
        if mode == WEBHOOK_AUTH_NONE:
            return web.json_response(_none_mode_authorization_server_document(base))
        return web.json_response(_authorization_server_document(base))


class _WellKnownProtectedResourceView(HomeAssistantView):
    """RFC 9728 §3.1 path-scoped Protected Resource Metadata.

    Same document as :class:`_ProtectedResourceMetadataView`, served at the
    well-known location derived from the webhook resource URL — claude.ai's
    first fallback probe when the 401's ``resource_metadata`` pointer is
    missing. The webhook id is a ROUTE PARAMETER (not baked into the path at
    registration): a remove + re-add of the entry mints a new webhook id in the
    same HA session, and the bound view must serve whichever id is currently
    live (404 for any other). Standalone view (not a subclass of the plain
    document view) because its handler takes the extra route parameter.
    """

    requires_auth = False
    cors_allowed = True
    name = "ha_mcp_tools:oauth:wellknown-protected-resource"
    url = "/.well-known/oauth-protected-resource/api/webhook/{webhook_id}"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def get(self, request: web.Request, webhook_id: str) -> web.Response:
        """Serve the document only for the CURRENT entry's webhook id."""
        active_id = _active_webhook_id(self._hass)
        if active_id is None or webhook_id != active_id:
            return _json_not_found()
        return web.json_response(
            _protected_resource_document(active_id, _build_base_url(request))
        )


class _WellKnownAuthorizationServerMetadataView(_AuthorizationServerMetadataView):
    """RFC 8414 / OIDC-discovery locations for the AS metadata document.

    Same document as :class:`_AuthorizationServerMetadataView`, registered at the
    well-known URLs MCP clients actually probe for the issuer.
    """

    def __init__(self, hass: HomeAssistant, url: str, name: str) -> None:
        """Bind and set an explicit well-known URL + unique view name."""
        super().__init__(hass)
        self.url = url
        self.name = name


def _metadata_views(hass: HomeAssistant) -> list[HomeAssistantView]:
    """Build the seven discovery-document views, shared by ha_auth and legacy
    (mode-agnostic — each view resolves the active mode per request)."""
    views: list[HomeAssistantView] = [
        _ProtectedResourceMetadataView(hass),
        _AuthorizationServerMetadataView(hass),
        _WellKnownProtectedResourceView(hass),
    ]
    for url, name in (
        (
            f"/.well-known/oauth-authorization-server{OAUTH_BASE}",
            "ha_mcp_tools:oauth:wellknown-as-rfc8414",
        ),
        (
            f"/.well-known/openid-configuration{OAUTH_BASE}",
            "ha_mcp_tools:oauth:wellknown-oidc-prefixed",
        ),
        (
            f"{OAUTH_BASE}/.well-known/openid-configuration",
            "ha_mcp_tools:oauth:wellknown-oidc-suffixed",
        ),
        (
            f"{OAUTH_BASE}/.well-known/oauth-authorization-server",
            "ha_mcp_tools:oauth:wellknown-as-suffixed",
        ),
    ):
        views.append(
            _WellKnownAuthorizationServerMetadataView(hass, url=url, name=name)
        )
    return views


def _register_metadata_views(hass: HomeAssistant) -> None:
    """Register the seven discovery views at most once per HA session.

    aiohttp cannot unregister a bound view, so a reload / re-enable / re-add /
    ha_auth<->legacy mode switch must all reuse the already-bound views — they
    resolve the ACTIVE mode + provider from hass.data per request (see
    ``active_auth_mode``), so a later entry (even with a new webhook id, or a
    different auth mode) is served correctly. The guard flag lives at a
    top-level hass.data key that survives config-entry teardown.
    """
    if hass.data.get(_OAUTH_VIEWS_REGISTERED_KEY):
        return
    # Set the flag only AFTER every view registers (issue #1978): it must mean
    # "the full bundle is bound", so a partial bind stays distinguishable from a
    # complete one. Marking it bound early would let a later setup assign a
    # provider and advertise discovery while some RFC metadata routes are still
    # unbound — a 404 for the clients that probe them. On a partial bind the flag
    # stays unset; the none-mode caller then fails open (the retry's duplicate
    # register is caught harmlessly) while ha_auth/legacy fail closed.
    for view in _metadata_views(hass):
        hass.http.register_view(view)
    hass.data[_OAUTH_VIEWS_REGISTERED_KEY] = True


def _build_unauthorized_response(request: web.Request) -> web.Response:
    """Build the 401 + ``WWW-Authenticate`` challenge MCP clients use to discover.

    Per RFC 9728 §5.1 / MCP spec, the ``resource_metadata`` parameter points to
    the protected-resource metadata URL where the client finds the authorization
    server.
    """
    base = _build_base_url(request)
    metadata_url = f"{base}{OAUTH_BASE}/protected-resource"
    return web.Response(
        status=401,
        text="Unauthorized",
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="HA-MCP", resource_metadata="{metadata_url}"'
            )
        },
    )


# ---------------------------------------------------------------------------
# Webhook forwarding handler
# ---------------------------------------------------------------------------


async def _check_webhook_auth(
    request: web.Request, cfg: dict[str, Any]
) -> web.StreamResponse | None:
    """Return a 401 challenge response if the request fails the auth gate, else None."""
    # Auth gate. ``none`` = the secret webhook URL is the credential; ``ha_auth``
    # validates the bearer via HA core; ``legacy`` validates it against this
    # module's own opaque tokens. Either failure emits the same 401 discovery
    # challenge so the client can start the OAuth flow. Gate on the PROVIDER
    # (constructed only for the matching mode) rather than a string compare,
    # so the coupling "provider present <=> mode" has a single owner and an
    # inconsistent cfg cannot fail open. auth_mode makes the two mutually
    # exclusive, so at most one of these is ever set.
    resource_server: ResourceServer | None = cfg.get("resource_server")
    if resource_server is not None and not await resource_server.validate_request(
        request
    ):
        return _build_unauthorized_response(request)
    oauth_provider: LegacyOAuthProvider | None = cfg.get("oauth_provider")
    if oauth_provider is not None and not oauth_provider.validate_bearer(request):
        return _build_unauthorized_response(request)
    return None


async def _async_handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.StreamResponse:
    """Forward an MCP request to the loopback server and stream the reply back."""
    domain_data = hass.data.get(DOMAIN)
    cfg = domain_data.get(DATA_WEBHOOK) if isinstance(domain_data, dict) else None
    if not isinstance(cfg, dict):
        return web.Response(status=503, text="MCP server is not available")

    auth_response = await _check_webhook_auth(request, cfg)
    if auth_response is not None:
        return auth_response

    target_url: str = cfg["target_url"]
    session: aiohttp.ClientSession = cfg["session"]

    body = await request.read()

    forward_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _STRIPPED_REQUEST_HEADERS
    }

    try:
        async with session.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            data=body if body else None,
        ) as upstream_resp:
            content_type = upstream_resp.headers.get("Content-Type", "")

            resp_headers = {
                "Cache-Control": "no-cache, no-transform",
                "Content-Encoding": "identity",
            }
            mcp_session = upstream_resp.headers.get("Mcp-Session-Id")
            if mcp_session:
                resp_headers["Mcp-Session-Id"] = mcp_session

            if "text/event-stream" in content_type:
                # SSE streaming: prevent HA's compression middleware from
                # buffering/breaking the stream (supervisor#6470).
                resp_headers["Content-Type"] = "text/event-stream"
                resp_headers["X-Accel-Buffering"] = "no"
                response = web.StreamResponse(
                    status=upstream_resp.status, headers=resp_headers
                )
                await response.prepare(request)
                # Once prepare() has sent the 200 + headers, a mid-stream
                # upstream failure can no longer become a 502 — returning a
                # fresh Response here would be silently dropped and the client
                # would see only a truncated stream with no log trail. End the
                # prepared stream deterministically and log instead.
                # Count forwarded bytes manually: StreamResponse.body_length
                # is only assigned in write_eof(), so it is still 0 here.
                bytes_forwarded = 0
                try:
                    async for chunk in upstream_resp.content.iter_any():
                        await response.write(chunk)
                        bytes_forwarded += len(chunk)
                except aiohttp.ClientError as err:
                    _LOGGER.error(
                        "MCP webhook: upstream dropped mid-stream after %d bytes: %s",
                        bytes_forwarded,
                        err,
                    )
                with suppress(ConnectionResetError):
                    await response.write_eof()
                return response

            if not any(ct in content_type for ct in _ALLOWED_CONTENT_TYPES):
                content_type = "application/json"
            resp_headers["Content-Type"] = content_type
            resp_body = await upstream_resp.read()
            return web.Response(
                status=upstream_resp.status, body=resp_body, headers=resp_headers
            )
    except aiohttp.ClientError as err:
        _LOGGER.error("MCP webhook: upstream request failed: %s", err)
        return web.Response(status=502, text="MCP server unavailable")
    except Exception as err:
        _LOGGER.exception("MCP webhook: unexpected error: %s", err)
        return web.Response(status=500, text="MCP server internal error")


# ---------------------------------------------------------------------------
# Registration / teardown
# ---------------------------------------------------------------------------


async def async_register_webhook(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    port: int,
    secret_path: str,
    auth_mode: str,
    register_endpoint: bool = True,
    oauth_client_id: str | None = None,
    oauth_client_secret: str | None = None,
    oauth_signing_key: str | None = None,
) -> bool:
    """Register the ingress webhook (and, for ha_auth/legacy, the OAuth surface).

    Stores the forwarding config in ``hass.data[DOMAIN][DATA_WEBHOOK]`` and opens
    a long-lived aiohttp session for streaming. Raises on failure with the webhook
    already unregistered, so the caller never leaves a half-configured endpoint
    live. ``webhook`` is a manifest dependency, so HA guarantees it is set up
    before this runs. ``oauth_client_id``/``oauth_client_secret``/
    ``oauth_signing_key`` are required when ``auth_mode == WEBHOOK_AUTH_LEGACY``
    (ignored otherwise); ``oauth_signing_key`` is the hex string persisted in
    ``entry.data`` — see ``oauth_legacy._normalize_signing_key``.

    With ``register_endpoint=False`` (remote webhook access disabled by option)
    no public endpoint or ha_auth/legacy surface is created — and any leftover
    endpoint from a crashed unload is cleared, so off means off; only the
    forwarding config is stored, which same-host consumers — the sidebar
    settings panel proxy — need to reach the loopback server (#1803).

    Returns True when the caller should surface ``ISSUE_LEGACY_OAUTH_RESTART``:
    the root ``/authorize``/``/token`` views just bound for the first time this
    HA session (or with changed credentials), or they are still bound from a
    prior legacy registration that this call has moved away from — either way
    aiohttp cannot bind or release a view without a full HA restart.
    """
    if auth_mode not in (WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY):
        # Fail CLOSED on an unknown mode (corrupt/migrated options): refusing
        # bring-up files a repair issue, instead of an unrecognized string
        # silently taking the unauthenticated forward path.
        raise ValueError(f"Unknown webhook auth mode: {auth_mode!r}")

    webhook_id: str = entry.data[DATA_WEBHOOK_ID]
    # Reload-safe and off-means-off: clear any leftover registration from a
    # crashed unload before (re)registering — or before storing a local-only
    # config (async_unregister is a no-op pop when nothing is registered).
    # Runs before the session opens so a raise here cannot leak it.
    async_unregister(hass, webhook_id)
    target_url = f"http://127.0.0.1:{port}{secret_path}"
    session = aiohttp.ClientSession(timeout=_CLIENT_TIMEOUT)

    cfg: dict[str, Any] = {
        "webhook_id": webhook_id,
        "target_url": target_url,
        "session": session,
        "auth_mode": auth_mode,
        "resource_server": None,
        "oauth_provider": None,
        CFG_AUTOAPPROVE_PROVIDER: None,
    }

    oauth_restart_needed = False
    if register_endpoint:
        try:
            async_register(
                hass,
                DOMAIN,
                _WEBHOOK_NAME,
                webhook_id,
                _async_handle_webhook,
                allowed_methods=["POST", "GET"],
            )
            if auth_mode == WEBHOOK_AUTH_HA:
                provider = ResourceServer(hass, webhook_id)
                _register_metadata_views(hass)
                cfg["resource_server"] = provider
            elif auth_mode == WEBHOOK_AUTH_LEGACY:
                if not (oauth_client_id and oauth_client_secret and oauth_signing_key):
                    raise ValueError(
                        "legacy webhook auth mode requires oauth_client_id, "
                        "oauth_client_secret, and oauth_signing_key"
                    )
                _register_metadata_views(hass)
                try:
                    oauth_provider, oauth_restart_needed = bind_legacy_views(
                        hass, oauth_client_id, oauth_client_secret, oauth_signing_key
                    )
                except LegacyOAuthRouteConflict as err:
                    raise ValueError(
                        "The Webhook Proxy add-on (or its dev flavor) already "
                        f"owns the root /authorize and /token routes ({err}). "
                        "Stop that add-on and restart Home Assistant, then "
                        "enable legacy mode again."
                    ) from err
                cfg["oauth_provider"] = oauth_provider
            else:
                # WEBHOOK_AUTH_NONE (the only remaining mode — unknown modes
                # already raised above). The secret webhook URL is the
                # credential, but we still serve our own corrected discovery +
                # an invisible auto-approve authorization server so claude.ai's
                # intermittent OAuth discovery resolves against us instead of HA
                # core's broken origin-root document, and completes with no HA
                # login (issue #1969). Both view bundles bind at most once per
                # HA session; the per-request resolvers gate them on this cfg,
                # so a none<->ha_auth switch needs no restart.
                #
                # Fails OPEN, unlike ha_auth/legacy (issue #1978): none mode is
                # intentionally unauthenticated, and this discovery is an
                # enhancement layered on a webhook that (already registered
                # above) otherwise always forwards. A failure here must NOT fall
                # through to the outer teardown and take down a webhook the user
                # configured to need no auth — it only means claude.ai's rare
                # OAuth-discovery fallback goes unassisted. Mirrors the add-on's
                # _setup_none_autoapprove. Provider is assigned last, so a
                # partial bind leaves none-autoapprove inactive (plain proxy)
                # rather than half-enabled.
                try:
                    _register_metadata_views(hass)
                    bind_autoapprove_views(hass)
                    cfg[CFG_AUTOAPPROVE_PROVIDER] = AutoApproveProvider()
                except Exception:
                    _LOGGER.exception(
                        "MCP webhook: failed to set up none-mode auto-approve "
                        "discovery; continuing as a plain unauthenticated proxy "
                        "(the webhook still forwards — only claude.ai's rare "
                        "OAuth-discovery fallback is unassisted)."
                    )
        except Exception:
            # Never leave a live endpoint (or a leaked session) behind a failed
            # auth-setup path. suppress: the ORIGINAL error must be what
            # propagates (review finding) - a raising cleanup would mask it.
            with suppress(Exception):
                async_unregister(hass, webhook_id)
            with suppress(Exception):
                await session.close()
            raise

    # A PRIOR registration this HA session may still own the legacy root views
    # even though THIS call bound no legacy provider — either the mode is no
    # longer legacy, OR legacy is still selected but the webhook endpoint is now
    # off (register_endpoint=False skips the bind block above). aiohttp can never
    # release a bound view without a restart, so gate on "no provider bound this
    # call" (not the mode string) to surface the restart that releases route
    # ownership in both cases.
    if cfg["oauth_provider"] is None and hass.data.get(OAUTH_ROUTE_OWNER_KEY) == DOMAIN:
        oauth_restart_needed = True

    hass.data.setdefault(DOMAIN, {})[DATA_WEBHOOK] = cfg
    return oauth_restart_needed


async def async_unregister_webhook(hass: HomeAssistant) -> None:
    """Unregister the ingress webhook and close its aiohttp session.

    Idempotent. The discovery views and the legacy root ``/authorize``/``/token``
    views are intentionally left bound (aiohttp can't unregister them until HA
    restarts); they 404 while their mode is not live (see ``active_auth_mode``
    / ``LegacyOAuthProvider.is_active``).
    """
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return
    cfg = domain_data.pop(DATA_WEBHOOK, None)
    if not isinstance(cfg, dict):
        return
    webhook_id = cfg.get("webhook_id")
    if webhook_id:
        async_unregister(hass, webhook_id)
    session = cfg.get("session")
    if session is not None:
        await session.close()
