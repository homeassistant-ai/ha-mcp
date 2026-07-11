"""Routing tests for ``ha_get_overview`` over the ``ha_mcp_tools`` component gate.

When the component advertises the ``overview`` capability, ``ha_get_overview``
fetches the eight raw reads (states + services + the three registries + config +
notifications + repairs) in one ``ha_mcp_tools/overview`` WebSocket call and
feeds those slices into its unchanged assembly, skipping the legacy ~8-fetch
pipeline. These tests pin that fast path, the error-taxonomy fallbacks (silent
on ``unknown_command``; legacy + ``warnings[]`` on any other command error), the
active-visibility bypass (an active filter keeps the legacy path), and
byte-identical parity between the two serving paths (same assembly, different
data source).

Harness mirrors ``test_ha_search_component_routing.py``: the WS client is an
``AsyncMock`` whose ``send_command`` dispatches on the command type, and the HA
client is a spy that tallies every legacy fetch so a test can assert none ran on
the component path.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import tools_search
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import register_search_tools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

from ._component_routing_helpers import make_ws, patch_ws

_STATES = [
    {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"friendly_name": "Kitchen"},
    },
    {
        "entity_id": "sensor.kitchen_temp",
        "state": "21",
        "attributes": {"friendly_name": "Kitchen Temp", "device_class": "temperature"},
    },
]
_SERVICES = [{"domain": "light", "services": {"turn_on": {}, "turn_off": {}}}]
_AREA_REGISTRY = {
    "success": True,
    "result": [{"area_id": "kitchen", "name": "Kitchen"}],
}
_ENTITY_REGISTRY = {
    "success": True,
    "result": [
        {"entity_id": "light.kitchen", "area_id": "kitchen", "entity_category": None},
        {
            "entity_id": "sensor.kitchen_temp",
            "area_id": "kitchen",
            "entity_category": None,
        },
    ],
}
_CONFIG = {
    "version": "2026.7.0",
    "location_name": "Home",
    "time_zone": "UTC",
    "language": "en",
    "state": "RUNNING",
}

_CAPS_OVERVIEW = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["overview"],
    "limits": {},
}


class OverviewRoutingClient:
    """Credentialed HA client spy: tallies every legacy fetch ha_get_overview makes."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.get_states_calls = 0
        self.get_services_calls = 0
        self.get_config_calls = 0
        self.ws_types: Counter[str] = Counter()

    async def get_states(self) -> list[dict[str, Any]]:
        self.get_states_calls += 1
        return [dict(s) for s in _STATES]

    async def get_services(self) -> list[dict[str, Any]]:
        self.get_services_calls += 1
        return [dict(s) for s in _SERVICES]

    async def get_config(self) -> dict[str, Any]:
        self.get_config_calls += 1
        return dict(_CONFIG)

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type", "")
        self.ws_types[msg_type] += 1
        if msg_type == "config/area_registry/list":
            return _AREA_REGISTRY
        if msg_type == "config/entity_registry/list":
            return _ENTITY_REGISTRY
        if msg_type == "config/device_registry/list":
            return {"success": True, "result": []}
        if msg_type == "persistent_notification/get":
            return {"success": True, "result": []}
        if msg_type == "repairs/list_issues":
            return {"success": True, "result": {"issues": []}}
        return {"success": False}

    def total_legacy_fetches(self) -> int:
        return (
            self.get_states_calls
            + self.get_services_calls
            + self.get_config_calls
            + sum(self.ws_types.values())
        )


def _build_overview_tool(client: Any) -> Any:
    mcp = MagicMock()
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp.add_tool = capture_add_tool
    register_search_tools(mcp, client, smart_tools=SmartSearchTools(client=client))
    return registered["ha_get_overview"]


def _setup_visibility_disabled(tmp_path: Any, monkeypatch: Any) -> None:
    save_visibility_config(tmp_path, VisibilityConfig(enabled=False))
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)


def _quiet_tail(monkeypatch: Any) -> None:
    """Make the wrapper's server-side-only tail deterministic + I/O-free.

    ``ha_mcp_update`` is already off (the unit conftest sets
    ``HA_MCP_DISABLE_UPDATE_CHECK``); pin the sidecar / HTTP-mount lookups to
    absent so a real settings sidecar on the host machine can't leak a
    ``settings_url`` into the assertions.
    """
    monkeypatch.setattr("ha_mcp.stdio_settings_sidecar.read_sidecar_url", lambda: None)
    monkeypatch.setattr("ha_mcp.settings_ui.get_http_settings_prefix", lambda: None)


def _overview_slices() -> dict[str, Any]:
    """The component's BARE ``ha_mcp_tools/overview`` slices for the fixture data.

    Registries are bare lists (no ``{success, result}`` WS wrapper), config is
    the bare ``get_config()`` dict, notifications/repairs are bare lists —
    exactly the raw-slice response design § ha_mcp_tools/overview specifies. The
    server wraps and assembles them into the overview envelope.
    """
    return {
        "states": [dict(s) for s in _STATES],
        "services": [dict(s) for s in _SERVICES],
        "area_registry": [dict(a) for a in _AREA_REGISTRY["result"]],
        "entity_registry": [dict(e) for e in _ENTITY_REGISTRY["result"]],
        "device_registry": [],
        "config": dict(_CONFIG),
        "notifications": [],
        "repairs": [],
    }


