"""Routing tests for ``ha_get_system_health`` over the ``ha_mcp_tools`` component gate.

``ha_get_system_health`` fetched ``config_entries/get`` up to THREE separate
times per call (once each inside ``_fetch_zwave_network``,
``_fetch_matter_network``, and ``_fetch_dead_entities``) plus a fourth
``repairs/list_issues`` fetch and a whole-registry ``config/entity_registry/list``
dump for ``dead_entities`` — a TOCTOU across sections, since each fetch could see
a different instant of the registries. When the component advertises
``system_snapshot``, ONE ``ha_mcp_tools/system_snapshot`` read replaces all of
that: its ``issues`` slice feeds ``_fetch_repairs``, its ``config_entries``
slice feeds ``_fetch_zwave_network`` + ``_fetch_matter_network`` (the
``zwave_js/network_status`` call itself is unchanged — system_snapshot doesn't
reach into the radio integration), and its ``entities``/``states``/
``config_entries`` slices feed ``_fetch_dead_entities``.

Routing is all-or-nothing per call: a capability miss, a component command
error/timeout, a malformed slice, OR a transport failure (both a
``HomeAssistantConnectionError`` off the frame and a plain ``Exception`` from
``get_websocket_client()`` failing to establish the socket) falls back to every
consuming section's own legacy fetch — never a partial hybrid where one section
reads the snapshot and a sibling reads legacy data from a different instant, and
never a WHOLE-tool failure where legacy would have degraded per-section (the
legacy sections use a dedicated health WS + REST + the never-raising bridge, not
the pooled snapshot socket). These tests pin: the snapshot is fetched exactly
once for a multi-section call and replaces every legacy fetch it covers; a
capability miss or snapshot failure degrades ALL consuming sections back to
legacy; ``unknown_command`` invalidates the cached caps while any other command
error/timeout leaves them cached; and a
still-real WS failure in a call the snapshot does NOT replace (the zwave_js
status call) still degrades to a per-section ``{error: ...}`` sub-dict rather
than raising or breaking sibling sections.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, tools_system
from ha_mcp.tools.tools_system import SystemTools

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

_CAPS_SNAPSHOT = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["system_snapshot"],
    "limits": {},
}


def _issue(
    issue_id: str,
    *,
    domain: str = "hue",
    dismissed_version: str | None = None,
    active: bool = True,
) -> dict[str, Any]:
    """A ``system_snapshot`` ``issues`` row (the ``_overview_repairs`` shape).

    ``active=False`` models a post-restart RESTORED placeholder: core's issue
    registry re-adds every previously-reported non-persistent issue as
    ``active=False`` with null severity/translation_key/is_fixable/issue_domain
    until the owning integration re-reports it, and core's ``repairs/list_issues``
    filters these out (``if issue.active``). The component's ``issues`` slice does
    NOT filter, so the server must — otherwise these phantoms surface only on the
    component path.
    """
    if not active:
        return {
            "issue_id": issue_id,
            "domain": domain,
            "severity": None,
            "translation_key": None,
            "translation_placeholders": {},
            "ignored": dismissed_version is not None,
            "dismissed_version": dismissed_version,
            "is_fixable": None,
            "breaks_in_ha_version": None,
            "created": "2026-01-01T00:00:00+00:00",
            "issue_domain": None,
            "learn_more_url": None,
            "active": False,
        }
    return {
        "issue_id": issue_id,
        "domain": domain,
        "severity": "warning",
        "translation_key": "tk",
        "translation_placeholders": {},
        "ignored": dismissed_version is not None,
        "dismissed_version": dismissed_version,
        "is_fixable": True,
        "breaks_in_ha_version": None,
        "created": "2026-01-01T00:00:00+00:00",
        "issue_domain": domain,
        "learn_more_url": None,
        "active": True,
    }


def _entry(
    entry_id: str, domain: str, *, state: str = "loaded", title: str = "Title"
) -> dict[str, Any]:
    """A ``system_snapshot`` ``config_entries`` row (identity fields only)."""
    return {
        "entry_id": entry_id,
        "domain": domain,
        "title": title,
        "state": state,
        "source": "user",
        "disabled_by": None,
    }


def _registry_row(
    entity_id: str,
    *,
    config_entry_id: str | None = None,
    platform: str = "hue",
    disabled_by: str | None = None,
) -> dict[str, Any]:
    """A ``system_snapshot`` ``entities`` row (``config/entity_registry/list`` shape)."""
    return {
        "entity_id": entity_id,
        "platform": platform,
        "config_entry_id": config_entry_id,
        "disabled_by": disabled_by,
    }


def _state(entity_id: str, state: str = "on", **attrs: Any) -> dict[str, Any]:
    """A ``system_snapshot`` ``states`` row (``State.as_dict()`` shape)."""
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy fetches ``system_snapshot`` replaces.

    Covers ``dead_entities``' three legacy sources (``get_states`` plus the
    per-client WebSocket bridge for the entity registry and config entries).
    """

    def __init__(
        self,
        *,
        states: list[dict[str, Any]] | None = None,
        registry: list[dict[str, Any]] | None = None,
        entries: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._states = list(states or [])
        self._registry = list(registry or [])
        self._entries = list(entries or [])
        self.get_states_calls = 0
        self.entity_registry_list_calls = 0
        self.config_entries_get_calls = 0

    async def get_states(self) -> list[dict[str, Any]]:
        self.get_states_calls += 1
        return list(self._states)

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        if msg_type == "config/entity_registry/list":
            self.entity_registry_list_calls += 1
            return {"success": True, "result": list(self._registry)}
        if msg_type == "config_entries/get":
            self.config_entries_get_calls += 1
            return {"success": True, "result": list(self._entries)}
        raise AssertionError(f"unexpected ws message {msg_type!r}")


def _health_ws(
    *,
    entries: list[dict[str, Any]] | None = None,
    repairs_issues: list[dict[str, Any]] | None = None,
    zwave_status: dict[str, Any] | None = None,
    zwave_status_exc: Exception | None = None,
) -> Any:
    """Stub for the SEPARATE ``system_health`` WS connection (repairs/zwave/matter).

    Distinct from the component WS ``get_websocket_client`` resolves —
    ``_fetch_health_info`` connects this one directly via
    ``get_connected_ws_client``, which routing tests bypass by patching
    ``_fetch_health_info`` itself (mirrors ``test_tools_system.py``'s
    ``_patch_health_info_baseline``).
    """
    ws = MagicMock()
    ws.disconnect = AsyncMock()
    ws.repairs_calls = 0
    ws.config_entries_get_calls = 0
    ws.zwave_status_calls = 0

    async def _send(command: str, **kwargs: Any) -> dict[str, Any]:
        if command == "repairs/list_issues":
            ws.repairs_calls += 1
            return {"success": True, "result": {"issues": list(repairs_issues or [])}}
        if command == "config_entries/get":
            ws.config_entries_get_calls += 1
            return {"success": True, "result": list(entries or [])}
        if command == "zwave_js/network_status":
            ws.zwave_status_calls += 1
            if zwave_status_exc is not None:
                raise zwave_status_exc
            return {
                "success": True,
                "result": zwave_status or {"controller": {"nodes": []}},
            }
        raise AssertionError(f"unexpected health command {command!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _health_baseline(ws_client: Any) -> Any:
    """Patch ``_fetch_health_info`` to hand back ``ws_client`` as the baseline."""
    return patch.object(
        SystemTools,
        "_fetch_health_info",
        new=AsyncMock(return_value=(ws_client, {"success": True, "health_info": {}})),
    )


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _snapshot_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/system_snapshot"
    ]


_INCLUDE_ALL = "repairs,zwave_network,matter_network,dead_entities"


@pytest.mark.asyncio
async def test_snapshot_fetched_once_for_multi_section_call() -> None:
    """One snapshot read serves repairs + zwave + matter + dead_entities; every
    legacy fetch it replaces never runs."""
    snapshot_result = {
        "config_entries": [
            _entry("cfg-zwave", "zwave_js"),
            _entry("cfg-matter", "matter", title="Matter Hub"),
        ],
        "issues": [_issue("iss-1")],
        "entities": [
            _registry_row("sensor.orphan", config_entry_id="cfg-gone"),
            _registry_row("sensor.alive", config_entry_id="cfg-zwave"),
        ],
        "states": [_state("sensor.orphan"), _state("sensor.alive")],
    }
    ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_result=_CAPS_SNAPSHOT,
        cmd_result=snapshot_result,
    )
    health_ws = _health_ws(
        zwave_status={"controller": {"name": "Z-Stick", "nodes": []}}
    )
    client = RoutingClient()

    with patch_ws(ws, tools_system), _health_baseline(health_ws):
        resp = await SystemTools(client).ha_get_system_health(include=_INCLUDE_ALL)

    assert len(_snapshot_calls(ws)) == 1

    # Every legacy fetch the snapshot replaces never ran.
    assert health_ws.repairs_calls == 0
    assert health_ws.config_entries_get_calls == 0
    assert client.get_states_calls == 0
    assert client.entity_registry_list_calls == 0
    assert client.config_entries_get_calls == 0
    # The zwave_js status call itself is NOT replaced by the snapshot.
    assert health_ws.zwave_status_calls == 1

    assert resp["repairs"]["issues"] == [_issue("iss-1")]
    assert resp["zwave_network"]["controller"]["name"] == "Z-Stick"
    assert resp["matter_network"] == {
        "config_entry_id": "cfg-matter",
        "state": "loaded",
        "title": "Matter Hub",
    }
    orphan_ids = {
        i["entity_id"] for i in resp["dead_entities"]["config_entry_orphans"]["items"]
    }
    assert orphan_ids == {"sensor.orphan"}


