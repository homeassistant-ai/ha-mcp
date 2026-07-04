"""Webhook ingress for the in-process ha-mcp server (issue #1527).

Ported from the proven webhook-proxy add-on (``mcp_proxy``): an HA webhook
(``/api/webhook/<id>``) forwards MCP traffic to the loopback server and streams
the response back, so the server is reachable through Nabu Casa remote UI (or any
reverse proxy) with the webhook id as the shared secret.

Two auth postures, chosen in the options flow:

* ``none`` — the secret webhook URL *is* the credential (matches the add-on's
  default). No bearer is required.
* ``ha_auth`` — Home Assistant core is the OAuth authorization server. This
  module serves the RFC 8414 / RFC 9728 discovery documents (so claude.ai /
  ChatGPT can sign in with the user's HA account) and validates inbound bearer
  tokens via ``hass.auth``. There is no bespoke authorization-server code here —
  every protocol step is HA core's own ``/auth/*``.

The forwarding handler mirrors ``mcp_proxy._handle_webhook`` exactly (hop-by-hop
header stripping, the SSE streaming branch with anti-buffering headers, the
content-type whitelist, ``Mcp-Session-Id`` propagation, and the 502/500 error
mapping); the ``ha_auth`` bearer check + discovery documents mirror the add-on's
``auth_native.py`` + the ``ha_auth`` subset of ``oauth.py``.
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
    WEBHOOK_AUTH_NONE,
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

# Content-Types an MCP response may carry; anything else is coerced to JSON to
# prevent HTML injection / XSS through the proxy.
_ALLOWED_CONTENT_TYPES = ("application/json", "text/event-stream")

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
    cloud URL AND any other external URL. Honors ``X-Forwarded-Proto/Host`` set
    by HA's trusted-proxy layer, falling back to the request scheme/host.
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

    def resource_url(self, base_url: str) -> str:
        """Absolute URL of the protected webhook resource under ``base_url``."""
        return f"{base_url}/api/webhook/{self._webhook_id}"

    def authorization_server_url(self, base_url: str) -> str:
        """Issuer / authorization-server URL under ``base_url``."""
        return f"{base_url}{OAUTH_BASE}"

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
        return result is not None


# ---------------------------------------------------------------------------
# RFC 8414 / RFC 9728 discovery views (ha_auth mode only)
# ---------------------------------------------------------------------------


def _active_resource_server(hass: HomeAssistant) -> ResourceServer | None:
    """Return the CURRENT entry's ha_auth resource server, or None.

    The discovery views resolve this per request instead of binding a provider
    at registration time: aiohttp can't drop a bound view until HA restarts, so
    a remove + re-add of the config entry (which mints a NEW webhook id in the
    same HA session) would otherwise leave the views advertising the old id.
    Returns None when no entry is live or the webhook auth mode is not ha_auth
    — the views then 404 like an unregistered route.
    """
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return None
    cfg = domain_data.get(DATA_WEBHOOK)
    if not isinstance(cfg, dict) or cfg.get("auth_mode") != WEBHOOK_AUTH_HA:
        return None
    provider = cfg.get("resource_server")
    return provider if isinstance(provider, ResourceServer) else None


def _json_not_found() -> web.Response:
    """404 JSON body used by stale-but-bound discovery views."""
    return web.json_response({"error": "not_found"}, status=404)


def _protected_resource_document(provider: ResourceServer, base: str) -> dict[str, Any]:
    """RFC 9728 protected-resource document for ``provider`` under ``base``."""
    return {
        "resource": provider.resource_url(base),
        "authorization_servers": [provider.authorization_server_url(base)],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/homeassistant-ai/ha-mcp",
    }


class _ProtectedResourceMetadataView(HomeAssistantView):
    """RFC 9728 Protected Resource Metadata."""

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/protected-resource"
    name = "ha_mcp_tools:oauth:protected-resource"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; the provider is resolved per request."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Serve the protected-resource document (or 404 when ha_auth is off)."""
        provider = _active_resource_server(self._hass)
        if provider is None:
            return _json_not_found()
        return web.json_response(
            _protected_resource_document(provider, _build_base_url(request))
        )


