"""Contract test for the component-``system_snapshot`` seam.

The component's ``_do_system_snapshot`` and the server's ``_fetch_repairs`` /
``_fetch_zwave_network`` / ``_fetch_matter_network`` / ``_fetch_dead_entities``
were written against the same design contract but never against each other's
code. This suite drives the REAL ``_do_system_snapshot`` (fake hass, real
registry/state/issue-registry joins) through the REAL ``ha_get_system_health``
tool, and separately drives the SAME underlying data through the legacy
per-section fetches, then asserts the four consuming sections are
byte-identical between the two paths — so a vocabulary or shape drift on
either side of the seam (e.g. the snapshot's ``entities`` slice missing a key
``_fetch_dead_entities`` reads) fails here instead of shipping a
component-served response the consumer mis-shapes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools import component_api, tools_system
from ha_mcp.tools.tools_system import SystemTools

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import (
    FakeConfigEntry,
    FakeHass,
    FakeIssue,
    FakeIssueRegistry,
    FakeIssueRegModule,
    FakeRegEntry,
    FakeState,
    make_view,
    wsapi,
)
from .test_ha_system_health_component_routing import (
    RoutingClient,
    _entry,
    _health_baseline,
    _health_ws,
    _issue,
    _registry_row,
    _state,
)

_CAPS_SNAPSHOT = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["system_snapshot"],
    "limits": {},
}
_CAPS_NONE = {
    "schema_version": 1,
    "component_version": "1.1.1",
    "capabilities": [],
    "limits": {},
}

# One scenario exercising all three dead_entities tiers-of-interest plus a
# zwave/matter config-entry lookup and one repair issue, shared by both the
# real-component fake hass and the legacy RoutingClient/health-ws fixtures.
_ENTRIES = [
    _entry("cfg-zwave", "zwave_js", title="Z-Wave JS"),
    _entry("cfg-matter", "matter", title="Matter Hub"),
]
_ISSUES = [_issue("iss-mqtt", domain="mqtt")]


def _hass() -> FakeHass:
    return FakeHass(
        states=[
            FakeState("sensor.zwave_battery", "100", friendly_name="Battery"),
            FakeState("sensor.orphan", "on"),
            FakeState("light.stale", "unavailable", restored=True),
        ],
        config_entries=[
            FakeConfigEntry(
                "zwave_js", title="Z-Wave JS", entry_id="cfg-zwave", state="loaded"
            ),
            FakeConfigEntry(
                "matter", title="Matter Hub", entry_id="cfg-matter", state="loaded"
            ),
        ],
    )


def _view() -> Any:
    return make_view(
        entity={
            # Alive: config_entry_id resolves against a live entry.
            "sensor.zwave_battery": FakeRegEntry(
                "sensor.zwave_battery",
                config_entry_id="cfg-zwave",
                platform="zwave_js",
                unique_id="zw-1",
            ),
            # config_entry_orphan: config_entry_id "cfg-gone" is absent from
            # _ENTRIES — the owning integration instance was removed.
            "sensor.orphan": FakeRegEntry(
                "sensor.orphan",
                config_entry_id="cfg-gone",
                platform="zwave_js",
                unique_id="zw-orphan",
            ),
            # stale_restored: unavailable + attributes.restored, owning entry
            # (cfg-matter) still present.
            "light.stale": FakeRegEntry(
                "light.stale",
                config_entry_id="cfg-matter",
                platform="matter",
                unique_id="matter-1",
            ),
        }
    )


def _real_snapshot_ws(hass: FakeHass, *, info_result: dict[str, Any]) -> AsyncMock:
    """A WS mock whose ``info`` is fixed and whose ``system_snapshot`` command
    runs the REAL ``_do_system_snapshot`` against ``hass``."""
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": info_result}
        if command_type == tools_system.WS_SYSTEM_SNAPSHOT:
            return {
                "success": True,
                "result": wsapi._do_system_snapshot(hass, kwargs),
            }
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _legacy_fixture_client() -> RoutingClient:
    """A RoutingClient serving the ``dead_entities`` legacy bridges from the
    SAME data as ``_hass()`` / ``_view()``."""
    return RoutingClient(
        states=[
            _state("sensor.zwave_battery", "100", friendly_name="Battery"),
            _state("sensor.orphan", "on"),
            _state("light.stale", "unavailable", restored=True),
        ],
        registry=[
            _registry_row(
                "sensor.zwave_battery", config_entry_id="cfg-zwave", platform="zwave_js"
            ),
            _registry_row(
                "sensor.orphan", config_entry_id="cfg-gone", platform="zwave_js"
            ),
            _registry_row(
                "light.stale", config_entry_id="cfg-matter", platform="matter"
            ),
        ],
        entries=_ENTRIES,
    )


def _legacy_health_ws() -> Any:
    return _health_ws(
        entries=_ENTRIES,
        repairs_issues=_ISSUES,
        zwave_status={"controller": {"nodes": []}},
    )


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.mark.asyncio
async def test_component_and_legacy_paths_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repairs / zwave_network / matter_network / dead_entities are
    byte-identical whether served by the real component snapshot or the
    legacy per-section fetches over the same underlying data."""
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: _view())
    monkeypatch.setattr(
        wsapi,
        "ir",
        FakeIssueRegModule(
            FakeIssueRegistry(
                [
                    FakeIssue(
                        "iss-mqtt",
                        "mqtt",
                        translation_key="tk",
                        created="2026-01-01T00:00:00+00:00",
                        issue_domain="mqtt",
                    ),
                    # A post-restart RESTORED placeholder (active=False, null
                    # severity/translation_key). The component's issues slice emits
                    # it (carrying `active` additively) where core's
                    # `repairs/list_issues` would filter it out; the server's
                    # active-filter must drop it so the component path matches the
                    # legacy path, which sees only `iss-mqtt` (_ISSUES).
                    FakeIssue(
                        "iss-restored",
                        "hue",
                        severity=None,
                        translation_key=None,
                        is_fixable=None,
                        created="2026-01-01T00:00:00+00:00",
                        issue_domain=None,
                        active=False,
                    ),
                ]
            )
        ),
    )
    include = "repairs,zwave_network,matter_network,dead_entities"

    # --- component path: the real _do_system_snapshot backs one WS frame ---
    hass = _hass()
    ws = _real_snapshot_ws(hass, info_result=_CAPS_SNAPSHOT)
    component_client = RoutingClient()
    component_health_ws = _legacy_health_ws()
    with patch_ws(ws, tools_system), _health_baseline(component_health_ws):
        component_resp = await SystemTools(component_client).ha_get_system_health(
            include=include
        )

    # --- legacy path: caps advertise nothing, so system_snapshot is never sent ---
    ws_legacy = _real_snapshot_ws(hass, info_result=_CAPS_NONE)
    legacy_client = _legacy_fixture_client()
    legacy_health_ws = _legacy_health_ws()
    with patch_ws(ws_legacy, tools_system), _health_baseline(legacy_health_ws):
        legacy_resp = await SystemTools(legacy_client).ha_get_system_health(
            include=include
        )

    for key in ("repairs", "zwave_network", "matter_network", "dead_entities"):
        assert component_resp[key] == legacy_resp[key], key

    # The component path never touched the legacy fetches system_snapshot
    # replaces (the zwave_js status call itself always runs on both paths).
    assert component_client.get_states_calls == 0
    assert component_client.entity_registry_list_calls == 0
    assert component_client.config_entries_get_calls == 0
    assert component_health_ws.repairs_calls == 0
    assert component_health_ws.config_entries_get_calls == 0
    assert component_health_ws.zwave_status_calls == 1

    # The legacy path paid for every fetch the snapshot would have collapsed —
    # including the TOCTOU-prone double config_entries/get (zwave + matter
    # each resolve it independently).
    assert legacy_client.get_states_calls == 1
    assert legacy_client.entity_registry_list_calls == 1
    assert legacy_client.config_entries_get_calls == 1
    assert legacy_health_ws.repairs_calls == 1
    assert legacy_health_ws.config_entries_get_calls == 2
    assert legacy_health_ws.zwave_status_calls == 1

    # Sanity: the fixture actually exercises all three dead_entities tiers.
    orphan_ids = {
        i["entity_id"]
        for i in component_resp["dead_entities"]["config_entry_orphans"]["items"]
    }
    stale_ids = {
        i["entity_id"]
        for i in component_resp["dead_entities"]["stale_restored"]["items"]
    }
    assert orphan_ids == {"sensor.orphan"}
    assert stale_ids == {"light.stale"}
    assert component_resp["repairs"]["issues"][0]["issue_id"] == "iss-mqtt"
    assert component_resp["zwave_network"]["controller"] == {"nodes": []}
    assert component_resp["matter_network"]["config_entry_id"] == "cfg-matter"