@pytest.mark.asyncio
async def test_single_section_call_requests_only_needed_slices() -> None:
    """A repairs-only call doesn't ask the component for entities/states."""
    ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_result=_CAPS_SNAPSHOT,
        cmd_result={
            "config_entries": [],
            "issues": [_issue("iss-1")],
            "entities": [],
            "states": [],
        },
    )
    health_ws = _health_ws()
    client = RoutingClient()

    with patch_ws(ws, tools_system), _health_baseline(health_ws):
        resp = await SystemTools(client).ha_get_system_health(include="repairs")

    calls = _snapshot_calls(ws)
    assert len(calls) == 1
    assert calls[0].kwargs == {
        "include_issues": True,
        "include_config_entries": False,
        "include_entities": False,
        "include_states": False,
    }
    assert resp["repairs"]["issues"] == [_issue("iss-1")]


@pytest.mark.asyncio
async def test_snapshot_repairs_drops_inactive_placeholder_issues() -> None:
    """The component ``issues`` slice carries EVERY registry issue including
    post-restart ``active=False`` placeholders; the repairs section must apply
    core's ``if issue.active`` filter so phantom null-severity issues never
    surface and never inflate count/dismissed_count."""
    active = _issue("iss-active")
    placeholder = _issue("iss-restored", active=False)
    ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_result=_CAPS_SNAPSHOT,
        cmd_result={
            "config_entries": [],
            "issues": [active, placeholder],
            "entities": [],
            "states": [],
        },
    )
    health_ws = _health_ws()
    client = RoutingClient()

    with patch_ws(ws, tools_system), _health_baseline(health_ws):
        resp = await SystemTools(client).ha_get_system_health(include="repairs")

    assert resp["repairs"]["issues"] == [active]
    assert resp["repairs"]["count"] == 1
    assert "dismissed_count" not in resp["repairs"]
    # Served from the snapshot; the legacy repairs read never ran.
    assert health_ws.repairs_calls == 0


