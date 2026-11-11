"""A visibility middleware chain for the enforcement tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
)
from ha_mcp.visibility.enforcement import (
    VisibilityInboundEnforcement,
    VisibilityOutboundEnforcement,
)


def _returns(result: SimpleNamespace) -> Callable[[Any], Awaitable[SimpleNamespace]]:
    async def _call_next(context: Any) -> SimpleNamespace:
        return result

    return _call_next


class _ChainedEnforcement:
    """Inbound wrapping outbound, as the server registers the pair."""

    def __init__(self, client: HomeAssistantClient) -> None:
        self.inbound = VisibilityInboundEnforcement(get_client=lambda: client)
        self.outbound = VisibilityOutboundEnforcement(get_client=lambda: client)

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        async def _inner(ctx: Any) -> Any:
            return await self.outbound.on_call_tool(ctx, call_next)

        return await self.inbound.on_call_tool(context, _inner)
