"""FastMCP middleware: actionable error for stale tool-search proxy/search calls.

On a connection serving the full catalog (see ``ha_mcp.tool_catalog``), the
synthetic tool-search tools — ``ha_search_tools`` and the
``ha_call_{read,write,delete}_tool`` proxies — do not resolve. If a
client calls one of them anyway, FastMCP raises a bare ``NotFoundError``
("Unknown tool: 'ha_search_tools'") with no recovery guidance.

Two things cause that call: the client caches a tool list it fetched from a
search-catalog connection, or the agent learned the names from another
ha-mcp server it is connected to (for example a ``/catalog/search`` URL
alongside a ``/catalog/full`` one). Restarting the app (add-on) or Home
Assistant fixes neither: the stale list lives in the client.

This middleware intercepts that one case and replaces the opaque "Unknown tool"
with a structured error naming the connection's catalog and both causes. It
only acts on a *top-level* resolution miss for one of those four names on a
full-catalog connection; in every other case — including a ``NotFoundError``
bubbling up from inside an executing tool — the original error propagates
unchanged.
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
    stale-tool-list hint on a full-catalog connection."""

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        try:
            return await call_next(context)
        except NotFoundError as exc:
            name = context.message.name
            # Only rewrite a TOP-LEVEL "Unknown tool" miss for one of the four
            # tool-search synthetic names on a full-catalog connection:
            #  - match the dispatch-layer message so a NotFoundError bubbling up
            #    from INSIDE an executing tool (a different name) is never
            #    mislabeled as a stale cache; on any wording change this simply
            #    fails closed and re-raises the original error.
            #  - require the full catalog (on a search connection the names
            #    resolve, so a miss is a different, real problem to not mask).
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
                            f"'{name}' exists only on a connection serving the "
                            "search catalog, and this connection serves the full "
                            "catalog, so every tool is available directly by name. "
                            "Either your MCP client is showing a cached tool list "
                            "from a search-catalog connection, or the name comes "
                            "from another ha-mcp server you are connected to (such "
                            "as a /catalog/search URL). Restarting the app "
                            "(add-on) or Home Assistant does not refresh the "
                            "client's cached list."
                        ),
                        suggestions=[
                            (
                                "Call the tool you need directly by its name on "
                                "this connection."
                            ),
                            (
                                "If the client lists stale tools, reconnect or "
                                "refresh this MCP server to reload its tool list."
                            ),
                        ],
                        context={"tool_name": name, "catalog": "full"},
                    )
                )
            raise