@pytest.mark.asyncio
async def test_snapshot_repairs_parity_with_legacy_active_filter() -> None:
    """Component path (snapshot ``issues`` with an inactive placeholder) and the
    legacy path (``repairs/list_issues``, already active-filtered by core) produce
    byte-identical repairs output."""
    active = _issue("iss-active")
    placeholder = _issue("iss-restored", active=False)

    comp_ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_result=_CAPS_SNAPSHOT,
        cmd_result={
            "config_entries": [],
            "issues": [active, placeholder],
            "entities": [],
            "states": [],
        },
    )
    with patch_ws(comp_ws, tools_system), _health_baseline(_health_ws()):
        comp = await SystemTools(RoutingClient()).ha_get_system_health(
            include="repairs"
        )

    # Legacy: an old component never sends the snapshot; core's repairs read is
    # already active-filtered, so it returns only the active issue.
    legacy_ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    legacy_health = _health_ws(repairs_issues=[active])
    with patch_ws(legacy_ws, tools_system), _health_baseline(legacy_health):
        legacy = await SystemTools(RoutingClient()).ha_get_system_health(
            include="repairs"
        )

    assert legacy_health.repairs_calls == 1
    assert comp["repairs"] == legacy["repairs"]


@pytest.mark.asyncio
async def test_capability_miss_falls_back_to_legacy_per_section() -> None:
    """An old component (info unknown_command) never sends system_snapshot;
    every section runs its own legacy fetch."""
    ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    entries = [_entry("cfg-zwave", "zwave_js"), _entry("cfg-matter", "matter")]
    health_ws = _health_ws(
        entries=entries,
        repairs_issues=[_issue("iss-1")],
        zwave_status={"controller": {"nodes": []}},
    )
    client = RoutingClient(
        states=[_state("sensor.orphan")],
        registry=[_registry_row("sensor.orphan", config_entry_id="cfg-gone")],
        entries=entries,
    )

    with patch_ws(ws, tools_system), _health_baseline(health_ws):
        resp = await SystemTools(client).ha_get_system_health(include=_INCLUDE_ALL)

    assert not _snapshot_calls(ws)
    assert health_ws.repairs_calls == 1
    # Both zwave_network and matter_network independently resolve their own
    # config_entries/get in the legacy path (the TOCTOU the snapshot fixes).
    assert health_ws.config_entries_get_calls == 2
    assert client.get_states_calls == 1
    assert client.entity_registry_list_calls == 1
    assert client.config_entries_get_calls == 1

    assert resp["repairs"]["issues"] == [_issue("iss-1")]
    assert resp["matter_network"]["config_entry_id"] == "cfg-matter"
    orphan_ids = {
        i["entity_id"] for i in resp["dead_entities"]["config_entry_orphans"]["items"]
    }
    assert orphan_ids == {"sensor.orphan"}


