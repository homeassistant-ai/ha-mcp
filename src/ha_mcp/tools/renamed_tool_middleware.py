"""FastMCP middleware: keep retired tool names callable after a rename.

Home Assistant 2026.2 renamed add-ons to apps, and this server's two add-on
tools moved with it (``ha_get_addon`` -> ``ha_get_app``, ``ha_manage_addon``
-> ``ha_manage_app``). Renaming a tool is not a breaking change here — clients
resolve tools by name at runtime and re-list after a server update — but a
client that has not re-listed yet would get a bare "Unknown tool" until it
does.

This middleware closes that window without paying for it in the catalog. It
runs before FastMCP resolves the name (``call_tool`` builds the middleware
context first and its ``call_next`` re-reads ``context.message.name``), so
rewriting the name here dispatches the call to the current tool. The old names
are never registered, so they stay out of ``tools/list`` and cost no tokens.

Registering the old name as a second, disabled tool would not work:
``get_tool`` filters on ``is_enabled`` and ``call_tool`` then raises
``NotFoundError``, so a disabled tool is unlisted *and* uncallable.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

from ..renamed_tools import RENAMED_TOOLS, adapt_retired_arguments

logger = logging.getLogger(__name__)

__all__ = ["RENAMED_TOOLS", "RenamedToolAliasMiddleware"]


class RenamedToolAliasMiddleware(Middleware):
    """Dispatch a call on a retired tool name to its current name.

    Installed first in the chain so every later middleware — read-only
    exemptions, policy gating, redaction — sees the current name it is keyed
    on, rather than an alias it would not recognise.
    """

    def __init__(self) -> None:
        # Which retired names have been logged at INFO already, so the first
        # call on each is audible and the rest stay at DEBUG. An operator
        # otherwise has no way to know a client is still calling the old name —
        # the one observation that could ever justify retiring this alias —
        # while a per-call INFO line would be noise for a tool called in a
        # loop. Log de-duplication only: no dispatch decision reads it, and a
        # restart simply announces each name once more.
        self._announced: set[str] = set()

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        retired = context.message.name
        current = RENAMED_TOOLS.get(retired)
        if current is None:
            return await call_next(context)

        if retired in self._announced:
            logger.debug(
                "Tool %s was renamed to %s; dispatching the call to the current name",
                retired,
                current,
            )
        else:
            self._announced.add(retired)
            logger.info(
                "A client called %s, which was renamed to %s; dispatching to the "
                "current name. Old names stay callable, but a client that re-lists "
                "its tools after a server update will not need this.",
                retired,
                current,
            )
        # A consolidated tool needs the ``action`` the retired signature
        # never carried; a plain rename forwards the arguments as they are.
        arguments = adapt_retired_arguments(retired, context.message.arguments)
        return await call_next(
            context.copy(
                message=context.message.model_copy(
                    update={"name": current, "arguments": arguments}
                )
            )
        )