class _AuthorizationServerMetadataView(HomeAssistantView):
    """RFC 8414 Authorization Server Metadata (points at HA core's OAuth)."""

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/authorization-server"
    name = "ha_mcp_tools:oauth:authorization-server"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Serve the authorization-server document (or 404 when ha_auth is off)."""
        if _active_resource_server(self._hass) is None:
            return _json_not_found()
        base = _build_base_url(request)
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
        """Bind the view to the HA instance; the provider is resolved per request."""
        self._hass = hass

    async def get(self, request: web.Request, webhook_id: str) -> web.Response:
        """Serve the document only for the CURRENT entry's webhook id."""
        provider = _active_resource_server(self._hass)
        if provider is None or webhook_id != provider.webhook_id:
            return _json_not_found()
        return web.json_response(
            _protected_resource_document(provider, _build_base_url(request))
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
    """Build the seven ha_auth discovery-document views (provider-agnostic)."""
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
    """Register the ha_auth discovery views at most once per HA session.

    aiohttp cannot unregister a bound view, so a reload / re-enable / re-add must
    reuse the already-bound views — they resolve the ACTIVE provider from
    hass.data per request, so a later entry (even with a new webhook id) is
    served correctly. The guard flag lives at a top-level hass.data key that
    survives config-entry teardown.
    """
    if hass.data.get(_OAUTH_VIEWS_REGISTERED_KEY):
        return
    for view in _metadata_views(hass):
        hass.http.register_view(view)
    hass.data[_OAUTH_VIEWS_REGISTERED_KEY] = True


def _build_unauthorized_response(
    request: web.Request, provider: ResourceServer
) -> web.Response:
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


async def _async_handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.StreamResponse:
    """Forward an MCP request to the loopback server and stream the reply back."""
    domain_data = hass.data.get(DOMAIN)
    cfg = domain_data.get(DATA_WEBHOOK) if isinstance(domain_data, dict) else None
    if not isinstance(cfg, dict):
        return web.Response(status=503, text="MCP server is not available")

    # Auth gate. ``none`` = the secret webhook URL is the credential; ``ha_auth``
    # = validate the bearer via HA core, and on failure emit the 401 discovery
    # challenge so the client can start the OAuth flow. Gate on the PROVIDER
    # (constructed only for ha_auth) rather than a string compare, so the
    # coupling "provider present <=> ha_auth" has a single owner and an
    # inconsistent cfg cannot fail open.
    provider = cfg.get("resource_server")
    if provider is not None:
        if not await provider.validate_request(request):
            return _build_unauthorized_response(request, provider)

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
                try:
                    async for chunk in upstream_resp.content.iter_any():
                        await response.write(chunk)
                except aiohttp.ClientError as err:
                    _LOGGER.error(
                        "MCP webhook: upstream dropped mid-stream after %d bytes: %s",
                        response.body_length or 0,
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
) -> None:
    """Register the ingress webhook (and, for ha_auth, the discovery views).

    Stores the forwarding config in ``hass.data[DOMAIN][DATA_WEBHOOK]`` and opens
    a long-lived aiohttp session for streaming. Raises on failure with the webhook
    already unregistered, so the caller never leaves a half-configured endpoint
    live. ``webhook`` is a manifest dependency, so HA guarantees it is set up
    before this runs.
    """
    if auth_mode not in (WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_HA):
        # Fail CLOSED on an unknown mode (corrupt/migrated options): refusing
        # bring-up files a repair issue, instead of an unrecognized string
        # silently taking the unauthenticated forward path.
        raise ValueError(f"Unknown webhook auth mode: {auth_mode!r}")

    webhook_id: str = entry.data[DATA_WEBHOOK_ID]
    target_url = f"http://127.0.0.1:{port}{secret_path}"
    session = aiohttp.ClientSession(timeout=_CLIENT_TIMEOUT)

    cfg: dict[str, Any] = {
        "webhook_id": webhook_id,
        "target_url": target_url,
        "session": session,
        "auth_mode": auth_mode,
        "resource_server": None,
    }

    try:
        # Reload-safe: clear any leftover registration from a crashed unload
        # before (re)registering (async_unregister is a no-op pop).
        async_unregister(hass, webhook_id)
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
    except Exception:
        # Never leave a live endpoint (or a leaked session) behind a failed
        # auth-setup path.
        async_unregister(hass, webhook_id)
        await session.close()
        raise

    hass.data.setdefault(DOMAIN, {})[DATA_WEBHOOK] = cfg


async def async_unregister_webhook(hass: HomeAssistant) -> None:
    """Unregister the ingress webhook and close its aiohttp session.

    Idempotent. The ha_auth discovery views are intentionally left bound (aiohttp
    can't unregister them until HA restarts); they 404 while ha_auth is not live.
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