@pytest.mark.asyncio
async def test_component_fast_path_skips_legacy_fetches(tmp_path, monkeypatch) -> None:
    """When the component serves overview, none of the ~8 legacy fetches run."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    _quiet_tail(monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/overview",
        info_result=_CAPS_OVERVIEW,
        cmd_result=_overview_slices(),
    )
    client = OverviewRoutingClient()
    overview = _build_overview_tool(client)

    with patch_ws(ws, tools_search):
        resp = await overview(detail_level="standard")

    assert resp["success"] is True
    assert resp["system_summary"]["total_entities"] == 2
    assert resp["system_info"]["version"] == "2026.7.0"
    # The legacy inventory is untouched: no states/services/config/registry/
    # notification/repairs fetches.
    assert client.total_legacy_fetches() == 0
    # Exactly one component overview command was issued.
    overview_calls = [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/overview"
    ]
    assert len(overview_calls) == 1


@pytest.mark.asyncio
async def test_caps_probed_once_across_overviews(tmp_path, monkeypatch) -> None:
    """The info probe is cached: two overviews, one ha_mcp_tools/info call."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    _quiet_tail(monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/overview",
        info_result=_CAPS_OVERVIEW,
        cmd_result=_overview_slices(),
    )
    client = OverviewRoutingClient()
    overview = _build_overview_tool(client)

    with patch_ws(ws, tools_search):
        await overview(detail_level="standard")
        await overview(detail_level="standard")

    info_calls = [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/info"
    ]
    assert len(info_calls) == 1


@pytest.mark.asyncio
async def test_unknown_command_falls_back_silently(tmp_path, monkeypatch) -> None:
    """unknown_command on the overview call → legacy path, no fallback warning."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    _quiet_tail(monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/overview",
        info_result=_CAPS_OVERVIEW,
        cmd_exc=HomeAssistantCommandError("Command failed: nope", "unknown_command"),
    )
    client = OverviewRoutingClient()
    overview = _build_overview_tool(client)

    with patch_ws(ws, tools_search):
        resp = await overview(detail_level="standard")

    assert resp["success"] is True
    # Legacy inventory served the request.
    assert client.get_states_calls == 1
    assert client.get_config_calls == 1
    assert client.ws_types["config/entity_registry/list"] == 1
    assert client.ws_types["repairs/list_issues"] == 1
    # Silent fallback: no component-failure warning.
    assert not any("served via legacy path" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_raised_command_falls_back_with_warning(tmp_path, monkeypatch) -> None:
    """A non-unknown command error → legacy path AND a warnings[] entry."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    _quiet_tail(monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/overview",
        info_result=_CAPS_OVERVIEW,
        cmd_exc=HomeAssistantCommandError("Command failed: boom", "internal_error"),
    )
    client = OverviewRoutingClient()
    overview = _build_overview_tool(client)

    with patch_ws(ws, tools_search):
        resp = await overview(detail_level="standard")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    assert any(
        "component overview path failed" in w and "served via legacy path" in w
        for w in resp["warnings"]
    )


@pytest.mark.asyncio
async def test_command_timeout_falls_back_with_warning(tmp_path, monkeypatch) -> None:
    """A component WS overview timeout → legacy path AND a warnings[] entry."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    _quiet_tail(monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/overview",
        info_result=_CAPS_OVERVIEW,
        cmd_exc=HomeAssistantCommandTimeout("Command timeout"),
    )
    client = OverviewRoutingClient()
    overview = _build_overview_tool(client)

    with patch_ws(ws, tools_search):
        resp = await overview(detail_level="standard")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    assert any(
        "component overview path failed" in w and "served via legacy path" in w
        for w in resp["warnings"]
    )


@pytest.mark.asyncio
async def test_malformed_slice_falls_back_with_warning(tmp_path, monkeypatch) -> None:
    """A required slice of the wrong type → legacy path AND a warnings[] entry.

    ``_build_overview_slices`` returns None on a malformed required slice, so a
    partial snapshot never serves a silently-degraded overview.
    """
    _setup_visibility_disabled(tmp_path, monkeypatch)
    _quiet_tail(monkeypatch)
    slices = _overview_slices()
    slices["states"] = "not a list"  # malformed required slice
    ws = make_ws("ha_mcp_tools/overview", info_result=_CAPS_OVERVIEW, cmd_result=slices)
    client = OverviewRoutingClient()
    overview = _build_overview_tool(client)

    with patch_ws(ws, tools_search):
        resp = await overview(detail_level="standard")

    assert resp["success"] is True
    # Legacy inventory served the request instead of the malformed slices.
    assert client.get_states_calls == 1
    assert any(
        "malformed slices" in w and "served via legacy path" in w
        for w in resp["warnings"]
    )


@pytest.mark.asyncio
async def test_slice_errors_falls_back_with_warning(tmp_path, monkeypatch) -> None:
    """A non-empty ``slice_errors`` list → legacy path AND a warnings[] entry."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    _quiet_tail(monkeypatch)
    slices = _overview_slices()  # every required slice well-formed...
    slices["slice_errors"] = ["entity_registry read failed"]  # ...but flagged bad
    ws = make_ws("ha_mcp_tools/overview", info_result=_CAPS_OVERVIEW, cmd_result=slices)
    client = OverviewRoutingClient()
    overview = _build_overview_tool(client)

    with patch_ws(ws, tools_search):
        resp = await overview(detail_level="standard")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    assert any(
        "malformed slices" in w and "served via legacy path" in w
        for w in resp["warnings"]
    )


