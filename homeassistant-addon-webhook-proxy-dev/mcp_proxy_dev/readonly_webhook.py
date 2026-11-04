"""Shared endpoint-suffix webhook routes for independently installed HA-MCP integrations.

Keep this file identical in the embedded component and Webhook Proxy app.
The shared registries let both installations use one HA HTTP route per
suffix; callbacks retain their existing authentication and forwarding
behavior. ``/readonly`` restricts the connection to reads; ``/catalog/full``
and ``/catalog/search`` fix its tool catalog, optionally after ``/readonly``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

# Older installations register only the read-only route and call handlers
# with ``read_only=True``; keep that registry and calling convention.
_REGISTRY = "ha_mcp_readonly_webhooks"
_CATALOG_REGISTRY = "ha_mcp_catalog_webhooks"

CATALOG_MODES = ("full", "search")
ENDPOINT_SUFFIXES = (
    "/readonly",
    *(
        f"{prefix}/catalog/{mode}"
        for prefix in ("", "/readonly")
        for mode in CATALOG_MODES
    ),
)


class ReadOnlyWebhookView(HomeAssistantView):
    """Dispatch only to currently registered MCP webhooks."""

    url = "/api/webhook/{webhook_id}/readonly"
    name = "ha_mcp:readonly-webhook"
    requires_auth = False
    cors_allowed = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request, webhook_id: str) -> web.StreamResponse:
        handler: Callable[..., Awaitable[web.StreamResponse]] | None = self.hass.data[
            _REGISTRY
        ].get(webhook_id)
        if handler is None:
            return web.Response(status=404)
        return await handler(self.hass, webhook_id, request, read_only=True)

    get = post


class CatalogWebhookView(HomeAssistantView):
    """Dispatch ``[/readonly]/catalog/<mode>`` to registered MCP webhooks."""

    url = "/api/webhook/{webhook_id}/catalog/{mode}"
    name = "ha_mcp:catalog-webhook"
    requires_auth = False
    cors_allowed = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.extra_urls = ["/api/webhook/{webhook_id}/readonly/catalog/{mode}"]

    async def post(
        self, request: web.Request, webhook_id: str, mode: str
    ) -> web.StreamResponse:
        handler: Callable[..., Awaitable[web.StreamResponse]] | None = self.hass.data[
            _CATALOG_REGISTRY
        ].get(webhook_id)
        suffix = endpoint_suffix(request.path, f"/api/webhook/{webhook_id}")
        if handler is None or mode not in CATALOG_MODES or not suffix:
            return web.Response(status=404)
        return await handler(self.hass, webhook_id, request, suffix=suffix)

    get = post


def register_readonly_webhook(
    hass: HomeAssistant,
    webhook_id: str,
    handler: Callable[..., Awaitable[web.StreamResponse]],
) -> None:
    """Bind the common routes once, and activate this webhook's aliases."""
    for registry, view in (
        (_REGISTRY, ReadOnlyWebhookView),
        (_CATALOG_REGISTRY, CatalogWebhookView),
    ):
        if registry not in hass.data:
            hass.http.register_view(view(hass))
            hass.data[registry] = {}
        hass.data[registry][webhook_id] = handler


def unregister_readonly_webhook(hass: HomeAssistant, webhook_id: str) -> None:
    """Leave the bound routes inert for removed webhooks, including failed setup."""
    for registry in (_REGISTRY, _CATALOG_REGISTRY):
        hass.data.get(registry, {}).pop(webhook_id, None)


def endpoint_suffix(path: str, base: str) -> str:
    """The endpoint suffix ``path`` adds to ``base``, or ``""`` for none."""
    suffix = path.removeprefix(base).rstrip("/") if path.startswith(base) else ""
    return suffix if suffix in ENDPOINT_SUFFIXES else ""


def suffixed_url(url: str, suffix: str) -> str:
    """Append ``suffix`` to the endpoint path, preserving any query string."""
    if not suffix:
        return url  # a configured trailing slash may be significant upstream
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + suffix))


def forward_url(url: str, suffix: str = "", *, read_only: bool = False) -> str:
    """The upstream URL for a webhook request on ``suffix``.

    The ``/readonly`` route calls handlers with ``read_only=True`` rather than a
    suffix. Read-only always wins, so a caller passing both cannot drop it.
    """
    if read_only and not suffix.startswith("/readonly"):
        suffix = "/readonly" + suffix
    return suffixed_url(url, suffix)
