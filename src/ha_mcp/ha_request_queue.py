"""Cross-session concurrency control for Home Assistant tool calls."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

CALL_PROXY_META_TOOLS = frozenset(
    {
        "ha_call_read_tool",
        "ha_call_write_tool",
        "ha_call_delete_tool",
    }
)

LOCAL_ONLY_TOOLS = frozenset({"ha_search_tools"})

_APPROVAL_MANAGEMENT_TOOL = "ha_dev_manage_server"
_APPROVAL_MANAGEMENT_ACTIONS = frozenset({"list_pending", "approve", "deny"})


def is_approval_management_call(name: str, args: dict[str, Any]) -> bool:
    """Return whether a dispatch manages the policy approval queue."""
    return (
        name == _APPROVAL_MANAGEMENT_TOOL
        and args.get("action") in _APPROVAL_MANAGEMENT_ACTIONS
    )


def _bypasses_outer_queue(name: str, args: dict[str, Any]) -> bool:
    """Return whether a dispatch must run without an outer queue slot."""
    return (
        name in CALL_PROXY_META_TOOLS
        or name in LOCAL_ONLY_TOOLS
        or is_approval_management_call(name, args)
    )


class HomeAssistantRequestQueueMiddleware(Middleware):
    """Bound concurrent outer tool calls while allowing nested redispatch."""

    def __init__(self, max_concurrency: int = 1) -> None:
        if not 1 <= max_concurrency <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._depth: ContextVar[int] = ContextVar(
            f"ha_request_queue_depth_{id(self)}", default=0
        )

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        if _bypasses_outer_queue(context.message.name, context.message.arguments or {}):
            return await call_next(context)

        depth = self._depth.get()
        if depth:
            token = self._depth.set(depth + 1)
            try:
                return await call_next(context)
            finally:
                self._depth.reset(token)

        async with self._semaphore:
            token = self._depth.set(1)
            try:
                return await call_next(context)
            finally:
                self._depth.reset(token)