@pytest.mark.asyncio
async def test_capsless_client_uses_legacy(tmp_path, monkeypatch) -> None:
    """info → unknown_command yields no caps, so overview runs the legacy path."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    _quiet_tail(monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/overview",
        info_exc=HomeAssistantCommandError(
            "Command failed: no info", "unknown_command"
        ),
    )
    client = OverviewRoutingClient()
    overview = _build_overview_tool(client)

    with patch_ws(ws, tools_search):
        resp = await overview(detail_level="standard")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    # The overview command must never be attempted without the capability.
    assert not any(
        c.args[0] == "ha_mcp_tools/overview" for c in ws.send_command.call_args_list
    )


@pytest.mark.asyncio
async def test_visibility_active_bypasses_component(tmp_path, monkeypatch) -> None:
    """An ACTIVE entity-visibility filter forces the legacy path.

    The filter is applied server-side over the slices either way, so this is a
    retained simplification (mirroring search): an active filter with
    ``respect_assist_exposure`` needs extra WS reads inside ``load_hidden_set``
    that the single overview round-trip doesn't carry, so a visibility-enabled
    install keeps the legacy path. Either way the denied entity is excluded from
    the counts / samples.
    """
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True,
            exclude_categories=[],
            deny_entity_ids=["light.kitchen"],
        ),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    _quiet_tail(monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/overview",
        info_result=_CAPS_OVERVIEW,
        cmd_result=_overview_slices(),
    )
    client = OverviewRoutingClient()
    overview = _build_overview_tool(client)

    with patch_ws(ws, tools_search):
        resp = await overview(detail_level="standard")

    # The component overview command must never run while the filter is active.
    assert not any(
        c.args[0] == "ha_mcp_tools/overview" for c in ws.send_command.call_args_list
    ), "component overview must not run while the visibility filter is active"
    # Legacy inventory served the request, and the denied entity is gone from
    # the (visibility-filtered) domain stats.
    assert client.get_states_calls == 1
    assert resp["system_summary"]["total_entities"] == 1
    assert "light" not in resp["domain_stats"]


@pytest.mark.asyncio
async def test_enabled_but_no_active_dimension_still_uses_component(
    tmp_path, monkeypatch
) -> None:
    """enabled=True with every dimension cleared hides nothing → component serves."""
    save_visibility_config(
        tmp_path, VisibilityConfig(enabled=True, exclude_categories=[])
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    _quiet_tail(monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/overview",
        info_result=_CAPS_OVERVIEW,
        cmd_result=_overview_slices(),
    )
    client = OverviewRoutingClient()
    overview = _build_overview_tool(client)

    with patch_ws(ws, tools_search):
        await overview(detail_level="standard")

    assert any(
        c.args[0] == "ha_mcp_tools/overview" for c in ws.send_command.call_args_list
    ), "no active hide dimension → component should still serve"
    assert client.total_legacy_fetches() == 0


@pytest.mark.asyncio
async def test_component_and_legacy_response_parity(tmp_path, monkeypatch) -> None:
    """The component (raw-slice) path is byte-identical to the legacy path.

    The component returns the same eight reads the legacy path fetches; the
    server feeds them into its unchanged assembly. Over identical fixture data
    the two final ha_get_overview responses must be equal — same assembly code,
    only the data source differs. Pins that the raw-slice adaptation
    (bare→wrapped registries/notifications/repairs + prefetched threading) drops
    or reshapes nothing.
    """
    _setup_visibility_disabled(tmp_path, monkeypatch)
    _quiet_tail(monkeypatch)

    # Legacy run (info → unknown_command ⇒ no caps ⇒ legacy per-read fetch path).
    ws_legacy = make_ws(
        "ha_mcp_tools/overview",
        info_exc=HomeAssistantCommandError(
            "Command failed: no info", "unknown_command"
        ),
    )
    client_legacy = OverviewRoutingClient()
    with patch_ws(ws_legacy, tools_search):
        legacy = await _build_overview_tool(client_legacy)(detail_level="standard")

    # Component run: the same eight reads, delivered as raw slices in one call.
    ws_component = make_ws(
        "ha_mcp_tools/overview",
        info_result=_CAPS_OVERVIEW,
        cmd_result=_overview_slices(),
    )
    client_component = OverviewRoutingClient()
    with patch_ws(ws_component, tools_search):
        component = await _build_overview_tool(client_component)(
            detail_level="standard"
        )

    assert component == legacy
    # And the component path did not touch the legacy fetch inventory.
    assert client_component.total_legacy_fetches() == 0