@pytest.mark.asyncio
async def test_include_dismissed_repairs_via_component_matches_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``include_dismissed_repairs=True`` through the prefetched ``issues``
    slice (the component's ``system_snapshot`` -> ``_fetch_repairs``
    ``prefetched_repairs`` path) returns the same repairs payload -- dismissed
    issue included -- as the legacy ``repairs/list_issues`` path."""
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: _view())
    monkeypatch.setattr(
        wsapi,
        "ir",
        FakeIssueRegModule(
            FakeIssueRegistry(
                [
                    FakeIssue(
                        "iss-mqtt",
                        "mqtt",
                        translation_key="tk",
                        created="2026-01-01T00:00:00+00:00",
                        issue_domain="mqtt",
                    ),
                    FakeIssue(
                        "iss-dismissed",
                        "hue",
                        translation_key="tk",
                        created="2026-01-01T00:00:00+00:00",
                        issue_domain="hue",
                        dismissed_version="2026.1.0",
                    ),
                ]
            )
        ),
    )
    hass = _hass()

    # --- component path: the real _do_system_snapshot's "issues" slice feeds
    # _fetch_repairs' prefetched_repairs, which include_dismissed then filters ---
    ws = _real_snapshot_ws(hass, info_result=_CAPS_SNAPSHOT)
    component_client = RoutingClient()
    component_health_ws = _health_ws()
    with patch_ws(ws, tools_system), _health_baseline(component_health_ws):
        component_resp = await SystemTools(component_client).ha_get_system_health(
            include="repairs", include_dismissed_repairs=True
        )

    # --- legacy path: caps advertise nothing, so system_snapshot is never sent ---
    ws_legacy = _real_snapshot_ws(hass, info_result=_CAPS_NONE)
    legacy_client = RoutingClient()
    legacy_health_ws = _health_ws(
        repairs_issues=[
            _issue("iss-mqtt", domain="mqtt"),
            _issue("iss-dismissed", domain="hue", dismissed_version="2026.1.0"),
        ]
    )
    with patch_ws(ws_legacy, tools_system), _health_baseline(legacy_health_ws):
        legacy_resp = await SystemTools(legacy_client).ha_get_system_health(
            include="repairs", include_dismissed_repairs=True
        )

    assert component_resp["repairs"] == legacy_resp["repairs"]
    issue_ids = {i["issue_id"] for i in component_resp["repairs"]["issues"]}
    assert issue_ids == {"iss-mqtt", "iss-dismissed"}
    assert component_resp["repairs"]["count"] == 2
    # include_dismissed=True never reports a separate dismissed_count.
    assert "dismissed_count" not in component_resp["repairs"]

    # The component path never touched the legacy repairs fetch; the legacy
    # path paid for exactly the one it replaces.
    assert component_health_ws.repairs_calls == 0
    assert legacy_health_ws.repairs_calls == 1
