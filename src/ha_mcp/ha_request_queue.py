"""Cross-session concurrency control for Home Assistant tool calls."""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import Any

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

from .errors import create_timeout_error
from .tool_dispatch import (
    CALL_PROXY_META_TOOLS,
    LOCAL_ONLY_TOOLS,
    OUTER_QUEUE_ORCHESTRATORS,
    is_approval_management_call,
)
from .tools.helpers import raise_tool_error, safe_progress

logger = logging.getLogger(__name__)

_QUEUE_WAIT_TIMEOUT_SECONDS = 60.0
_QUEUE_WAIT_HEARTBEAT_SECONDS = 15.0
_QUEUE_WAIT_WARNING_SECONDS = 15.0


def _bypasses_outer_queue(name: str, args: dict[str, Any]) -> bool:
    """Return whether an outer envelope must run without a queue slot.

    Proxy and orchestration envelopes leave admission to their nested dispatches.
    Local search does not contact Home Assistant. Approval-management actions
    must remain available while all configured slots are occupied.
    """
    return (
        name in CALL_PROXY_META_TOOLS
        or name in LOCAL_ONLY_TOOLS
        or name in OUTER_QUEUE_ORCHESTRATORS
        or is_approval_management_call(name, args)
    )


class HomeAssistantRequestQueueMiddleware(Middleware):
    """Bound concurrent outer tool calls while allowing nested redispatch."""

    def __init__(self, max_concurrency: int = 1) -> None:
        if not 1 <= max_concurrency <= 32:
            raise ValueError(
                "ha_tool_concurrency must be between 0 and 32; "
                "0 disables the queue middleware"
            )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._depth: ContextVar[int] = ContextVar(
            f"ha_request_queue_depth_{id(self)}", default=0
        )

    async def _acquire_slot(self, context: MiddlewareContext, name: str) -> None:
        """Acquire an outer-call slot with progress and a bounded wait."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        warned = False

        if not self._semaphore.locked():
            await self._semaphore.acquire()
            logger.debug("HA tool queue: acquired slot tool=%s wait=0.000s", name)
            return

        logger.debug("HA tool queue: waiting for slot tool=%s", name)

        while True:
            elapsed = loop.time() - started
            remaining = _QUEUE_WAIT_TIMEOUT_SECONDS - elapsed
            if remaining <= 0:
                logger.warning(
                    "HA tool queue: timed out waiting for slot tool=%s wait=%.1fs",
                    name,
                    elapsed,
                )
                raise_tool_error(
                    create_timeout_error(
                        "Home Assistant tool queue admission",
                        _QUEUE_WAIT_TIMEOUT_SECONDS,
                        details=(
                            f"Tool '{name}' did not start because all configured "
                            "Home Assistant tool slots remained busy."
                        ),
                        context={"tool": name, "phase": "queue_wait"},
                    )
                )

            ctx = getattr(context, "fastmcp_context", None)
            await safe_progress(
                ctx,
                progress=0,
                total=0,
                message=f"Waiting for Home Assistant tool capacity: {name}",
            )
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=min(_QUEUE_WAIT_HEARTBEAT_SECONDS, remaining),
                )
            except TimeoutError:
                elapsed = loop.time() - started
                if not warned and elapsed >= _QUEUE_WAIT_WARNING_SECONDS:
                    warned = True
                    logger.warning(
                        "HA tool queue: still waiting for slot tool=%s wait=%.1fs",
                        name,
                        elapsed,
                    )
                continue

            logger.debug(
                "HA tool queue: acquired slot tool=%s wait=%.3fs",
                name,
                loop.time() - started,
            )
            return

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

        name = context.message.name
        await self._acquire_slot(context, name)
        try:
            token = self._depth.set(1)
            try:
                return await call_next(context)
            finally:
                self._depth.reset(token)
        finally:
            self._semaphore.release()
            logger.debug("HA tool queue: released slot tool=%s", name)
