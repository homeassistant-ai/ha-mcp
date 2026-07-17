"""Routing tests for ``ha_remove_helpers_integrations`` (+ the set_helper wait
poll) over the ``ha_mcp_tools`` component ``registry_lookup`` gate.

Two helper paths resolve entities from the registry today:

- The SIMPLE-helper delete resolves one entity's ``unique_id`` with a 3-attempt
  exponential-backoff ``config/entity_registry/get`` loop. When the component
  advertises ``registry_lookup``, ONE ``registry_lookup(entity_ids=[...])`` read
  resolves it — an in-process read has no WS-timing race, so the retry loop (and
  its sleeps) is skipped entirely.
- The FLOW-helper delete and the ``ha_config_set_helper`` post-create wait poll
  both need EVERY entity for one ``config_entry_id`` and get them by dumping the
  WHOLE ``config/entity_registry/list``. ``registry_lookup(config_entry_id=...)``
  returns them scoped in one frame instead.

These pin that: the component-served resolves never dump the registry NOR run the
retry-loop sleeps, and every backend degradation (no caps, ``unknown_command`` →
invalidate + fall back, a non-unknown command error, a malformed reply) still
produces the byte-identical legacy result. A ``HomeAssistantConnectionError``
propagates from the SIMPLE resolve (the legacy path shares the socket).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, component_registry
from ha_mcp.tools.tools_config_helpers import _wait_for_flow_entities
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._component_routing_helpers import make_ws, patch_ws

_CAPS_REGISTRY = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["registry_lookup"],
    "limits": {},
}


def _row(entity_id: str, *, unique_id: str, config_entry_id: str | None = None) -> dict[str, Any]:
    """A ``registry_lookup`` row (the ``config/entity_registry/list`` shape)."""
    return {
        "entity_id": entity_id,
        "unique_id": unique_id,
        "config_entry_id": config_entry_id,
    }


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy registry reads.

    ``entity_get_calls`` counts the SIMPLE path's per-id
    ``config/entity_registry/get`` reads; ``entity_list_calls`` counts the FLOW
    path's whole-registry ``config/entity_registry/list`` dumps. Both stay 0 when
    the component serves the read.
    """

    def __init__(
        self,
        *,
        get_result: dict[str, Any] | None = None,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        # Reply for the SIMPLE-path config/entity_registry/get + FLOW-path
        # _get_entry_id_for_flow_helper get (kept legacy, single targeted read).
        self._get_result = get_result
        self._entities = list(entities or [])
        self.entity_get_calls = 0
        self.entity_list_calls = 0
        self.delete_calls: list[dict[str, Any]] = []
        self.deleted_entries: list[str] = []

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        return {"state": "on"}

    async def delete_config_entry(self, entry_id: str) -> dict[str, Any]:
        self.deleted_entries.append(entry_id)
        return {"require_restart": False}

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        if msg_type == "config/entity_registry/get":
            self.entity_get_calls += 1
            if self._get_result is not None:
                return self._get_result
            row = next(
                (e for e in self._entities if e.get("entity_id") == msg.get("entity_id")),
                None,
            )
            if row is None:
                return {"success": False, "error": "not found"}
            return {"success": True, "result": row}
        if msg_type == "config/entity_registry/list":
            self.entity_list_calls += 1
            return {"success": True, "result": list(self._entities)}
        if isinstance(msg_type, str) and msg_type.endswith("/delete"):
            self.delete_calls.append(msg)
            return {"success": True}
        raise AssertionError(f"unexpected ws message {msg_type!r}")


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()


def _lookup_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/registry_lookup"
    ]


# --- SIMPLE-helper delete: registry_lookup(entity_ids=...) resolve --------------


@pytest.mark.asyncio
async def test_simple_delete_resolves_via_component_no_retry_loop() -> None:
    """A component-served resolve reads the unique_id in ONE
    registry_lookup(entity_ids) frame — no per-id config/entity_registry/get and
    no retry-loop sleep."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_result={
            "entities": [_row("input_button.my_button", unique_id="uid-1")],
            "missing": [],
        },
    )
    client = RoutingClient()
    tools = IntegrationTools(client)

    with (
        patch_ws(ws, component_registry),
        patch("ha_mcp.tools.tools_integrations.asyncio.sleep", new_callable=AsyncMock) as sleep_mock,
    ):
        resp = await tools.ha_remove_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    assert resp["success"] is True
    assert resp["method"] == "websocket_delete"
    assert resp["unique_id"] == "uid-1"
    assert resp["entity_ids"] == ["input_button.my_button"]
    # Resolved from the component; the legacy per-id get never ran.
    assert client.entity_get_calls == 0
    lookups = _lookup_calls(ws)
    assert len(lookups) == 1
    assert lookups[0].kwargs["entity_ids"] == ["input_button.my_button"]
    # The delete used the component-resolved unique_id.
    assert client.delete_calls[0]["input_button_id"] == "uid-1"
    # No WS-timing race → no backoff sleep during resolve.
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_simple_delete_capability_miss_uses_legacy_loop() -> None:
    """Old component (info unknown_command) → legacy config/entity_registry/get
    resolve; no registry_lookup frame is sent."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient(
        entities=[_row("input_button.my_button", unique_id="uid-legacy")]
    )
    tools = IntegrationTools(client)

    with patch_ws(ws, component_registry):
        resp = await tools.ha_remove_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    assert resp["success"] is True
    assert resp["unique_id"] == "uid-legacy"
    assert client.entity_get_calls == 1
    assert _lookup_calls(ws) == []


