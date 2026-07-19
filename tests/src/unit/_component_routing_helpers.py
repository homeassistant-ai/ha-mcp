"""Shared WS-mock scaffolding for the ``ha_mcp_tools`` component routing tests.

The four routing suites (``test_ha_search_component_routing`` /
``test_ha_overview_component_routing`` / ``test_config_get_component_routing`` /
``test_ha_config_list_helpers_component_routing``) and the cross-seam
``test_component_readapi_contract`` all mock the same two moving parts: a WS
client whose ``send_command`` dispatches on the command type (``ha_mcp_tools/info``
for the caps probe plus one read command), and the ``get_websocket_client``
factory each tool resolves that WS through. This module owns both so the shape of
the mock lives in one place; each suite keeps its own ``RoutingClient`` spy,
which encodes the per-tool legacy-fetch surface and is deliberately NOT shared.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock, patch

from ha_mcp.tools import component_api


def make_ws(
    command: str,
    *,
    info_result: dict[str, Any] | None = None,
    info_exc: Exception | None = None,
    cmd_result: dict[str, Any] | None = None,
    cmd_exc: Exception | None = None,
) -> AsyncMock:
    """An ``AsyncMock`` WS whose ``send_command`` serves ``info`` + one ``command``.

    ``ha_mcp_tools/info`` returns ``info_result`` (or raises ``info_exc``); the
    read ``command`` (e.g. ``ha_mcp_tools/search``) returns ``cmd_result`` (or
    raises ``cmd_exc``). Any other command type is an ``AssertionError`` so a
    stray frame fails loudly rather than silently no-ops.
    """
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            if info_exc is not None:
                raise info_exc
            return {"success": True, "result": info_result}
        if command_type == command:
            if cmd_exc is not None:
                raise cmd_exc
            return {"success": True, "result": cmd_result}
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


@contextlib.contextmanager
def patch_ws(ws: AsyncMock, tool_module: Any) -> Any:
    """Patch ``get_websocket_client`` to yield ``ws`` on every resolution path.

    The caps probe always resolves through ``component_api.get_websocket_client``,
    so that is patched unconditionally. A tool module also imports its own
    ``get_websocket_client`` when it sends the read command itself (search /
    overview / helpers_list); the config-get consumers are legacy-only (their
    gets never route through the component at all), so those modules do not bind
    the symbol — patch the tool module only when it actually has it.
    """
    factory = AsyncMock(return_value=ws)
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(component_api, "get_websocket_client", factory)
        )
        if hasattr(tool_module, "get_websocket_client"):
            stack.enter_context(
                patch.object(tool_module, "get_websocket_client", factory)
            )
        yield ws


@contextlib.contextmanager
def patch_ws_establish_failure(
    caps_ws: AsyncMock, tool_module: Any, exc: BaseException
) -> Any:
    """Caps probe resolves ``caps_ws`` (``info`` works); the read raises ``exc``.

    Splits the two ``get_websocket_client`` bindings: ``component_api``'s resolves
    ``caps_ws`` so the caps probe succeeds and caches a positive capability, but the
    tool module's own ``get_websocket_client`` raises ``exc`` — simulating
    ``WebSocketManager`` failing to (re)establish the pooled socket for the READ
    command AFTER caps were cached. Callers pass the
    ``HomeAssistantConnectionError`` the manager raises on a failed connect
    (#1947 retyped it from a bare ``Exception``), which the REST-legacy helpers
    route to their legacy fetch.
    """
    good = AsyncMock(return_value=caps_ws)
    bad = AsyncMock(side_effect=exc)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(component_api, "get_websocket_client", good))
        if hasattr(tool_module, "get_websocket_client"):
            stack.enter_context(patch.object(tool_module, "get_websocket_client", bad))
        yield
