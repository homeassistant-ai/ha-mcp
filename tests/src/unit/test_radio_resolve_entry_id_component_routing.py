"""Routing tests for radio's ``resolve_entry_id`` over the config_entries gate.

``ha_manage_radio``'s single-instance resolvers map an integration ``domain`` to
its config ``entry_id`` via ``resolve_entry_id``, which dumped EVERY config entry
over the legacy ``config_entries/get`` WS read and filtered client-side. When the
component advertises ``config_entries`` the domain-scoped read now rides one
``ha_mcp_tools/config_entries`` frame (shared ``_fetch_entries_via_component``);
the legacy ``config_entries/get`` dump stays the fallback. These pin the seam:
component-hit (no legacy dump), legacy fallback on no-caps, ``unknown_command`` →
invalidate caps + legacy, a plain-``Exception`` establish failure → legacy, and
"integration not configured" → None.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import component_api, tools_integrations
from ha_mcp.tools.radio.base import resolve_entry_id

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

WS_CONFIG_ENTRIES = "ha_mcp_tools/config_entries"

_CAPS_CONFIG_ENTRIES = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["config_entries"],
    "limits": {},
}


def _entry(domain: str = "zwave_js", entry_id: str = "cfg-zwave") -> dict[str, Any]:
    """A ``config_entries/get``-shaped row as the component's ``entries`` element."""
    return {"entry_id": entry_id, "domain": domain, "title": f"Title {domain}"}


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy ``config_entries/get`` dumps."""

    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._entries = list(entries or [])
        self.entries_get_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "config_entries/get":
            self.entries_get_calls += 1
            return {"success": True, "result": list(self._entries)}
        raise AssertionError(f"unexpected ws message {msg.get('type')!r}")


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _entries_calls(ws: Any) -> list[Any]:
    return [c for c in ws.send_command.call_args_list if c.args[0] == WS_CONFIG_ENTRIES]


@pytest.mark.asyncio
async def test_resolve_entry_id_served_by_component() -> None:
    """The domain read rides one config_entries frame; the legacy dump never runs."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_entry()]},
    )
    client = RoutingClient()

    with patch_ws(ws, tools_integrations):
        entry_id = await resolve_entry_id(client, "zwave_js")

    assert entry_id == "cfg-zwave"
    assert client.entries_get_calls == 0
    calls = _entries_calls(ws)
    assert len(calls) == 1
    assert calls[0].kwargs["domain"] == "zwave_js"


@pytest.mark.asyncio
async def test_resolve_entry_id_capsless_uses_legacy_dump() -> None:
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient(entries=[_entry()])

    with patch_ws(ws, tools_integrations):
        entry_id = await resolve_entry_id(client, "zwave_js")

    assert entry_id == "cfg-zwave"
    assert client.entries_get_calls == 1
    assert not _entries_calls(ws)


@pytest.mark.asyncio
async def test_resolve_entry_id_unknown_command_invalidates_and_falls_back() -> None:
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(entries=[_entry()])

    with patch_ws(ws, tools_integrations):
        entry_id = await resolve_entry_id(client, "zwave_js")

    assert entry_id == "cfg-zwave"
    assert client.entries_get_calls == 1
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_resolve_entry_id_establish_failure_falls_back_to_legacy() -> None:
    """A plain-``Exception`` WS establish failure after caps cached → legacy dump.

    The config_entries legacy path is the REST-client WS bridge (never raises),
    NOT the shared pooled WS, so the establish failure must route to legacy.
    """
    caps_ws = make_ws(WS_CONFIG_ENTRIES, info_result=_CAPS_CONFIG_ENTRIES)
    client = RoutingClient(entries=[_entry()])

    with patch_ws_establish_failure(
        caps_ws, tools_integrations, RuntimeError("WS establish failed")
    ):
        entry_id = await resolve_entry_id(client, "zwave_js")

    assert entry_id == "cfg-zwave"
    assert client.entries_get_calls == 1


@pytest.mark.asyncio
async def test_resolve_entry_id_not_configured_returns_none() -> None:
    """Component serves an authoritative empty entries list → no match → None."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": []},
    )
    client = RoutingClient()

    with patch_ws(ws, tools_integrations):
        entry_id = await resolve_entry_id(client, "zwave_js")

    assert entry_id is None
    assert client.entries_get_calls == 0
    assert len(_entries_calls(ws)) == 1
