"""FastMCP middleware: keep retired tool and parameter names callable.

Home Assistant 2026.2 renamed add-ons to apps, and this server's two add-on
tools moved with it (``ha_get_addon`` -> ``ha_get_app``, ``ha_manage_addon``
-> ``ha_manage_app``). Renaming a tool or one of its parameters is not a
breaking change here — clients resolve tools by name at runtime and re-list
after a server update — but a client that has not re-listed yet would get a
bare "Unknown tool", or an unknown-parameter rejection, until it does.

This middleware closes that window without paying for it in the catalog. It
runs before FastMCP resolves the name and validates the arguments
(``call_tool`` builds the middleware context first and its ``call_next``
re-reads ``context.message``), so rewriting the name or an argument key here
dispatches the call to the current tool. The old names are never registered
or declared, so they stay out of ``tools/list`` and cost no tokens.

The call is served, so nothing else tells that client its list is stale: the
result carries a warning with the refresh steps, and the server log records
the first use of each old name.

Registering the old name as a second, disabled tool would not work:
``get_tool`` filters on ``is_enabled`` and ``call_tool`` then raises
``NotFoundError``, so a disabled tool is unlisted *and* uncallable.
"""

from __future__ import annotations

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)

from ..renamed_tools import (
    RENAMED_TOOLS,
    adapt_retired_arguments,
    add_stale_list_warning,
    retired_parameters_used,
)

logger = logging.getLogger(__name__)

__all__ = ["RENAMED_TOOLS", "RenamedToolAliasMiddleware"]


class RenamedToolAliasMiddleware(Middleware):
    """Dispatch a call on a retired tool or parameter name to its current name.

    Installed first in the chain so every later middleware — read-only
    exemptions, policy gating, redaction — sees the current names it is keyed
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

    def _announce(self, key: str, old: str, new: str) -> None:
        if key in self._announced:
            logger.debug(
                "%s was renamed to %s; dispatching the call to the current name",
                old,
                new,
            )
            return
        self._announced.add(key)
        logger.info(
            "A client called %s, which was renamed to %s; dispatching to the "
            "current name. Old names stay callable, but a client that re-lists "
            "its tools after a server update will not need this.",
            old,
            new,
        )

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        called = context.message.name
        name = RENAMED_TOOLS.get(called, called)
        arguments = context.message.arguments
        renames: list[tuple[str, str]] = []
        if name != called:
            self._announce(called, called, name)
            renames.append((called, name))
            # A consolidated tool needs the ``action`` the retired signature
            # never carried; a plain rename forwards the arguments as they are.
            arguments = adapt_retired_arguments(called, arguments)
        retired_parameters = retired_parameters_used(name, arguments)
        if retired_parameters:
            arguments = {
                retired_parameters.get(key, key): value
                for key, value in (arguments or {}).items()
            }
            for old, new in retired_parameters.items():
                self._announce(f"{name}.{old}", f"{name}({old}=...)", new)
                renames.append((old, new))
        if not renames:
            return await call_next(context)

        result = await call_next(
            context.copy(
                message=context.message.model_copy(
                    update={"name": name, "arguments": arguments}
                )
            )
        )
        add_stale_list_warning(result, renames)
        return result
