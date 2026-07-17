"""Routing tests for ``ha_remove_helpers_integrations`` (+ the set_helper wait
poll) over the ``ha_mcp_tools`` component ``registry_lookup`` gate.

Two helper paths resolve entities from the registry today:

- The SIMPLE-helper delete resolves one entity's ``unique_id`` with a 3-attempt
  exponential-backoff ``config/entity_registry/get`` loop. When the component
  advertises ``registry_lookup``, ONE ``registry_lookup(entity_ids=[...])`` read
  resolves it — the single read plus the direct-id fallback replaces the retry
  loop (the loop absorbed registration LAG, not a WS-timing race), so the retry
  loop and its sleeps are skipped entirely.
- The FLOW-helper delete and the ``ha_config_set_helper`` post-create wait poll
  both need EVERY entity for one ``config_entry_id`` and get them by dumping the
  WHOLE ``config/entity_registry/list``. ``registry_lookup(config_entry_id=...)``
  returns them scoped in one frame instead.

These pin that: the component-served resolves never dump the registry NOR run the
retry-loop sleeps, and every backend degradation (no caps, ``unknown_command`` →
invalidate + fall back, a non-unknown command error, a malformed reply, AND a
transport failure — both a ``HomeAssistantConnectionError`` off the frame and a
plain ``Exception`` from ``get_websocket_client()`` failing to establish the
socket) still produces the byte-identical legacy result. The registry_lookup
consumers' legacy reads ride the swallowing ``send_websocket_message`` bridge, so a
transport failure falls back rather than propagating — and, crucially, letting it
escape the SIMPLE resolve would skip its legacy retry loop entirely.
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
from ha_mcp.tools import component_api, component_registry_lookup
from ha_mcp.tools.tools_config_helpers import (
    _get_entities_for_config_entry,
    _wait_for_flow_entities,
)
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

_CAPS_REGISTRY = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["registry_lookup"],
    "limits": {},
}


def _row(
    entity_id: str, *, unique_id: str, config_entry_id: str | None = None
) -> dict[str, Any]:
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
                (
                    e
                    for e in self._entities
                    if e.get("entity_id") == msg.get("entity_id")
                ),
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


class DeleteFailsRoutingClient(RoutingClient):
    """Like ``RoutingClient``, but every ``*/delete`` fails — drives the
    exhausted-fallback branch (direct-id delete also fails, entity still
    present in state) instead of the happy-path direct-id success."""

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        if isinstance(msg_type, str) and msg_type.endswith("/delete"):
            self.delete_calls.append(msg)
            return {"success": False, "error": "not found"}
        return await super().send_websocket_message(msg)


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
        patch_ws(ws, component_registry_lookup),
        patch(
            "ha_mcp.tools.tools_integrations.asyncio.sleep", new_callable=AsyncMock
        ) as sleep_mock,
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
    # Component read replaces the retry loop → no backoff sleep during resolve.
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

    with patch_ws(ws, component_registry_lookup):
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

    with patch_ws(ws, component_registry_lookup):
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

    with patch_ws(ws, component_registry_lookup):
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
async def test_simple_delete_connection_error_falls_back_to_legacy() -> None:
    """A HomeAssistantConnectionError from the component resolve falls back to the
    legacy retry loop (which rides the swallowing bridge, so it does not die
    identically) rather than propagating — an escaping error would SKIP the legacy
    resolve entirely."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantConnectionError("socket down"),
    )
    client = RoutingClient(
        entities=[_row("input_button.my_button", unique_id="uid-legacy")]
    )
    tools = IntegrationTools(client)

    with patch_ws(ws, component_registry_lookup):
        resp = await tools.ha_remove_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    assert resp["success"] is True
    assert resp["unique_id"] == "uid-legacy"
    assert client.entity_get_calls == 1
    # A transient connection error is not a downgrade — caps stay cached.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_simple_delete_ws_establish_failure_falls_back_to_legacy() -> None:
    """A plain establish ``Exception`` from ``get_websocket_client()`` (after caps
    are cached) falls back to the legacy retry loop, not a propagated error."""
    caps_ws = make_ws("ha_mcp_tools/registry_lookup", info_result=_CAPS_REGISTRY)
    client = RoutingClient(
        entities=[_row("input_button.my_button", unique_id="uid-legacy")]
    )
    tools = IntegrationTools(client)

    with patch_ws_establish_failure(
        caps_ws,
        component_registry_lookup,
        Exception("Failed to connect to Home Assistant WebSocket"),
    ):
        resp = await tools.ha_remove_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    assert resp["success"] is True
    assert resp["unique_id"] == "uid-legacy"
    assert client.entity_get_calls == 1
    assert client in component_api._CAPS_CACHE