@pytest.mark.asyncio
async def test_unknown_command_on_snapshot_invalidates_caps_and_falls_back() -> None:
    """unknown_command on system_snapshot -> invalidate caps + legacy per-section."""
    ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_result=_CAPS_SNAPSHOT,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    health_ws = _health_ws(
        entries=[_entry("cfg-zwave", "zwave_js")],
        repairs_issues=[],
        zwave_status={"controller": {"nodes": []}},
    )
    client = RoutingClient()

    with patch_ws(ws, tools_system), _health_baseline(health_ws):
        resp = await SystemTools(client).ha_get_system_health(
            include="repairs,zwave_network"
        )

    assert health_ws.repairs_calls == 1
    assert health_ws.config_entries_get_calls == 1
    assert resp["zwave_network"]["controller"] == {"nodes": []}
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_non_unknown_snapshot_error_falls_back_without_invalidating_caps() -> (
    None
):
    """A snapshot command timeout falls back to legacy WITHOUT invalidating
    caps — the capability is still advertised, only this one frame failed."""
    ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_result=_CAPS_SNAPSHOT,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    health_ws = _health_ws(repairs_issues=[_issue("iss-1")])
    client = RoutingClient()

    with patch_ws(ws, tools_system), _health_baseline(health_ws):
        resp = await SystemTools(client).ha_get_system_health(include="repairs")

    assert health_ws.repairs_calls == 1
    assert resp["repairs"]["issues"] == [_issue("iss-1")]
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_malformed_snapshot_slice_falls_back_to_legacy() -> None:
    """A snapshot reply missing an expected slice key (shape drift) is treated
    as untrustworthy as a WHOLE: every consuming section falls back to legacy
    rather than trusting a partially-shaped payload."""
    ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_result=_CAPS_SNAPSHOT,
        cmd_result={
            "config_entries": [],
            # "issues" key missing entirely — malformed.
            "entities": [],
            "states": [],
        },
    )
    health_ws = _health_ws(repairs_issues=[_issue("iss-1")])
    client = RoutingClient()

    with patch_ws(ws, tools_system), _health_baseline(health_ws):
        resp = await SystemTools(client).ha_get_system_health(include="repairs")

    assert health_ws.repairs_calls == 1
    assert resp["repairs"]["issues"] == [_issue("iss-1")]


