"""A visibility middleware chain for the enforcement tests."""

from __future__ import annotations

from types import SimpleNamespace

from ha_mcp.visibility.enforcement import (
    VisibilityInboundEnforcement,
    VisibilityOutboundEnforcement,
)


def _returns(result):
    async def _call_next(context):
        return result

    return _call_next


class _ChainedEnforcement:
    """Inbound + outbound halves chained in server registration order.

    The server registers the inbound half before read-only/policy and the
    outbound half innermost; from the enforcement pair's own perspective the
    composition is inbound-wrapping-outbound, which this reproduces so tests
    exercise the full conceal/refuse/scan pipeline through one entry point.
    """

    def __init__(self, get_client):
        self.inbound = VisibilityInboundEnforcement(get_client=get_client)
        self.outbound = VisibilityOutboundEnforcement(get_client=get_client)

    async def on_call_tool(self, context, call_next):
        async def _inner(ctx):
            return await self.outbound.on_call_tool(ctx, call_next)

        return await self.inbound.on_call_tool(context, _inner)


def make_context(name, arguments=None):
    return SimpleNamespace(
        message=SimpleNamespace(name=name, arguments=arguments or {})
    )


def text_result(text="ok", structured=None):
    """A ToolResult stand-in with a text content block + structured content."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        structured_content=structured,
    )
