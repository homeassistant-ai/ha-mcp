"""Friendly browser landing page for the MCP endpoint (issue #777).

A browser (or any GET client) that hits the POST-only Streamable HTTP MCP
endpoint would otherwise see a bare "Method Not Allowed". This module registers
a GET handler that answers ``405`` with a human-readable explanation plus the
correct ``Allow`` header, so automated clients still get the right HTTP
semantics.

Extracted from :mod:`ha_mcp.__main__` so the in-process server (the
``ha_mcp_tools`` custom-component worker thread) can register the same landing
page. Importing ``ha_mcp.__main__`` runs process-global side effects at module
load (``truststore.inject_into_ssl()``) that must never happen in-process, so
the reusable core lives here — free of those side effects. ``__main__`` keeps a
thin wrapper that additionally tidies the uvicorn access log.
"""

from __future__ import annotations

import logging
import weakref
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import PlainTextResponse

logger = logging.getLogger(__name__)


class CustomRouteServer(Protocol):
    """Structural type for servers exposing FastMCP's ``custom_route`` decorator.

    Matches the real FastMCP server and ``__main__``'s deferred proxy. Only the
    ``(path, methods)`` call shape used here is pinned; the decorator's return
    type is left to the implementation (fastmcp also takes optional ``name`` /
    ``include_in_schema`` parameters this module never passes).
    """

    def custom_route(self, path: str, methods: list[str]) -> Any: ...


# The landing body. Plain text so no browser ever interprets it as markup, and
# so it survives the ha_mcp_tools webhook proxy (which forwards text/plain but
# coerces text/html to JSON as an XSS guard).
LANDING_MESSAGE = (
    "HA-MCP server is up and running!\n"
    "\n"
    "To connect, paste the full URL (including the /private_... key) into the\n"
    "connector or MCP settings of your AI/LLM client. No username or password required.\n"
    "Setup instructions: https://homeassistant-ai.github.io/ha-mcp/\n"
    "\n"
    "--- Seeing this page? Your URL is set up correctly ---\n"
    "\n"
    "If this page loads in your browser, the MCP server is reachable and the\n"
    "URL is correct. If your AI client still cannot connect, the problem is\n"
    "not on HA-MCP's side. Common causes:\n"
    "\n"
    "- Geo / country blocking in your reverse proxy / CDN (Cloudflare, NGINX,\n"
    "  Traefik, Zoraxy, etc.). Most AI/LLM services connect from US-based\n"
    "  cloud infrastructure, so if you block US IP addresses (or only allow\n"
    "  your own country), that is why your client cannot connect. Allow your\n"
    "  provider's IP ranges (or your client's egress IPs). For example,\n"
    # Anthropic's documented outbound range; re-verify at
    # https://platform.claude.com/docs/en/api/ip-addresses if it ever changes.
    "  Claude.ai connects from Anthropic's network, 160.79.104.0/21.\n"
    "- WAF, bot-blocking, or rate-limiting rules on the proxy that drop or\n"
    "  challenge the request.\n"
    "- The AI client's network can sometimes be spotty -- you may just need\n"
    "  to try connecting again.\n"
    "- The AI client itself refusing certain domains or proxy providers on\n"
    "  its end. This is rare and outside your control; try a different\n"
    "  hostname or proxy if you suspect it.\n"
    "\n"
    "Your proxy's access logs will show the blocked attempt -- look for the\n"
    "request from your AI provider's IP (e.g. an Anthropic 160.79.x.x address).\n"
    "\n"
    "--- Cloudflare Users ---\n"
    "\n"
    'If your LLM cannot connect, Cloudflare\'s "Block AI training bots"\n'
    "setting is the most common cause. To disable it:\n"
    "\n"
    "1. Log in to Cloudflare (https://dash.cloudflare.com)\n"
    "2. In the left sidebar, click Domains, then click Overview\n"
    "3. Click on the domain you use for connecting to Home Assistant\n"
    '4. On the right side, find "Control AI Crawlers"\n'
    '5. Under "Block AI training bots", open the dropdown\n'
    '6. Select "do not block (allow crawlers)"\n'
    "\n"
    "Screenshot of the setting:\n"
    "https://homeassistant-ai.github.io/ha-mcp/images/cloudflare-ai-crawlers-setting.jpg\n"
)

# Landing routes already registered, keyed PER MCP INSTANCE (weakly, so a
# discarded server never leaks). The key must not be the path alone: the
# in-process server builds a NEW FastMCP on every config-entry reload in the
# SAME Python process while keeping the same secret path — a process-global
# path set would skip the re-registration and silently drop the landing page
# after the first reload. The CLI/add-on never sees this (their process exits).
_registered_landing_paths: weakref.WeakKeyDictionary[CustomRouteServer, set[str]] = (
    weakref.WeakKeyDictionary()
)


def register_browser_landing(mcp_instance: CustomRouteServer, path: str) -> bool:
    """Register a GET handler that returns 405 with the friendly landing message.

    ``mcp_instance`` is a FastMCP-like object exposing ``custom_route`` (the real
    server or ``__main__``'s deferred proxy). Returns True if the route was newly
    registered, False if this instance already had one for ``path`` (idempotent
    per instance + path).
    """
    paths = _registered_landing_paths.setdefault(mcp_instance, set())
    if path in paths:
        logger.warning(
            "register_browser_landing: %r already registered, skipping", path
        )
        return False
    paths.add(path)

    # Safe because FastMCP registers the MCP route with methods=["POST", "DELETE"]
    # in stateless mode, so Starlette rejects GET requests before the MCP handler
    # runs. Custom routes are registered at lowest precedence (after the MCP route).
    @mcp_instance.custom_route(path, methods=["GET"])
    async def _browser_landing(_: Request) -> PlainTextResponse:
        # Any GET here is a non-MCP caller (browser, health check, proxy, or a
        # connector's SSE-style pre-flight) hitting this POST-only Streamable HTTP
        # endpoint, which answers 405 by design. Log one annotated line so the 405
        # reads as expected.
        logger.info("GET %s -> 405 (NORMAL for most non-SSE connections)", path)
        return PlainTextResponse(
            LANDING_MESSAGE,
            status_code=405,
            # DELETE is included per the MCP Streamable HTTP spec (used for
            # session termination), even though this deployment uses stateless mode.
            headers={"Allow": "POST, DELETE"},
        )

    return True