@pytest.mark.asyncio
async def test_simple_delete_unknown_command_invalidates_and_falls_back() -> None:
    """unknown_command on registry_lookup → invalidate caps + legacy resolve."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(
        entities=[_row("input_button.my_button", unique_id="uid-legacy")]
    )
    tools = IntegrationTools(client)

    with patch_ws(ws, component_registry):
        resp = await tools.ha_remove_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    assert resp["success"] is True
    assert resp["unique_id"] == "uid-legacy"
    # The component command was tried, failed unknown_command → legacy get.
    assert client.entity_get_calls == 1
    # Caps invalidated so the next call re-probes.
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_simple_delete_command_error_falls_back_keeps_caps() -> None:
    """A non-unknown registry_lookup error (timeout) → legacy resolve WITHOUT
    invalidating the still-advertised capability."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantCommandTimeout("slow"),
    )
    client = RoutingClient(
        entities=[_row("input_button.my_button", unique_id="uid-legacy")]
    )
    tools = IntegrationTools(client)

    with patch_ws(ws, component_registry):
        resp = await tools.ha_remove_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    assert resp["success"] is True
    assert resp["unique_id"] == "uid-legacy"
    assert client.entity_get_calls == 1
    # A transient failure is not a downgrade — caps stay cached.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_simple_delete_connection_error_propagates() -> None:
    """A HomeAssistantConnectionError from the component resolve is NOT swallowed
    into ENTITY_NOT_FOUND — it reaches the outer handler as CONNECTION_FAILED
    (the legacy path shares the socket and would fail identically)."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantConnectionError("socket down"),
    )
    client = RoutingClient()
    tools = IntegrationTools(client)

    with patch_ws(ws, component_registry), pytest.raises(ToolError) as excinfo:
        await tools.ha_remove_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    err = json.loads(str(excinfo.value))
    assert err["error"]["code"] == "CONNECTION_FAILED"
    assert err["error"]["code"] != "ENTITY_NOT_FOUND"


# --- FLOW-helper delete: registry_lookup(config_entry_id=...) sub-entities -------


@pytest.mark.asyncio
async def test_flow_delete_subentities_via_component_no_dump() -> None:
    """The FLOW delete collects ALL sub-entities from one
    registry_lookup(config_entry_id) frame — the whole-registry list is never
    dumped."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_result={
            "entities": [
                _row("sensor.energy_peak", unique_id="p", config_entry_id="um_entry"),
                _row(
                    "sensor.energy_offpeak", unique_id="o", config_entry_id="um_entry"
                ),
                _row("select.energy_tariff", unique_id="t", config_entry_id="um_entry"),
            ]
        },
    )
    # Step 1 (_get_entry_id_for_flow_helper) stays legacy: a single targeted get.
    client = RoutingClient(
        get_result={"success": True, "result": {"config_entry_id": "um_entry"}}
    )
    tools = IntegrationTools(client)

    with patch_ws(ws, component_registry):
        resp = await tools.ha_remove_helpers_integrations(
            target="sensor.energy_peak",
            helper_type="utility_meter",
            confirm=True,
            wait=False,
        )

    assert resp["success"] is True
    assert resp["method"] == "config_flow_delete"
    assert resp["entry_id"] == "um_entry"
    assert set(resp["entity_ids"]) == {
        "sensor.energy_peak",
        "sensor.energy_offpeak",
        "select.energy_tariff",
    }
    # Sub-entities came from the component; the whole registry was never dumped.
    assert client.entity_list_calls == 0
    lookups = _lookup_calls(ws)
    assert len(lookups) == 1
    assert lookups[0].kwargs["config_entry_id"] == "um_entry"
    assert client.deleted_entries == ["um_entry"]


