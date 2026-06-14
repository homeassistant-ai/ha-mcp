"""FastMCP middleware: actionable error for stale tool-search proxy/search calls.

When ``enable_tool_search`` is off, the synthetic tool-search tools —
``ha_search_tools`` and the ``ha_call_{read,write,delete}_tool`` proxies — are
never registered (the ``CategorizedSearchTransform`` is not installed). If a
client calls one of them anyway, FastMCP raises a bare ``NotFoundError``
("Unknown tool: 'ha_search_tools'") with no recovery guidance.

That is exactly what happens when an MCP client (e.g. a ChatGPT connector) is
still advertising a *cached* tool list captured from a previous session when
Tool Search was on: the client shows ``ha_search_tools`` as available, calls
it, and the live server — now in Tool-Search-off mode — rejects it. Restarting
the add-on or Home Assistant does not help because the stale list lives in the
client, not the server.

This middleware intercepts that one case and replaces the opaque "Unknown tool"
with a structured error that tells the user their tool list is stale and to
reconnect/refresh the MCP server. It only acts when Tool Search is off (the
only state in which those four names fail to resolve); in every other case the
original ``NotFoundError`` propagates unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.exceptions import NotFoundError
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from ..policy.middleware import PROXY_META_TOOLS
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
        except NotFoundError:
            name = context.message.name
            # Only the four tool-search synthetic tools are affected, and only
            # when Tool Search is off (when it is on they resolve normally, so
            # a NotFoundError here would be a different, real problem we must
            # not mask).
            if (
                name in PROXY_META_TOOLS
                and not get_global_settings().enable_tool_search
            ):
                logger.info(
                    "Stale tool-search call for %r while enable_tool_search is off "
                    "- returning refresh-your-tool-list hint",
                    name,
                )
                raise_tool_error(
                    create_error_response(
                        code=ErrorCode.RESOURCE_NOT_FOUND,
                        message=(
                            f"'{name}' only exists when Tool Search is enabled, and "
                            "Tool Search is currently OFF on this ha-mcp server. Your "
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
                            "Reconnect or refresh the ha-mcp MCP server in your client "
                            "to reload the current tool list.",
                            "Then call the tool you need directly by its name (with "
                            "Tool Search off, ha_search_tools and the ha_call_* "
                            "proxies do not exist).",
                        ],
                        context={"tool_name": name, "enable_tool_search": False},
                    )
                )
            raise
