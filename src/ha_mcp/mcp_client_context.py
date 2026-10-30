"""What the connected MCP client said about itself on the current request.

Handshake-era clients (through 2025-11-25) send their client info and
capabilities once, in ``initialize``. On 2026-07-28 there is no handshake:
every request carries its protocol version, client info and capabilities in
``_meta``, and the SDK exposes them on that request's session. Either way the
values are read per request, so they describe the request being served.
"""

from __future__ import annotations

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.server.dependencies import get_http_headers
from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from ha_mcp._vendor.mcp_types import ClientCapabilities

logger = logging.getLogger(__name__)


def protocol_version(ctx: Any) -> str | None:
    """The MCP protocol version this request was served on."""
    version = getattr(getattr(ctx, "request_context", None), "protocol_version", None)
    return version if isinstance(version, str) else None


def client_capabilities(ctx: Any) -> ClientCapabilities | None:
    """The capabilities the client declared for this request, if any."""
    session = getattr(getattr(ctx, "request_context", None), "session", None)
    caps = getattr(session, "client_capabilities", None)
    return caps if isinstance(caps, ClientCapabilities) else None


def declares_form_elicitation(caps: ClientCapabilities | None) -> bool:
    """True when the client can show a form-mode elicitation to its user.

    An empty ``elicitation`` object predates the form/url split and means
    form mode only.
    """
    elicitation = caps.elicitation if caps is not None else None
    if elicitation is None:
        return False
    return elicitation.form is not None or elicitation.url is None


def capability_names(caps: ClientCapabilities | None) -> list[str] | None:
    """Flatten declared capabilities into stable, readable names.

    ``None`` means the request carried none to read, which is not the same as
    declaring none: a handshake-era client over stateless HTTP sends them only
    in ``initialize``, never on a later call.
    """
    if caps is None:
        return None
    names: list[str] = []
    if caps.elicitation is not None:
        if declares_form_elicitation(caps):
            names.append("elicitation.form")
        if caps.elicitation.url is not None:
            names.append("elicitation.url")
    names.extend(
        field
        for field in ("sampling", "roots", "tasks")
        if getattr(caps, field) is not None
    )
    names.extend(f"extension:{key}" for key in sorted(caps.extensions or {}))
    names.extend(f"experimental:{key}" for key in sorted(caps.experimental or {}))
    return names


def extract_client_info(ctx: Any) -> dict[str, str]:
    """Pull the connecting MCP client's self-identification off the request context.

    The MCP ``initialize`` handshake carries a ``clientInfo`` Implementation
    object (``name``/``version``/optional ``title``). FastMCP exposes the
    underlying server session as ``ctx.session``; the MCP SDK's
    ``ServerSession`` keeps the parsed initialize params on ``client_params``;
    on the 2026-07-28 protocol the SDK synthesizes them from the request's
    ``_meta`` when it carries both client info and capabilities. An
    older-protocol client over stateless HTTP sends client info only in
    ``initialize``, so the request's ``User-Agent`` is the fallback there,
    marked as such in ``title``.

    Returns ``{"name": ..., "version": ..., "title": ...}``. ``name`` and
    ``version`` fall back to ``"unknown"`` when the client didn't send them;
    ``title`` falls back to the empty string so callers can distinguish "not
    sent" from a real title without false-positive aside rendering.

    Returns an empty dict when there is no context, or no client info and no
    ``User-Agent`` (e.g. stdio), so its callers (bug report, overview, debug
    log) never fail on it. The log level is intentionally INFO, not DEBUG:
    this catch is the only signal we'd get if FastMCP/MCP SDK shape drifts in
    a future release, and silent drift would hide a regression for months.
    """
    if ctx is None:
        return {}
    try:
        params = getattr(getattr(ctx, "session", None), "client_params", None)
        client = getattr(params, "client_info", None) or getattr(
            params, "clientInfo", None
        )
        if client is None:
            return _client_info_from_user_agent()
        return {
            "name": getattr(client, "name", None) or "unknown",
            "version": getattr(client, "version", None) or "unknown",
            "title": getattr(client, "title", None) or "",
        }
    except Exception as e:  # noqa: BLE001
        logger.info(
            "Failed to read MCP client info from context: %s (%s)",
            e,
            type(e).__name__,
        )
        return {}


def _client_info_from_user_agent() -> dict[str, str]:
    """``{"name", "version", "title"}`` from the HTTP ``User-Agent``, or ``{}``."""
    user_agent = get_http_headers(include={"user-agent"}).get("user-agent", "").strip()
    if not user_agent:
        return {}
    product = user_agent.split()[0]
    name, _, version = product.partition("/")
    logger.debug("No MCP client info on the request; using User-Agent %r", product)
    return {
        "name": name or "unknown",
        "version": version or "unknown",
        "title": "from HTTP User-Agent",
    }


def describe_mcp_client(ctx: Any) -> dict[str, Any]:
    """Protocol version, client info and declared capabilities of this request."""
    return {
        "protocol_version": protocol_version(ctx) or "unknown",
        "client_info": extract_client_info(ctx),
        "capabilities": capability_names(client_capabilities(ctx)),
    }


class McpClientDebugLogMiddleware(Middleware):
    """Log, at DEBUG, which protocol and capabilities each tool call arrived with.

    The modern protocol is stateless, so there is no connection to log once
    per client; the line is per call and costs one level check when DEBUG is
    off.
    """

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "tools/call %s: %s",
                context.message.name,
                describe_mcp_client(context.fastmcp_context),
            )
        return await call_next(context)
