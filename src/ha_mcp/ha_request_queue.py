"""Cross-session concurrency control for Home Assistant tool calls."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any
from weakref import WeakKeyDictionary

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

CALL_PROXY_META_TOOLS = frozenset(
    {
        "ha_call_read_tool",
        "ha_call_write_tool",
        "ha_call_delete_tool",
    }
)

_APPROVAL_MANAGEMENT_TOOL = "ha_dev_manage_server"
_APPROVAL_MANAGEMENT_ACTIONS = frozenset({"list_pending", "approve", "deny"})

_transport_concurrency = 1
_transport_semaphores: WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = WeakKeyDictionary()
_transport_request_depth: ContextVar[int] = ContextVar(
    "ha_transport_request_depth", default=0
)


def configure_ha_transport_concurrency(max_concurrency: int) -> None:
    """Set the process-wide HA transport capacity for subsequently used loops."""
    if not 1 <= max_concurrency <= 32:
        raise ValueError("max_concurrency must be between 1 and 32")
    global _transport_concurrency
    _transport_concurrency = max_concurrency
    _transport_semaphores.clear()


@asynccontextmanager
async def limit_ha_transport_request() -> AsyncIterator[None]:
    """Bound in-flight HA REST and WebSocket requests across all clients."""
    depth = _transport_request_depth.get()
    if depth:
        token = _transport_request_depth.set(depth + 1)
        try:
            yield
        finally:
            _transport_request_depth.reset(token)
        return

    loop = asyncio.get_running_loop()
    semaphore = _transport_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_transport_concurrency)
        _transport_semaphores[loop] = semaphore
    async with semaphore:
        token = _transport_request_depth.set(1)
        try:
            yield
        finally:
            _transport_request_depth.reset(token)


def is_approval_management_call(name: str, args: dict[str, Any]) -> bool:
    """Return whether a dispatch manages the policy approval queue."""
    return (
        name == _APPROVAL_MANAGEMENT_TOOL
        and args.get("action") in _APPROVAL_MANAGEMENT_ACTIONS
    )


def _bypasses_outer_queue(name: str, args: dict[str, Any]) -> bool:
    """Return whether a dispatch must run without an outer queue slot."""
    return name in CALL_PROXY_META_TOOLS or is_approval_management_call(name, args)


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