@pytest.mark.parametrize("falsy_unique_id", ["", None])
@pytest.mark.asyncio
async def test_simple_delete_falsy_unique_id_degrades_like_missing(
    falsy_unique_id: str | None,
) -> None:
    """A component-served row where the entity IS registered but ``unique_id``
    is falsy (empty string or None) must degrade EXACTLY like the missing-entity
    case: no usable id is resolved, so the direct-id fallback runs, and when
    that also fails with the entity still present in state, the SAME
    ENTITY_NOT_FOUND classification as the legacy exhausted-fallback path
    (test_simple_path_all_fallbacks_exhausted) is raised — with path-accurate
    wording naming the component lookup rather than "3 attempts"."""
    row = {
        "entity_id": "input_button.my_button",
        "unique_id": falsy_unique_id,
        "config_entry_id": None,
    }
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_result={"entities": [row], "missing": []},
    )
    client = DeleteFailsRoutingClient()
    tools = IntegrationTools(client)

    with patch_ws(ws, component_registry_lookup), pytest.raises(ToolError) as excinfo:
        await tools.ha_remove_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    err = json.loads(str(excinfo.value))
    assert err["error"]["code"] == "ENTITY_NOT_FOUND"
    message = err["error"]["message"]
    assert "Component registry lookup" in message
    # The direct-id fallback ran (using the bare id, no unique_id resolved).
    assert client.delete_calls[0]["input_button_id"] == "my_button"
    # Path-accurate: never claims a 3-attempt retry loop that never ran.
    assert "3 attempts" not in message
    # The component row served the resolve; no legacy per-id get ran.
    assert client.entity_get_calls == 0


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

    with patch_ws(ws, component_registry_lookup):
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

    with patch_ws(ws, component_registry_lookup):
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
        patch_ws(ws, component_registry_lookup),
        patch(
            "ha_mcp.tools.tools_config_helpers.asyncio.sleep", new_callable=AsyncMock
        ) as sleep_mock,
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


# --- component_registry_lookup seam functions (direct; error taxonomy) -----------------


@pytest.mark.asyncio
async def test_config_entry_lookup_unknown_command_invalidates_caps() -> None:
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient()
    with patch_ws(ws, component_registry_lookup):
        assert (
            await component_registry_lookup.fetch_entities_for_config_entry_via_component(
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
    with patch_ws(ws, component_registry_lookup):
        assert (
            await component_registry_lookup.fetch_entities_for_config_entry_via_component(
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
    with patch_ws(ws, component_registry_lookup):
        assert (
            await component_registry_lookup.fetch_entities_for_config_entry_via_component(
                client, "um_entry"
            )
            is None
        )


@pytest.mark.asyncio
async def test_get_entities_component_connection_error_falls_back_to_legacy() -> None:
    """A component transport failure rides the seam's fallback, not the warnings.

    Under the uniform transport taxonomy the seam maps a
    ``HomeAssistantConnectionError`` to ``None``, so
    ``_get_entities_for_config_entry`` reads the legacy
    ``config/entity_registry/list`` dump (the swallowing bridge) and returns its
    rows with NO warning — the flow-helper delete proceeds exactly as on a
    component-less install."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantConnectionError("socket down"),
    )
    client = RoutingClient(
        entities=[
            {"entity_id": "sensor.um", "config_entry_id": "um_entry"},
        ]
    )
    warnings: list[str] = []

    with patch_ws(ws, component_registry_lookup):
        result = await _get_entities_for_config_entry(client, "um_entry", warnings)

    assert result == [{"entity_id": "sensor.um", "config_entry_id": "um_entry"}]
    assert warnings == []
    assert client.entity_list_calls == 1


@pytest.mark.asyncio
async def test_get_entities_unexpected_seam_error_converted_to_warnings() -> None:
    """(F8) A failure that DOES escape the seam is converted, with honest attribution.

    Transport/command failures no longer escape the seam, but a genuinely
    unexpected error (programming fault) still must not abort the flow-helper
    delete: ``_get_entities_for_config_entry`` converts it into ``warnings`` +
    ``[]``, and the warning names the read that actually failed
    (``registry_lookup``, not ``entity_registry/list``)."""
    client = RoutingClient()
    warnings: list[str] = []

    # Patch the CONSUMER module's binding (tools_config_helpers imports the
    # function directly), not the source module's.
    with patch(
        "ha_mcp.tools.tools_config_helpers"
        ".fetch_entities_for_config_entry_via_component",
        side_effect=RuntimeError("boom"),
    ):
        result = await _get_entities_for_config_entry(client, "um_entry", warnings)

    assert result == []
    assert len(warnings) == 1
    assert "registry_lookup failed for config_entry_id=um_entry" in warnings[0]
    assert client.entity_list_calls == 0


@pytest.mark.asyncio
async def test_resolve_entities_unknown_command_invalidates_caps() -> None:
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient()
    with patch_ws(ws, component_registry_lookup):
        assert (
            await component_registry_lookup.resolve_entities_via_component(
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
    with patch_ws(ws, component_registry_lookup):
        assert (
            await component_registry_lookup.resolve_entities_via_component(
                client, ["input_button.x"]
            )
            is None
        )


@pytest.mark.asyncio
async def test_resolve_entities_connection_error_falls_back() -> None:
    """The seam catches a connection error and returns ``None`` (legacy fallback)
    so the caller's legacy per-id resolve runs — the legacy read rides the
    swallowing bridge, so it does not die identically."""
    ws = make_ws(
        "ha_mcp_tools/registry_lookup",
        info_result=_CAPS_REGISTRY,
        cmd_exc=HomeAssistantConnectionError("socket down"),
    )
    client = RoutingClient()
    with patch_ws(ws, component_registry_lookup):
        assert (
            await component_registry_lookup.resolve_entities_via_component(
                client, ["input_button.x"]
            )
            is None
        )


@pytest.mark.asyncio
async def test_resolve_entities_ws_establish_failure_falls_back() -> None:
    """A plain establish ``Exception`` from ``get_websocket_client()`` (after caps
    are cached) returns ``None`` (legacy fallback)."""
    caps_ws = make_ws("ha_mcp_tools/registry_lookup", info_result=_CAPS_REGISTRY)
    client = RoutingClient()
    with patch_ws_establish_failure(
        caps_ws,
        component_registry_lookup,
        Exception("Failed to connect to Home Assistant WebSocket"),
    ):
        assert (
            await component_registry_lookup.resolve_entities_via_component(
                client, ["input_button.x"]
            )
            is None
        )
