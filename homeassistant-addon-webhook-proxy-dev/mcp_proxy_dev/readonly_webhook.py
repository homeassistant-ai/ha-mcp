"""Shared read-only webhook route for independently installed HA-MCP integrations.

Keep this file identical in the embedded component and Webhook Proxy app.
The shared registry lets both installations use one HA HTTP route; callbacks
retain their existing authentication and forwarding behavior.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

_REGISTRY = "ha_mcp_readonly_webhooks"


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


def register_readonly_webhook(
    hass: HomeAssistant,
    webhook_id: str,
    handler: Callable[..., Awaitable[web.StreamResponse]],
) -> None:
    """Bind the common route once, and activate this webhook's alias."""
    if _REGISTRY not in hass.data:
        hass.http.register_view(ReadOnlyWebhookView(hass))
        hass.data[_REGISTRY] = {}
    hass.data[_REGISTRY][webhook_id] = handler


def unregister_readonly_webhook(hass: HomeAssistant, webhook_id: str) -> None:
    """Leave the bound route inert for removed webhooks, including failed setup."""
    hass.data.get(_REGISTRY, {}).pop(webhook_id, None)


def readonly_url(url: str) -> str:
    """Append to the endpoint path, preserving any configured query string."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/readonly"))
