"""Cross-seam contract test for the ``server_entry`` capability.

Pipes the REAL component ``_do_server_entry`` (driven against a ``FakeHass``
carrying both a "server" entry and a "services" entry — the two
``ha_mcp_tools``-domain entries the component can register) through the REAL
server-side ``find_server_config_entry``, so a vocabulary or shape drift
between the component's marker-based entry discrimination and the server's
consumption of it fails here rather than at runtime. Modeled on
``test_component_readapi_contract.py``'s ``_real_component_ws`` pattern.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools import component_api, tools_dev
from ha_mcp.tools.tools_dev import find_server_config_entry

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import FakeConfigEntry, FakeHass, wsapi


def _real_component_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock whose commands are served by the REAL component functions."""
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info()}
        if command_type == "ha_mcp_tools/server_entry":
            return {
                "success": True,
                "result": wsapi._do_server_entry(hass, dict(kwargs)),
            }
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


class _StubClient:
    """Credentialed client stub: real WS goes through the component mock; the
    (not component-routed) options-flow open is a local fake keyed by
    entry_id, mirroring what HA's REST API would return."""

    def __init__(self, flows: dict[str, dict[str, Any]]) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self._flows = dict(flows)
        self.start_options_flow_calls: list[str] = []

    async def start_options_flow(self, entry_id: str) -> dict[str, Any]:
        self.start_options_flow_calls.append(entry_id)
        return self._flows[entry_id]

    async def abort_options_flow(self, flow_id: str) -> dict[str, Any]:
        raise AssertionError(
            f"abort_options_flow({flow_id!r}) should not run: the component "
            "identified the entry directly, no wrong candidate to close"
        )

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(
            f"legacy config_entries/get should not run: {msg!r}"
        )


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.mark.asyncio
async def test_real_server_entry_picks_the_server_entry_over_services() -> None:
    """A FakeHass with a server entry AND a services entry: the real
    ``_do_server_entry`` picks the server one by its ``entry_type`` marker,
    and the real ``find_server_config_entry`` opens exactly its flow."""
    server_entry = FakeConfigEntry(
        wsapi.DOMAIN,
        entry_id="server-1",
        data={wsapi.CONF_ENTRY_TYPE: wsapi.ENTRY_TYPE_SERVER},
        options={wsapi.OPT_CHANNEL: "stable", wsapi.OPT_PIP_SPEC: None},
    )
    services_entry = FakeConfigEntry(
        wsapi.DOMAIN,
        entry_id="services-1",
        data={},
        options={},
    )
    hass = FakeHass(config_entries=[services_entry, server_entry])

    ws = _real_component_ws(hass)
    server_flow = {
        "flow_id": "flow-server-1",
        "type": "form",
        "data_schema": [
            {"name": "channel", "default": "stable"},
            {"name": "pip_spec", "description": {"suggested_value": None}},
        ],
    }
    client = _StubClient(flows={"server-1": server_flow})

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is not None
    entry_id, flow, current = found
    assert entry_id == "server-1"
    assert flow["flow_id"] == "flow-server-1"
    assert current["channel"] == "stable"
    # Exactly one flow was opened, for the entry the component identified —
    # the services entry was never touched (no abort, no probe).
    assert client.start_options_flow_calls == ["server-1"]


@pytest.mark.asyncio
async def test_real_server_entry_no_server_entry_returns_none() -> None:
    """A FakeHass with only a services entry: the real ``_do_server_entry``
    reports ``entry_id: None`` (authoritative) and ``find_server_config_entry``
    returns ``None`` without opening any options flow."""
    services_entry = FakeConfigEntry(
        wsapi.DOMAIN,
        entry_id="services-1",
        data={},
        options={},
    )
    hass = FakeHass(config_entries=[services_entry])

    ws = _real_component_ws(hass)
    client = _StubClient(flows={})

    with patch_ws(ws, tools_dev):
        found = await find_server_config_entry(client)

    assert found is None
    assert client.start_options_flow_calls == []
