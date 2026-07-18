"""Cross-seam contract test for the ``server_entry_update`` write capability.

Like ``test_component_call_service_contract.py``, this wires the REAL component
write functions (``_server_entry_update_prep`` -> ``_do_server_entry_update``,
driven against a component-side fake hass) underneath the mocked WS transport and
then invokes the REAL server ``ha_dev_manage_server(update_source)`` consumer — so a
shape drift on either side of the write seam fails here rather than shipping a
component-served reply the consumer mis-maps.

Two things are pinned end-to-end: the consumer maps the REAL component reply into
its ``scheduled:true`` data shape (never calling the legacy options-flow submit),
and driving the DEFERRED component task then applies ``async_update_entry`` with the
MERGED options (the channel delta applied, the server_url / pip_spec keys preserved).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools import component_api, tools_dev
from ha_mcp.tools.tools_dev import DevTools

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import FakeConfigEntry, wsapi


class _RecordingConfigEntries:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = list(entries)
        self.update_calls: list[tuple[Any, dict[str, Any] | None]] = []

    def async_entries(self) -> list[Any]:
        return list(self._entries)

    def async_update_entry(
        self, entry: Any, *, options: Any = None, **_kw: Any
    ) -> bool:
        self.update_calls.append(
            (entry, dict(options) if options is not None else None)
        )
        if options is not None:
            entry.options = dict(options)
        return True


class _ComponentHass:
    """Component-side hass: enumerates entries + CAPTURES the deferred apply task."""

    def __init__(self, entries: list[Any]) -> None:
        self.config_entries = _RecordingConfigEntries(entries)
        self.data: dict[str, Any] = {}
        self.scheduled: list[Any] = []

    def async_create_background_task(self, coro: Any, name: str | None = None) -> Any:
        self.scheduled.append(coro)
        return coro


def _real_update_ws(component_hass: _ComponentHass) -> AsyncMock:
    """A WS mock whose server_entry / server_entry_update frames run the REAL
    component functions against ``component_hass``."""
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info(component_hass)}
        if command_type == "ha_mcp_tools/server_entry":
            return {
                "success": True,
                "result": wsapi._do_server_entry(component_hass, dict(kwargs)),
            }
        if command_type == "ha_mcp_tools/server_entry_update":
            msg = {"type": wsapi.WS_SERVER_ENTRY_UPDATE, **kwargs}
            extra = await wsapi._server_entry_update_prep(component_hass, msg)
            return {
                "success": True,
                "result": wsapi._do_server_entry_update(component_hass, msg, **extra),
            }
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


class _ContractClient:
    """Credentialed client whose legacy surface is a tripwire.

    Component-first: on the fast path the consumer routes through the component write
    BEFORE find_server_config_entry, so no options flow is opened here — the
    start/abort methods exist only for the fallback path, and the submit / legacy
    config_entries/get methods raise if the component route is ever bypassed.
    """

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.abort_calls: list[str] = []

    async def start_options_flow(self, entry_id: str) -> dict[str, Any]:
        return {"flow_id": f"flow-{entry_id}", "type": "form", "data_schema": []}

    async def abort_options_flow(self, flow_id: str) -> dict[str, Any]:
        self.abort_calls.append(flow_id)
        return {}

    async def submit_options_flow_step(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("legacy options-flow submit must not run when routed")

    async def send_websocket_message(self, msg: dict[str, Any]) -> Any:
        raise AssertionError(f"legacy config_entries/get must not run: {msg!r}")


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.fixture(autouse=True)
def _embedded_fast(monkeypatch: Any) -> Any:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    monkeypatch.setattr(wsapi, "SERVER_ENTRY_UPDATE_FLUSH_DELAY_S", 0)
    yield


@pytest.mark.asyncio
async def test_real_component_write_maps_and_defers_merged_apply() -> None:
    """The real component prep, driven through the real update_source consumer:
    the consumer maps the scheduled reply (no legacy submit), and driving the
    deferred component task applies the MERGED options."""
    entry = FakeConfigEntry(
        domain="ha_mcp_tools",
        data={"entry_type": "server", "webhook_id": "secret"},
        options={"channel": "stable", "pip_spec": "", "server_url": "http://ha:8123"},
        entry_id="srv1",
    )
    component_hass = _ComponentHass([entry])
    ws = _real_update_ws(component_hass)
    client = _ContractClient()

    with patch_ws(ws, tools_dev):
        result = await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )

    data = result["data"]
    assert data["scheduled"] is True
    assert data["entry_id"] == "srv1"
    assert data["applying"] == {"channel": "dev"}
    assert data["previous"] == {"channel": "stable", "pip_spec": ""}
    # Component-first: the consumer tries the component write BEFORE opening the
    # legacy options flow, so no flow is opened — nothing to submit or abort.
    assert client.abort_calls == []

    # The component deferred the write; drive it and confirm the merged options.
    assert component_hass.config_entries.update_calls == []
    assert len(component_hass.scheduled) == 1
    await component_hass.scheduled[0]
    _entry, applied = component_hass.config_entries.update_calls[0]
    assert applied == {
        "channel": "dev",
        "pip_spec": "",
        "server_url": "http://ha:8123",
    }


@pytest.mark.asyncio
async def test_real_component_no_entry_falls_back_to_component_not_installed() -> None:
    """When the component reports no server entry AND the legacy probe also finds
    none, the consumer surfaces COMPONENT_NOT_INSTALLED. Component-first: the
    component write is attempted first and returns None (no entry), so
    find_server_config_entry runs SECOND and, also finding none, is what raises."""
    from fastmcp.exceptions import ToolError

    component_hass = _ComponentHass([])  # no server entry anywhere
    ws = _real_update_ws(component_hass)
    client = _ContractClient()

    with patch_ws(ws, tools_dev), pytest.raises(ToolError, match="server entry"):
        await DevTools(client).ha_dev_manage_server(
            action="update_source", channel="dev"
        )
    assert component_hass.scheduled == []