@pytest.mark.asyncio
async def test_section_error_still_degrades_to_error_subdict() -> None:
    """Even with a successful snapshot, a real failure in a call the snapshot
    does NOT replace (zwave_js/network_status) still degrades to a per-section
    error sub-dict instead of raising — and sibling sections are unaffected."""
    snapshot_result = {
        "config_entries": [_entry("cfg-zwave", "zwave_js")],
        "issues": [_issue("iss-1")],
        "entities": [],
        "states": [],
    }
    ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_result=_CAPS_SNAPSHOT,
        cmd_result=snapshot_result,
    )
    health_ws = _health_ws(zwave_status_exc=RuntimeError("boom"))
    client = RoutingClient()

    with patch_ws(ws, tools_system), _health_baseline(health_ws):
        resp = await SystemTools(client).ha_get_system_health(
            include="repairs,zwave_network"
        )

    assert "error" in resp["zwave_network"]
    assert "boom" in resp["zwave_network"]["error"]
    # The sibling repairs section, served from the same snapshot, is unaffected.
    assert resp["repairs"]["issues"] == [_issue("iss-1")]


@pytest.mark.asyncio
async def test_snapshot_connection_error_falls_back_to_legacy() -> None:
    """A ``HomeAssistantConnectionError`` on the snapshot frame degrades to the
    per-section legacy fetches rather than failing the WHOLE tool: the legacy
    sections run on a dedicated health WS + REST + the never-raising bridge, not
    the pooled snapshot socket, so they still serve their data."""
    ws = make_ws(
        "ha_mcp_tools/system_snapshot",
        info_result=_CAPS_SNAPSHOT,
        cmd_exc=HomeAssistantConnectionError("connection lost"),
    )
    health_ws = _health_ws(repairs_issues=[_issue("iss-1")])
    client = RoutingClient()

    with patch_ws(ws, tools_system), _health_baseline(health_ws):
        resp = await SystemTools(client).ha_get_system_health(include="repairs")

    assert health_ws.repairs_calls == 1
    assert resp["repairs"]["issues"] == [_issue("iss-1")]
    # A transient connection error is not a downgrade — caps stay cached.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_snapshot_ws_establish_failure_falls_back_to_legacy() -> None:
    """A plain establish ``Exception`` from ``get_websocket_client()`` after caps
    are cached degrades to the per-section legacy fetches, not a whole-tool error."""
    caps_ws = make_ws("ha_mcp_tools/system_snapshot", info_result=_CAPS_SNAPSHOT)
    health_ws = _health_ws(repairs_issues=[_issue("iss-1")])
    client = RoutingClient()

    with (
        patch_ws_establish_failure(
            caps_ws,
            tools_system,
            HomeAssistantConnectionError(
                "Failed to connect to Home Assistant WebSocket"
            ),
        ),
        _health_baseline(health_ws),
    ):
        resp = await SystemTools(client).ha_get_system_health(include="repairs")

    assert health_ws.repairs_calls == 1
    assert resp["repairs"]["issues"] == [_issue("iss-1")]
    assert client in component_api._CAPS_CACHE
