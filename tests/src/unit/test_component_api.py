"""Unit tests for the ``ha_mcp_tools`` capability gate (``component_api``).

Covers the negotiation the server does before routing a read tool through the
custom component: the single cached ``ha_mcp_tools/info`` probe, the
cache-on-failure taxonomy, and the ``code``-based ``unknown_command`` detector.
The WebSocket client is an ``AsyncMock`` whose ``send_command`` dispatches on
the command type, mirroring the live component's info/search surface.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api
from ha_mcp.tools.component_api import (
    SUPPORTED_SCHEMA_VERSION,
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
    # _INFO_OK carries no timezone (a component too old to report it): None.
    assert caps.timezone is None


@pytest.mark.asyncio
async def test_info_probe_parses_timezone_present() -> None:
    """An ``info`` payload carrying ``timezone`` (a newer component) is cached."""
    ws = _make_ws(info_result={**_INFO_OK, "timezone": "Europe/London"})
    with _patch_ws(ws):
        caps = await get_component_caps(_client())

    assert isinstance(caps, ComponentCaps)
    assert caps.timezone == "Europe/London"


@pytest.mark.asyncio
async def test_info_probe_non_string_timezone_is_none() -> None:
    """A non-string ``timezone`` (malformed / drift) is ignored (None)."""
    ws = _make_ws(info_result={**_INFO_OK, "timezone": 123})
    with _patch_ws(ws):
        caps = await get_component_caps(_client())

    assert caps.timezone is None


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
async def test_connection_error_caches_short_transient_negative(monkeypatch) -> None:
    """A WS-down probe caches a SHORT transient negative: a repeat WITHIN the window
    skips the (slow) re-connect and serves ``None`` from cache (review-5 M8)."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    ws = _make_ws(info_exc=HomeAssistantConnectionError("WebSocket not authenticated"))
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None
        # Still inside the short transient window: no re-probe.
        clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S - 1
        assert await get_component_caps(client) is None
    assert ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_expired_transient_negative_reprobes_and_adopts(monkeypatch) -> None:
    """Past the short transient TTL the negative expires: the next call re-probes
    and adopts a now-reachable component (self-healing)."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])

    ws = AsyncMock()
    probe_count = _clock(0.0)

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        assert command_type == "ha_mcp_tools/info"
        probe_count[0] += 1
        if probe_count[0] == 1:
            raise HomeAssistantConnectionError("WebSocket not authenticated")
        return {"success": True, "result": _INFO_OK}

    ws.send_command = AsyncMock(side_effect=_send)
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None  # transient cached at t0
        clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S + 1  # expire it
        caps = await get_component_caps(client)

    assert isinstance(caps, ComponentCaps)
    assert ws.send_command.await_count == 2


@pytest.mark.asyncio
async def test_establish_failure_caches_short_transient_negative(monkeypatch) -> None:
    """The plain ``Exception`` ``get_websocket_client()`` raises when it can't build
    the socket (review-5 M8's slow-connect case) also caches the short transient
    negative, so a repeat within the window skips the connect attempt entirely."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    client = _client()
    factory = AsyncMock(
        side_effect=Exception("Failed to connect to Home Assistant WebSocket")
    )
    with patch.object(component_api, "get_websocket_client", factory):
        assert await get_component_caps(client) is None
        clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S - 1
        assert await get_component_caps(client) is None
    # The connect attempt ran exactly once — the second call used the cache.
    assert factory.await_count == 1


@pytest.mark.asyncio
async def test_transient_negative_shorter_than_absent_negative(monkeypatch) -> None:
    """A transport transient negative expires FAST while an absent-negative from the
    same instant is still live — the two windows are independent."""
    assert (
        component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S
        < component_api._NEGATIVE_CACHE_TTL_S
    )
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    ws = _make_ws(info_exc=HomeAssistantConnectionError("ws down"))
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None
        # Between the transient TTL and the absent TTL: the transient has expired,
        # so this re-probes (unlike a definitive absent-negative, which would not).
        clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S + 1
        assert await get_component_caps(client) is None
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


@pytest.mark.asyncio
async def test_supported_schema_version_routes() -> None:
    """A probe at the supported schema_version yields usable caps."""
    info = {**_INFO_OK, "schema_version": SUPPORTED_SCHEMA_VERSION}
    ws = _make_ws(info_result=info)
    with _patch_ws(ws):
        caps = await get_component_caps(_client())
    assert isinstance(caps, ComponentCaps)
    assert component_supports(caps, "search") is True


@pytest.mark.asyncio
async def test_unsupported_schema_version_caps_none_and_warns(caplog) -> None:
    """A probe at an unsupported schema_version is a cached negative (logged once)."""
    info = {**_INFO_OK, "schema_version": SUPPORTED_SCHEMA_VERSION + 1}
    ws = _make_ws(info_result=info)
    client = _client()
    with _patch_ws(ws), caplog.at_level(logging.WARNING, logger=component_api.__name__):
        first = await get_component_caps(client)
        second = await get_component_caps(client)

    assert first is None
    assert second is None
    # Negative is cached, so the probe (and the warning) happen exactly once.
    assert ws.send_command.await_count == 1
    schema_warnings = [r for r in caplog.records if "schema_version" in r.getMessage()]
    assert len(schema_warnings) == 1
    assert str(SUPPORTED_SCHEMA_VERSION + 1) in schema_warnings[0].getMessage()
    assert str(SUPPORTED_SCHEMA_VERSION) in schema_warnings[0].getMessage()


def _clock(start: float = 1000.0) -> list[float]:
    """A one-element mutable clock; tests advance ``holder[0]`` in place."""
    return [start]


@pytest.mark.asyncio
async def test_fresh_negative_does_not_reprobe(monkeypatch) -> None:
    """A negative within the TTL window is honored without re-probing."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    ws = _make_ws(
        info_exc=HomeAssistantCommandError("Command failed: x", "unknown_command")
    )
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None
        clock[0] += component_api._NEGATIVE_CACHE_TTL_S - 1  # still inside window
        assert await get_component_caps(client) is None
    assert ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_expired_negative_reprobes_and_adopts(monkeypatch) -> None:
    """Past the TTL the negative expires: the next call re-probes and can adopt."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])

    ws = AsyncMock()
    probe_count = _clock(0.0)

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        assert command_type == "ha_mcp_tools/info"
        probe_count[0] += 1
        if probe_count[0] == 1:
            raise HomeAssistantCommandError("Command failed: x", "unknown_command")
        return {"success": True, "result": _INFO_OK}

    ws.send_command = AsyncMock(side_effect=_send)
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None  # negative cached at t0
        clock[0] += component_api._NEGATIVE_CACHE_TTL_S + 1  # expire it
        caps = await get_component_caps(client)

    assert isinstance(caps, ComponentCaps)  # mid-session install adopted
    assert ws.send_command.await_count == 2


@pytest.mark.asyncio
async def test_positive_unaffected_by_ttl(monkeypatch) -> None:
    """A positive entry never expires on the negative TTL timer."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    ws = _make_ws(info_result=_INFO_OK)
    client = _client()
    with _patch_ws(ws):
        first = await get_component_caps(client)
        clock[0] += component_api._NEGATIVE_CACHE_TTL_S * 10  # far past any TTL
        second = await get_component_caps(client)

    assert isinstance(first, ComponentCaps)
    assert first is second
    assert ws.send_command.await_count == 1


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
