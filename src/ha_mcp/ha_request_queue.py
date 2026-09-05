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

_transport_concurrency = 1
_transport_semaphores: WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = WeakKeyDictionary()


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
    loop = asyncio.get_running_loop()
    semaphore = _transport_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_transport_concurrency)
        _transport_semaphores[loop] = semaphore
    async with semaphore:
        yield


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
        if context.message.name in CALL_PROXY_META_TOOLS:
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
