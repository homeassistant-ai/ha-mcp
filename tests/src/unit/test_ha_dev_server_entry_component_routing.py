"""Routing tests for ``find_server_config_entry`` over the ``ha_mcp_tools``
component gate (``server_entry`` capability).

``find_server_config_entry`` used to probe EVERY ``ha_mcp_tools``-domain
config entry's options flow from the outside (start + inspect schema for the
``pip_spec`` field + abort on mismatch) just to find its own server entry.
When the component advertises ``server_entry``, one in-process read returns
the entry_id directly (or an authoritative "no server entry" verdict), and
exactly one options flow — for that entry — is opened for the caller to
submit or abort. These tests pin: the component path opens exactly one flow
and never probes/aborts a wrong candidate; a capability miss falls back to
the legacy per-candidate probe loop unchanged; and the full component error
taxonomy (unknown_command / command error / a transport failure — both a
connection error off the frame and a plain establish ``Exception`` — falling back
to the legacy probe, which rides the swallowing bridge and so does not die
identically) behaves like every other component consumer.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, tools_dev
from ha_mcp.tools.tools_dev import find_server_config_entry

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

_CAPS_SERVER_ENTRY = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["server_entry"],
    "limits": {},
}

_SERVER_SCHEMA = [
    {"name": "channel", "default": "stable"},
    {"name": "pip_spec", "description": {"suggested_value": None}},
    {"name": "server_url", "description": {"suggested_value": "http://ha.local:8123"}},
]

_TOOLS_SCHEMA: list[dict[str, Any]] = []  # informational form, no fields


class RoutingClient:
    """Credentialed HA client spy: tallies legacy probe/abort calls."""

    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._entries = list(entries or [])
        self._flows: dict[str, Any] = {}
        self.config_entries_get_calls = 0
        self.start_options_flow_calls: list[str] = []
        self.abort_options_flow_calls: list[str] = []

    def set_flow(self, entry_id: str, flow: Any) -> None:
        """``flow`` is either a flow dict or an Exception instance to raise."""
        self._flows[entry_id] = flow

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "config_entries/get":
            self.config_entries_get_calls += 1
            return {"success": True, "result": list(self._entries)}
        raise AssertionError(f"unexpected ws message {msg!r}")

    async def start_options_flow(self, entry_id: str) -> Any:
        self.start_options_flow_calls.append(entry_id)
        flow = self._flows.get(entry_id)
        if isinstance(flow, Exception):
            raise flow
        if flow is None:
            raise AssertionError(f"no flow configured for {entry_id!r}")
        return flow

    async def abort_options_flow(self, flow_id: str) -> dict[str, Any]:
        self.abort_options_flow_calls.append(flow_id)
        return {}


def _flow(
    entry_id: str, schema: list[dict[str, Any]], flow_type: str = "form"
) -> dict[str, Any]:
    return {"flow_id": f"flow-{entry_id}", "type": flow_type, "data_schema": schema}


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.mark.asyncio
async def test_component_success_opens_exactly_one_flow_no_aborts() -> None:
    """capability present → the component identifies the entry; exactly one
    start_options_flow for THAT entry; zero probe aborts; legacy
    config_entries/get is never called."""
    ws = make_ws(
        "ha_mcp_tools/server_entry",
        info_result=_CAPS_SERVER_ENTRY,
        cmd_result={"entry_id": "server-1", "channel": "stable", "pip_spec": None},
    )
    client = RoutingClient()
    client.set_flow("server-1", _flow("server-1", _SERVER_SCHEMA))

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is not None
    entry_id, flow, current = found
    assert entry_id == "server-1"
    assert flow["flow_id"] == "flow-server-1"
    # current_options is built from the flow's OWN schema (not the narrower
    # component channel/pip_spec shape), so _update_source's preserved-keys
    # resend still sees server_url etc.
    assert current["channel"] == "stable"
    assert current["server_url"] == "http://ha.local:8123"
    assert client.start_options_flow_calls == ["server-1"]
    assert client.abort_options_flow_calls == []
    assert client.config_entries_get_calls == 0


@pytest.mark.asyncio
async def test_component_authoritative_no_entry_returns_none_without_probing() -> None:
    """component says entry_id=None → treated as authoritative "not found":
    return None directly, no options flow opened, no legacy probe."""
    ws = make_ws(
        "ha_mcp_tools/server_entry",
        info_result=_CAPS_SERVER_ENTRY,
        cmd_result={"entry_id": None, "channel": None, "pip_spec": None},
    )
    client = RoutingClient()

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is None
    assert client.start_options_flow_calls == []
    assert client.config_entries_get_calls == 0


@pytest.mark.asyncio
async def test_capability_miss_falls_back_to_legacy_probe_loop() -> None:
    """No server_entry capability → the unchanged legacy per-candidate probe
    loop runs: both entries are probed, the non-matching one is aborted, the
    pip_spec-carrying one is returned."""
    ws = make_ws(
        "ha_mcp_tools/server_entry",
        info_result={
            "schema_version": 1,
            "component_version": "1.1.1",
            "capabilities": [],
            "limits": {},
        },
    )
    client = RoutingClient(entries=[{"entry_id": "tools-1"}, {"entry_id": "server-1"}])
    client.set_flow("tools-1", _flow("tools-1", _TOOLS_SCHEMA))
    client.set_flow("server-1", _flow("server-1", _SERVER_SCHEMA))

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is not None
    entry_id, flow, current = found
    assert entry_id == "server-1"
    assert client.config_entries_get_calls == 1
    assert client.start_options_flow_calls == ["tools-1", "server-1"]
    # The non-matching tools entry's probe flow was aborted.
    assert client.abort_options_flow_calls == ["flow-tools-1"]


@pytest.mark.asyncio
async def test_unknown_command_falls_back_and_invalidates_caps() -> None:
    """server_entry advertised but returns unknown_command (downgrade) →
    invalidate caps, fall back to the legacy probe loop."""
    ws = make_ws(
        "ha_mcp_tools/server_entry",
        info_result=_CAPS_SERVER_ENTRY,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(entries=[{"entry_id": "server-1"}])
    client.set_flow("server-1", _flow("server-1", _SERVER_SCHEMA))

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is not None
    assert found[0] == "server-1"
    assert client.config_entries_get_calls == 1
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_command_error_falls_back_without_invalidating_caps() -> None:
    """A non-unknown_command error (timeout) falls back to legacy WITHOUT
    invalidating the still-advertised capability."""
    ws = make_ws(
        "ha_mcp_tools/server_entry",
        info_result=_CAPS_SERVER_ENTRY,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient(entries=[{"entry_id": "server-1"}])
    client.set_flow("server-1", _flow("server-1", _SERVER_SCHEMA))

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is not None
    assert found[0] == "server-1"
    assert client.config_entries_get_calls == 1
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_connection_error_falls_back_to_legacy() -> None:
    """A WS connection error on the server_entry frame falls back to the legacy
    per-candidate probe rather than propagating — the legacy probe rides the
    swallowing bridge, so it does not die identically."""
    ws = make_ws(
        "ha_mcp_tools/server_entry",
        info_result=_CAPS_SERVER_ENTRY,
        cmd_exc=HomeAssistantConnectionError("ws down"),
    )
    client = RoutingClient(entries=[{"entry_id": "server-1"}])
    client.set_flow("server-1", _flow("server-1", _SERVER_SCHEMA))

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is not None
    assert found[0] == "server-1"
    assert client.config_entries_get_calls == 1
    # A transient connection error is not a downgrade — caps stay cached.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_ws_establish_failure_falls_back_to_legacy() -> None:
    """A plain establish ``Exception`` from ``get_websocket_client()`` (after caps
    are cached) falls back to the legacy per-candidate probe."""
    caps_ws = make_ws("ha_mcp_tools/server_entry", info_result=_CAPS_SERVER_ENTRY)
    client = RoutingClient(entries=[{"entry_id": "server-1"}])
    client.set_flow("server-1", _flow("server-1", _SERVER_SCHEMA))

    with patch_ws_establish_failure(
        caps_ws,
        tools_dev,
        HomeAssistantConnectionError("Failed to connect to Home Assistant WebSocket"),
    ):
        found = await find_server_config_entry(client)

    assert found is not None
    assert found[0] == "server-1"
    assert client.config_entries_get_calls == 1
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_malformed_component_result_falls_back_to_legacy() -> None:
    """A server_entry reply missing the entry_id key (shape drift) is treated
    as a component miss, not trusted — falls back to the legacy probe."""
    ws = make_ws(
        "ha_mcp_tools/server_entry",
        info_result=_CAPS_SERVER_ENTRY,
        cmd_result={"channel": "stable"},  # no entry_id key
    )
    client = RoutingClient(entries=[{"entry_id": "server-1"}])
    client.set_flow("server-1", _flow("server-1", _SERVER_SCHEMA))

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is not None
    assert found[0] == "server-1"
    assert client.config_entries_get_calls == 1


@pytest.mark.asyncio
async def test_empty_string_entry_id_falls_back_to_legacy() -> None:
    """An empty-string ``entry_id`` is NOT the component's real "no entry"
    signal (that's ``None``) — it is treated like a malformed reply (a
    component miss) and falls back to the legacy probe, rather than being
    trusted as an authoritative "not found" verdict."""
    ws = make_ws(
        "ha_mcp_tools/server_entry",
        info_result=_CAPS_SERVER_ENTRY,
        cmd_result={"entry_id": "", "channel": None, "pip_spec": None},
    )
    client = RoutingClient(entries=[{"entry_id": "server-1"}])
    client.set_flow("server-1", _flow("server-1", _SERVER_SCHEMA))

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is not None
    assert found[0] == "server-1"
    assert client.config_entries_get_calls == 1


@pytest.mark.asyncio
async def test_identified_entry_flow_open_failure_falls_back_and_exhausts_legacy() -> (
    None
):
    """The component identifies an entry_id, but opening ITS options flow
    fails on every attempt (e.g. the entry was removed) — falls back to the
    legacy per-candidate probe loop, which retries the SAME entry, fails
    again, and (no other candidates) returns None rather than raising."""
    ws = make_ws(
        "ha_mcp_tools/server_entry",
        info_result=_CAPS_SERVER_ENTRY,
        cmd_result={"entry_id": "server-1", "channel": "stable", "pip_spec": None},
    )
    client = RoutingClient(entries=[{"entry_id": "server-1"}])
    client.set_flow("server-1", HomeAssistantAPIError("entry gone"))

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is None
    # Tried once from the component-identified path, once more from the
    # legacy loop over the (still-listed) entry — both failed the same way.
    assert client.start_options_flow_calls == ["server-1", "server-1"]
    assert client.config_entries_get_calls == 1


@pytest.mark.asyncio
async def test_identified_entry_flow_open_failure_then_legacy_finds_entry() -> None:
    """Same race as above, but the legacy retry's flow open succeeds (the
    transient failure cleared) — the caller still gets a usable result."""
    ws = make_ws(
        "ha_mcp_tools/server_entry",
        info_result=_CAPS_SERVER_ENTRY,
        cmd_result={"entry_id": "server-1", "channel": "stable", "pip_spec": None},
    )

    class _FlakyOnceClient(RoutingClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._start_calls = 0

        async def start_options_flow(self, entry_id: str) -> Any:
            self._start_calls += 1
            self.start_options_flow_calls.append(entry_id)
            if self._start_calls == 1:
                raise HomeAssistantAPIError("entry gone")
            return self._flows[entry_id]

    client = _FlakyOnceClient(entries=[{"entry_id": "server-1"}])
    client.set_flow("server-1", _flow("server-1", _SERVER_SCHEMA))

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is not None
    assert found[0] == "server-1"
    # First open (component-identified path) failed, legacy loop retried it.
    assert client.start_options_flow_calls == ["server-1", "server-1"]
    assert client.config_entries_get_calls == 1
