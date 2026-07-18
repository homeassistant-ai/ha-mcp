"""Routing tests for ``ha_dev_manage_server(update_source)`` over the component
``server_entry_update`` WRITE capability (issue #1813 Phase 3).

In EMBEDDED mode the tool prefers the component's one-hop direct write
(``ha_mcp_tools/server_entry_update`` → ``async_update_entry``) over the
options-flow start+submit round-trip. These pin the routing contract:

* capability present → the component write is used, its scheduled reply is mapped,
  the legacy ``submit_options_flow_step`` is NEVER called, and the flow
  ``find_server_config_entry`` opened is aborted;
* capability absent → falls back to the legacy (deferred) options-flow submit;
* the full component-error taxonomy (unknown_command invalidates caps; a command
  error / a connection error / an establishment failure all fall back) routes to
  the legacy submit — the write is idempotent, so a re-apply is safe;
* NON-embedded mode never routes to the component write (the ``server_entry_update``
  frame is never sent) and submits the options flow synchronously.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, tools_dev
from ha_mcp.tools.tools_dev import DevTools

from ._component_routing_helpers import patch_ws, patch_ws_establish_failure

_CAPS_FULL = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["server_entry", "server_entry_update"],
    "limits": {},
}
_CAPS_READ_ONLY = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["server_entry"],
    "limits": {},
}

_SERVER_FLOW = {
    "flow_id": "flow-srv1",
    "type": "form",
    "data_schema": [
        {"name": "channel", "default": "stable"},
        {"name": "pip_spec", "description": {"suggested_value": None}},
        {"name": "server_url", "description": {"suggested_value": "http://ha:8123"}},
    ],
}

_SERVER_ENTRY_RESULT = {"entry_id": "srv1", "channel": "stable", "pip_spec": None}


def _update_ws(
    *,
    caps: dict[str, Any],
    update_result: dict[str, Any] | None = None,
    update_exc: Exception | None = None,
) -> AsyncMock:
    """WS mock serving info + server_entry + (optionally) server_entry_update."""
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": caps}
        if command_type == "ha_mcp_tools/server_entry":
            return {"success": True, "result": dict(_SERVER_ENTRY_RESULT)}
        if command_type == "ha_mcp_tools/server_entry_update":
            if update_exc is not None:
                raise update_exc
            return {"success": True, "result": update_result}
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _update_frame_kwargs(ws: AsyncMock) -> dict[str, Any]:
    """The kwargs delivered on the single ``server_entry_update`` frame.

    The delta reaches the wire as ``send_command`` KWARGS (the security-relevant
    channel/pip_spec fields), so the routing tests inspect ``.kwargs`` — not just
    that the command TYPE reached ``.args[0]`` — to catch a dropped / wrong-keyed
    field.
    """
    calls = [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/server_entry_update"
    ]
    assert len(calls) == 1
    return calls[0].kwargs


class UpdateClient:
    """Credentialed client: options-flow surface + the legacy config_entries bridge.

    ``config_entries`` is what ``config_entries/get`` returns for the legacy
    ``find_server_config_entry`` fallback. Empty by default: the component
    ``server_entry`` read short-circuits find in the component-served tests, so the
    legacy probe is never reached. A test drives find DOWN the legacy path by
    supplying the server entry here — e.g. the establishment-failure case, where the
    SHARED ``tools_dev.get_websocket_client`` is broken for BOTH the component read
    and the write, so find must locate the entry through this bridge (which rides
    ``send_websocket_message``, a different transport that stays up).
    """

    def __init__(self, config_entries: list[dict[str, Any]] | None = None) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self._config_entries = list(config_entries or [])
        self.start_options_flow_calls: list[str] = []
        self.abort_options_flow_calls: list[str] = []
        self.submit_calls: list[tuple[str, dict[str, Any]]] = []
        self.config_entries_get_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "config_entries/get":
            self.config_entries_get_calls += 1
            return {"success": True, "result": list(self._config_entries)}
        raise AssertionError(f"unexpected ws message {msg!r}")

    async def start_options_flow(self, entry_id: str) -> dict[str, Any]:
        self.start_options_flow_calls.append(entry_id)
        return dict(_SERVER_FLOW)

    async def abort_options_flow(self, flow_id: str) -> dict[str, Any]:
        self.abort_options_flow_calls.append(flow_id)
        return {}

    async def submit_options_flow_step(
        self, flow_id: str, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        self.submit_calls.append((flow_id, dict(user_input)))
        return {"type": "create_entry"}


async def _drain_background_tasks() -> None:
    tasks = list(tools_dev._BACKGROUND_TASKS)
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks)
    assert not pending
    for task in done:
        task.result()


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.fixture(autouse=True)
def _embedded_env(monkeypatch: Any) -> Any:
    """Default to embedded mode with a zero flush delay; individual tests may
    clear the env to exercise the non-embedded path."""
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    monkeypatch.setattr(tools_dev, "_SELF_ACTION_FLUSH_DELAY_S", 0)
    yield


@pytest.mark.asyncio
async def test_embedded_capability_present_routes_to_component_write() -> None:
    """capability present → component direct write; scheduled reply mapped; the
    legacy submit is never called; the opened flow is aborted."""
    ws = _update_ws(
        caps=_CAPS_FULL,
        update_result={
            "scheduled": True,
            "entry_id": "srv1",
            "applying": {"channel": "dev"},
            "previous": {"channel": "stable", "pip_spec": None},
        },
    )
    client = UpdateClient()

    with patch_ws(ws, tools_dev):
        result = await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )

    data = result["data"]
    assert data["scheduled"] is True
    assert data["entry_id"] == "srv1"
    assert data["applying"] == {"channel": "dev"}
    assert data["previous"] == {"channel": "stable", "pip_spec": None}
    assert data["note"] == (
        "The in-process server will reinstall and restart now; this "
        "connection will drop. Reconnect in 1-5 minutes and verify with "
        "ha_dev_manage_server('info')."
    )
    # Preserves the #1929 target field on the component path (embedded phrasing).
    assert data["target"] == "this server (the embedded ha_mcp_tools in-process entry)"
    # The component write was used; the legacy options-flow submit was NOT.
    assert client.submit_calls == []
    # The flow find_server_config_entry opened (for the unused legacy path) was
    # aborted rather than leaked.
    assert client.abort_options_flow_calls == ["flow-srv1"]
    # The server_entry_update frame carried EXACTLY the channel delta as kwargs.
    assert _update_frame_kwargs(ws) == {"channel": "dev"}


@pytest.mark.asyncio
async def test_embedded_unchanged_reply_maps_without_submit() -> None:
    """A no-op component reply (unchanged) maps to scheduled:false/unchanged and
    still bypasses the legacy submit."""
    ws = _update_ws(
        caps=_CAPS_FULL,
        update_result={
            "scheduled": False,
            "unchanged": True,
            "entry_id": "srv1",
            "applying": {"channel": "stable"},
            "previous": {"channel": "stable", "pip_spec": None},
        },
    )
    client = UpdateClient()

    with patch_ws(ws, tools_dev):
        result = await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="stable"
        )

    data = result["data"]
    assert data["scheduled"] is False
    assert data["unchanged"] is True
    assert data["previous"] == {"channel": "stable", "pip_spec": None}
    assert data["note"] == (
        "No change: the requested channel/pip_spec already matches the "
        "current in-process server source."
    )
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_embedded_pip_spec_delta_delivers_pip_spec_kwargs() -> None:
    """A pip_spec-only update sends EXACTLY {"pip_spec": ...} on the frame (the
    security-relevant field is neither dropped nor mis-keyed) and maps the scheduled
    reply — note text and the embedded target preserved."""
    ws = _update_ws(
        caps=_CAPS_FULL,
        update_result={
            "scheduled": True,
            "entry_id": "srv1",
            "applying": {"pip_spec": "ha-mcp==2.0.0"},
            "previous": {"channel": "stable", "pip_spec": None},
        },
    )
    client = UpdateClient()

    with patch_ws(ws, tools_dev):
        result = await DevTools(client).ha_dev_manage_server(
            action="update_source", pip_spec="ha-mcp==2.0.0"
        )

    assert _update_frame_kwargs(ws) == {"pip_spec": "ha-mcp==2.0.0"}
    data = result["data"]
    assert data["scheduled"] is True
    assert data["applying"] == {"pip_spec": "ha-mcp==2.0.0"}
    assert data["target"] == "this server (the embedded ha_mcp_tools in-process entry)"
    assert data["note"] == (
        "The in-process server will reinstall and restart now; this "
        "connection will drop. Reconnect in 1-5 minutes and verify with "
        "ha_dev_manage_server('info')."
    )
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_embedded_channel_and_pip_spec_delta_delivers_both_kwargs() -> None:
    """channel AND pip_spec set → the frame carries EXACTLY both fields as kwargs."""
    ws = _update_ws(
        caps=_CAPS_FULL,
        update_result={
            "scheduled": True,
            "entry_id": "srv1",
            "applying": {"channel": "dev", "pip_spec": "ha-mcp==2.0.0"},
            "previous": {"channel": "stable", "pip_spec": None},
        },
    )
    client = UpdateClient()

    with patch_ws(ws, tools_dev):
        await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev", pip_spec="ha-mcp==2.0.0"
        )

    assert _update_frame_kwargs(ws) == {"channel": "dev", "pip_spec": "ha-mcp==2.0.0"}
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_embedded_capability_absent_falls_back_to_options_flow() -> None:
    """No server_entry_update capability → legacy (deferred) options-flow submit,
    resending the preserved server_url override alongside the channel change."""
    ws = _update_ws(caps=_CAPS_READ_ONLY)
    client = UpdateClient()

    with patch_ws(ws, tools_dev):
        result = await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )
        assert result["data"]["scheduled"] is True
        assert client.submit_calls == []  # deferred, not yet run
        await _drain_background_tasks()

    assert len(client.submit_calls) == 1
    flow_id, user_input = client.submit_calls[0]
    assert flow_id == "flow-srv1"
    assert user_input == {"server_url": "http://ha:8123", "channel": "dev"}
    # The component write frame was never sent (capability absent).
    sent = [c.args[0] for c in ws.send_command.call_args_list]
    assert "ha_mcp_tools/server_entry_update" not in sent


@pytest.mark.asyncio
async def test_embedded_unknown_command_invalidates_caps_and_falls_back() -> None:
    """server_entry_update advertised but returns unknown_command → invalidate the
    cached caps and fall back to the legacy submit."""
    ws = _update_ws(
        caps=_CAPS_FULL,
        update_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = UpdateClient()

    with patch_ws(ws, tools_dev):
        await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )
        await _drain_background_tasks()

    assert len(client.submit_calls) == 1
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_embedded_command_error_falls_back_without_invalidating() -> None:
    """A non-unknown_command error (timeout) falls back to the legacy submit
    WITHOUT invalidating the still-advertised capability."""
    ws = _update_ws(caps=_CAPS_FULL, update_exc=HomeAssistantCommandTimeout("slow"))
    client = UpdateClient()

    with patch_ws(ws, tools_dev):
        await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )
        await _drain_background_tasks()

    assert len(client.submit_calls) == 1
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_embedded_connection_error_falls_back() -> None:
    """A WS connection error on the server_entry_update frame falls back to the
    legacy submit (the write is idempotent, so a re-apply is safe)."""
    ws = _update_ws(caps=_CAPS_FULL, update_exc=HomeAssistantConnectionError("down"))
    client = UpdateClient()

    with patch_ws(ws, tools_dev):
        await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )
        await _drain_background_tasks()

    assert len(client.submit_calls) == 1
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_embedded_malformed_reply_falls_back() -> None:
    """A component reply that is neither scheduled nor unchanged (shape drift) is
    treated as unusable and falls back to the legacy submit."""
    ws = _update_ws(caps=_CAPS_FULL, update_result={"entry_id": "srv1"})
    client = UpdateClient()

    with patch_ws(ws, tools_dev):
        await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )
        await _drain_background_tasks()

    assert len(client.submit_calls) == 1


@pytest.mark.asyncio
async def test_embedded_establishment_failure_falls_back() -> None:
    """A plain establish Exception from get_websocket_client falls back to the
    legacy submit.

    ``patch_ws_establish_failure`` breaks the SHARED ``tools_dev.get_websocket_client``
    for BOTH the component ``server_entry`` read AND the write frame (one binding
    serves both), exactly as a real broken pooled socket would. So
    ``find_server_config_entry``'s component read fails over to the legacy
    ``config_entries/get`` bridge — which rides ``send_websocket_message``, a
    different transport that stays up — and locates the entry there; THEN the write
    frame's establishment fails and the tool falls back to the legacy options-flow
    submit. The caps probe (``component_api``'s binding) still succeeds, so the write
    route is attempted and its establishment failure is what triggers the fallback.
    """
    caps_ws = _update_ws(caps=_CAPS_FULL)
    client = UpdateClient(config_entries=[{"entry_id": "srv1"}])

    with patch_ws_establish_failure(
        caps_ws, tools_dev, Exception("Failed to connect to HA WebSocket")
    ):
        await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )
        await _drain_background_tasks()

    assert len(client.submit_calls) == 1
    # find used the legacy config_entries/get bridge (the pooled read socket was
    # down), not the in-process component server_entry read.
    assert client.config_entries_get_calls == 1


@pytest.mark.asyncio
async def test_non_embedded_never_routes_to_component_write(
    monkeypatch: Any,
) -> None:
    """NON-embedded mode submits the options flow SYNCHRONOUSLY and never sends the
    server_entry_update frame, even though the capability is advertised."""
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    ws = _update_ws(
        caps=_CAPS_FULL,
        update_result={"scheduled": True, "entry_id": "srv1"},
    )
    client = UpdateClient()

    with patch_ws(ws, tools_dev):
        result = await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )

    # Synchronous legacy submit (non-embedded returns "applied", not "scheduled").
    assert result["data"]["applied"] == {
        "server_url": "http://ha:8123",
        "channel": "dev",
    }
    assert len(client.submit_calls) == 1
    sent = [c.args[0] for c in ws.send_command.call_args_list]
    assert "ha_mcp_tools/server_entry_update" not in sent
