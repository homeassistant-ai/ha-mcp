"""Unit tests for the ``ha_mcp_tools`` capability gate (``component_api``).

Covers the negotiation the server does before routing a read tool through the
custom component: the single cached ``ha_mcp_tools/info`` probe, the
cache-on-failure taxonomy, and the ``code``-based ``unknown_command`` detector.
The WebSocket client is an ``AsyncMock`` whose ``send_command`` dispatches on
the command type, mirroring the live component's info/search surface.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api
from ha_mcp.tools.component_api import (
    ComponentCaps,
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)

_INFO_OK = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["search", "overview"],
    "limits": {"max_results": 500},
}


class _FakeClient:
    """Weakref-able credentialed client stand-in (real REST client is a class)."""

    def __init__(self, base_url: str = "http://ha.local:8123", token: str = "tok"):
        self.base_url = base_url
        self.token = token


class _BareClient:
    """Weakref-able client with no credentials (nothing to negotiate over)."""


def _client() -> Any:
    return _FakeClient()


def _make_ws(
    *,
    info_result: dict[str, Any] | None = None,
    info_exc: Exception | None = None,
) -> AsyncMock:
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            if info_exc is not None:
                raise info_exc
            return {"success": True, "result": info_result}
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _patch_ws(ws: AsyncMock) -> Any:
    return patch.object(
        component_api, "get_websocket_client", AsyncMock(return_value=ws)
    )


@pytest.mark.asyncio
async def test_info_probe_parses_capabilities() -> None:
    ws = _make_ws(info_result=_INFO_OK)
    with _patch_ws(ws):
        caps = await get_component_caps(_client())

    assert isinstance(caps, ComponentCaps)
    assert caps.capabilities == frozenset({"search", "overview"})
    assert caps.schema_version == 1
    assert caps.component_version == "1.1.0"
    assert caps.limits == {"max_results": 500}


@pytest.mark.asyncio
async def test_caps_cached_one_probe_per_client() -> None:
    ws = _make_ws(info_result=_INFO_OK)
    client = _client()
    with _patch_ws(ws):
        first = await get_component_caps(client)
        second = await get_component_caps(client)

    assert first is second
    info_calls = [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/info"
    ]
    assert len(info_calls) == 1


@pytest.mark.asyncio
async def test_unknown_command_caches_none_and_does_not_reprobe() -> None:
    ws = _make_ws(
        info_exc=HomeAssistantCommandError("Command failed: x", "unknown_command")
    )
    client = _client()
    with _patch_ws(ws):
        first = await get_component_caps(client)
        second = await get_component_caps(client)

    assert first is None
    assert second is None
    # Cached negative: the info command is probed exactly once.
    assert ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_connection_error_is_not_cached_and_reprobes() -> None:
    ws = _make_ws(info_exc=HomeAssistantConnectionError("WebSocket not authenticated"))
    client = _client()
    with _patch_ws(ws):
        first = await get_component_caps(client)
        second = await get_component_caps(client)

    assert first is None
    assert second is None
    # Transient failure is not cached, so each call re-probes.
    assert ws.send_command.await_count == 2


@pytest.mark.asyncio
async def test_no_credentials_returns_none_without_probing() -> None:
    ws = _make_ws(info_result=_INFO_OK)
    bare_client = _BareClient()  # no base_url / token
    with _patch_ws(ws):
        caps = await get_component_caps(bare_client)

    assert caps is None
    ws.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_info_result_caches_none() -> None:
    ws = _make_ws(info_result=None)  # success but no result dict
    client = _client()
    with _patch_ws(ws):
        first = await get_component_caps(client)
        second = await get_component_caps(client)

    assert first is None
    assert second is None
    # A responding-but-malformed component is a stable negative → probed once.
    assert ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_invalidate_caps_forces_reprobe() -> None:
    ws = _make_ws(info_result=_INFO_OK)
    client = _client()
    with _patch_ws(ws):
        await get_component_caps(client)
        invalidate_caps(client)
        await get_component_caps(client)

    assert ws.send_command.await_count == 2


def test_component_supports() -> None:
    caps = ComponentCaps(
        schema_version=1,
        component_version="1.1.0",
        capabilities=frozenset({"search"}),
        limits={},
    )
    assert component_supports(caps, "search") is True
    assert component_supports(caps, "overview") is False
    assert component_supports(None, "search") is False


def test_is_unknown_command_keys_off_code_not_message() -> None:
    # Structured code drives the decision.
    assert is_unknown_command(
        HomeAssistantCommandError("Command failed: nope", "unknown_command")
    )
    # A message that merely mentions "unknown command" but carries no code does
    # NOT match — routing must not key off the human-readable text.
    assert not is_unknown_command(
        HomeAssistantCommandError("Command failed: unknown command foo")
    )
    # Some other structured code does not match either.
    assert not is_unknown_command(
        HomeAssistantCommandError("Command failed: bad", "invalid_format")
    )
    assert not is_unknown_command(ValueError("boom"))
