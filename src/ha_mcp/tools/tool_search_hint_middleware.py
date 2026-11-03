"""FastMCP middleware: actionable error for stale tool-search proxy/search calls.

On a connection using the full catalog (see ``ha_mcp.tool_catalog``), the
synthetic tool-search tools — ``ha_search_tools`` and the
``ha_call_{read,write,delete}_tool`` proxies — do not resolve. If a
client calls one of them anyway, FastMCP raises a bare ``NotFoundError``
("Unknown tool: 'ha_search_tools'") with no recovery guidance.

That is exactly what happens when an MCP client that caches its tool list is
still advertising one captured from a previous session when Tool Search was on:
the client shows ``ha_search_tools`` as available, calls it, and the live
server — now in Tool-Search-off mode — rejects it. Restarting the add-on or
Home Assistant does not help because the stale list lives in the client, not
the server.

This middleware intercepts that one case and replaces the opaque "Unknown tool"
with a structured error that tells the user their tool list is stale and to
reconnect/refresh the MCP server. It only acts on a *top-level* resolution miss
for one of those four names while Tool Search is off; in every other case —
including a ``NotFoundError`` bubbling up from inside an executing tool — the
original error propagates unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import NotFoundError
from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)

from ..errors import ErrorCode, create_error_response
from ..policy.middleware import PROXY_META_TOOLS
from ..tool_catalog import tool_search_active
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)


class ToolSearchHintMiddleware(Middleware):
    """Turn the opaque "Unknown tool" for tool-search synthetic tools into a
    stale-tool-list hint when Tool Search is disabled."""

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        try:
            return await call_next(context)
        except NotFoundError as exc:
            name = context.message.name
            # Only rewrite a TOP-LEVEL "Unknown tool" miss for one of the four
            # tool-search synthetic names while Tool Search is off:
            #  - match the dispatch-layer message so a NotFoundError bubbling up
            #    from INSIDE an executing tool (a different name) is never
            #    mislabeled as a stale cache; on any wording change this simply
            #    fails closed and re-raises the original error.
            #  - require Tool Search to be off (when on, the names resolve, so a
            #    miss would be a different, real problem we must not mask).
            top_level_miss = str(exc) == f"Unknown tool: {name!r}"
            if top_level_miss and name in PROXY_META_TOOLS and not tool_search_active():
                logger.info(
                    "Stale tool-search call for %r on a full-catalog connection "
                    "- returning refresh-your-tool-list hint",
                    name,
                )
                raise_tool_error(
                    create_error_response(
                        code=ErrorCode.RESOURCE_NOT_FOUND,
                        message=(
                            f"'{name}' only exists when Tool Search is enabled, and "
                            "Tool Search is currently OFF for this connection. Your "
                            "MCP client is showing a cached tool list from when Tool "
                            "Search was on. After changing any ha-mcp setting (Tool "
                            "Search, pinned/disabled tools, etc.) the client must "
                            "reconnect or refresh the MCP server to re-fetch the "
                            "current tools — restarting the add-on or Home Assistant "
                            "does not refresh the client's cached list. With Tool "
                            f"Search off, every tool is available directly by name, "
                            f"so {name} is not needed."
                        ),
                        suggestions=[
                            (
                                "Reconnect or refresh the ha-mcp MCP server in your "
                                "client to reload the current tool list."
                            ),
                            (
                                "Then call the tool you need directly by its name "
                                "(with Tool Search off, ha_search_tools and the "
                                "ha_call_* proxies do not exist)."
                            ),
                        ],
                        context={"tool_name": name, "enable_tool_search": False},
                    )
                )
            raise