@pytest.mark.asyncio
async def test_flow_delete_capability_miss_uses_legacy_dump() -> None:
    """Old component → the FLOW delete falls back to the whole-registry dump and
    still finds every sub-entity."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient(
        get_result={"success": True, "result": {"config_entry_id": "um_entry"}},
        entities=[
            _row("sensor.energy_peak", unique_id="p", config_entry_id="um_entry"),
            _row("sensor.energy_offpeak", unique_id="o", config_entry_id="um_entry"),
            # Noise on a different entry — excluded by the client-side filter.
            _row("light.other", unique_id="x", config_entry_id="other"),
        ],
    )
    tools = IntegrationTools(client)

    with patch_ws(ws, component_registry):
        resp = await tools.ha_remove_helpers_integrations(
            target="sensor.energy_peak",
            helper_type="utility_meter",
            confirm=True,
            wait=False,
        )

    assert resp["success"] is True
    assert set(resp["entity_ids"]) == {"sensor.energy_peak", "sensor.energy_offpeak"}
    # The legacy whole-registry list served the sub-entities.
    assert client.entity_list_calls == 1
    assert _lookup_calls(ws) == []


# --- set_helper post-create wait poll rides the same shared helper --------------


@pytest.mark.asyncio
async def test_set_helper_wait_resolves_via_component_no_dump_no_sleep() -> None:
    """The ha_config_set_helper create+wait poll resolves the new entry's entities
    from one registry_lookup on the FIRST poll — no whole-registry dump and no
    poll sleep."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_result={
            "entities": [
                _row("sensor.energy_peak", unique_id="p", config_entry_id="um_entry"),
                _row(
                    "sensor.energy_offpeak", unique_id="o", config_entry_id="um_entry"
                ),
            ]
        },
    )
    client = RoutingClient()

    with (
        patch_ws(ws, component_registry),
        patch("ha_mcp.tools.tools_config_helpers.asyncio.sleep", new_callable=AsyncMock) as sleep_mock,
    ):
        entities, warnings = await _wait_for_flow_entities(
            client, "um_entry", "create", wait=True
        )

    assert {e["entity_id"] for e in entities} == {
        "sensor.energy_peak",
        "sensor.energy_offpeak",
    }
    assert warnings == []
    assert client.entity_list_calls == 0
    assert len(_lookup_calls(ws)) == 1
    # Resolved on the first poll → the graduated-poll sleep never fired.
    sleep_mock.assert_not_awaited()


# --- component_registry seam functions (direct; error taxonomy) -----------------


@pytest.mark.asyncio
async def test_config_entry_lookup_unknown_command_invalidates_caps() -> None:
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient()
    with patch_ws(ws, component_registry):
        assert (
            await component_registry.fetch_entities_for_config_entry_via_component(
                client, "um_entry"
            )
            is None
        )
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_config_entry_lookup_timeout_keeps_caps() -> None:
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantCommandTimeout("slow"),
    )
    client = RoutingClient()
    with patch_ws(ws, component_registry):
        assert (
            await component_registry.fetch_entities_for_config_entry_via_component(
                client, "um_entry"
            )
            is None
        )
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_config_entry_lookup_malformed_shape_falls_back() -> None:
    """A reply whose ``entities`` is not a list (shape drift) → None so the caller
    reads the legacy dump instead of trusting it."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_result={"entities": "not-a-list"},
    )
    client = RoutingClient()
    with patch_ws(ws, component_registry):
        assert (
            await component_registry.fetch_entities_for_config_entry_via_component(
                client, "um_entry"
            )
            is None
        )


@pytest.mark.asyncio
async def test_resolve_entities_unknown_command_invalidates_caps() -> None:
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient()
    with patch_ws(ws, component_registry):
        assert (
            await component_registry.resolve_entities_via_component(
                client, ["input_button.x"]
            )
            is None
        )
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_resolve_entities_malformed_shape_falls_back() -> None:
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_result={"missing": ["input_button.x"]},  # no "entities" key
    )
    client = RoutingClient()
    with patch_ws(ws, component_registry):
        assert (
            await component_registry.resolve_entities_via_component(
                client, ["input_button.x"]
            )
            is None
        )


@pytest.mark.asyncio
async def test_resolve_entities_connection_error_propagates() -> None:
    """The seam does NOT catch a connection error — it propagates so the caller's
    legacy path (sharing the socket) surfaces the transport failure."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantConnectionError("socket down"),
    )
    client = RoutingClient()
    with (
        patch_ws(ws, component_registry),
        pytest.raises(HomeAssistantConnectionError),
    ):
        await component_registry.resolve_entities_via_component(
            client, ["input_button.x"]
        )
