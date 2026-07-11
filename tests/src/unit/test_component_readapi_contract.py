"""Cross-seam contract tests for the component read API (non-search commands).

Like ``test_component_search_contract.py``, these wire the REAL component
functions (``_do_config_get`` / ``_do_overview`` / ``_do_helpers_list``, driven
against fake hass objects) underneath the mocked WS transport and then invoke
the REAL server tools — so a vocabulary or shape drift on either side of a
seam fails here rather than shipping a component-served response the consumer
mis-shapes. The component and consumer test suites each verify their own side
against the design doc; this file is the bridge that verifies them against
each other.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.tools import (
    component_api,
    tools_config_automations,
    tools_config_scripts,
)
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from .test_component_ws_search import (
    FakeArea,
    FakeComponent,
    FakeConfig,
    FakeConfigEntity,
    FakeDevice,
    FakeHass,
    FakeRegEntry,
    FakeServices,
    FakeState,
    make_view,
    wsapi,
)
from .test_config_get_component_routing import (
    RoutingClient as GetRoutingClient,
)
from .test_config_get_component_routing import (
    _patch_ws as _patch_get_ws,
)
from .test_ha_config_list_helpers_component_routing import (
    RoutingClient as HelpersRoutingClient,
)
from .test_ha_config_list_helpers_component_routing import (
    _build_list_helpers,
)
from .test_ha_config_list_helpers_component_routing import (
    _patch_ws as _patch_helpers_ws,
)
from .test_ha_overview_component_routing import (
    OverviewRoutingClient,
    _build_overview_tool,
    _PatchBothWs,
    _setup_visibility_disabled,
)

_REAL_FNS = {
    "ha_mcp_tools/config_get": wsapi._do_config_get,
    "ha_mcp_tools/overview": wsapi._do_overview,
    "ha_mcp_tools/helpers_list": wsapi._do_helpers_list,
}


def _real_component_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock whose commands are served by the REAL component functions.

    ``info`` returns the real ``_do_info()`` (so the caps probe sees the real
    capability list), and each read command runs the real ``_do_*`` against
    ``hass`` — the seam under test is everything between that return value and
    the tool response.
    """
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info()}
        fn = _REAL_FNS[command_type]
        return {"success": True, "result": fn(hass, dict(kwargs))}

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


# --- config_get ---------------------------------------------------------------
class TestConfigGetSeam:
    def _hass(self) -> FakeHass:
        storage = FakeConfigEntity(
            "automation.ui",
            "UI Auto",
            unique_id="uid-1",
            raw_config={
                "id": "uid-1",
                "alias": "UI Auto",
                "action": [{"service": "light.turn_on"}],
            },
        )
        yaml_auto = FakeConfigEntity(
            "automation.pkg",
            "Package Auto",
            unique_id=None,
            raw_config={"alias": "Package Auto", "action": []},
        )
        return FakeHass(
            states=[FakeState("automation.ui", "on", "UI Auto")],
            data={"automation": FakeComponent([storage, yaml_auto])},
        )

    @pytest.mark.asyncio
    async def test_storage_automation_served_by_real_component(
        self, monkeypatch
    ) -> None:
        hass = self._hass()
        monkeypatch.setattr(
            wsapi,
            "_resolve_registries",
            lambda h: make_view(
                entity={
                    "automation.ui": FakeRegEntry(
                        "automation.ui", categories={"automation": "cat-1"}
                    )
                }
            ),
        )
        client = GetRoutingClient()
        ws = _real_component_ws(hass)
        with _patch_get_ws(ws, tools_config_automations):
            resp = await AutomationConfigTools(client).ha_config_get_automation("uid-1")
        assert resp["success"] is True
        assert resp["config"]["alias"] == "UI Auto"
        # The tool's canonicalization (singular -> plural root keys) runs
        # identically on the component path — parity includes the normalize.
        assert resp["config"]["actions"] == [{"service": "light.turn_on"}]
        assert client.legacy_fetch_count() == 0, (
            "storage get must be fully component-served"
        )

    @pytest.mark.asyncio
    async def test_yaml_automation_not_found_without_legacy_call(
        self, monkeypatch
    ) -> None:
        """A YAML item (found:false from the real component) must produce the
        legacy not-found error WITHOUT any legacy REST fetch, and its body
        must appear nowhere."""
        hass = self._hass()
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: make_view())
        client = GetRoutingClient()
        ws = _real_component_ws(hass)
        with (
            _patch_get_ws(ws, tools_config_automations),
            pytest.raises(Exception) as excinfo,
        ):
            await AutomationConfigTools(client).ha_config_get_automation(
                "automation.pkg"
            )
        assert "not found" in str(excinfo.value).lower()
        # The not-found raiser fetches available ids for the error context —
        # legacy-parity behavior. What must NOT run is the per-id config fetch.
        assert client.get_automation_config.await_count == 0

    def _script_hass(self) -> FakeHass:
        storage = FakeConfigEntity(
            "script.morning",
            "Morning Script",
            unique_id="morning",
            raw_config={
                "alias": "Morning Script",
                "sequence": [{"delay": {"seconds": 5}}],
            },
        )
        return FakeHass(
            states=[FakeState("script.morning", "off", "Morning Script")],
            data={"script": FakeComponent([storage])},
        )

    @pytest.mark.asyncio
    async def test_storage_script_served_by_real_component(self, monkeypatch) -> None:
        """Script analog of the automation seam: the real component's
        ``config_get`` output is reshaped into the legacy REST-envelope contract
        (storage key + category injected) with zero legacy fetches. Scripts run
        NO root-key canonicalization (unlike automations' action->actions), so
        the storage ``sequence`` body passes through byte-exact."""
        hass = self._script_hass()
        monkeypatch.setattr(
            wsapi,
            "_resolve_registries",
            lambda h: make_view(
                entity={
                    "script.morning": FakeRegEntry(
                        "script.morning", categories={"script": "cat-s"}
                    )
                }
            ),
        )
        client = GetRoutingClient()
        ws = _real_component_ws(hass)
        with _patch_get_ws(ws, tools_config_scripts):
            resp = await ConfigScriptTools(client).ha_config_get_script("morning")
        assert resp["success"] is True
        assert resp["script_id"] == "morning"
        # The legacy path returns the REST envelope under ``config``; the real
        # component reconstructs it (storage key + category injected).
        assert resp["config"]["script_id"] == "morning"
        assert resp["config"]["category"] == "cat-s"
        # raw_config served byte-exact — the sequence body is untouched.
        assert resp["config"]["config"] == {
            "alias": "Morning Script",
            "sequence": [{"delay": {"seconds": 5}}],
        }
        assert client.legacy_fetch_count() == 0, (
            "storage get must be fully component-served"
        )

    @pytest.mark.asyncio
    async def test_scene_get_stays_legacy_with_full_caps(self, monkeypatch) -> None:
        """With the real component advertising ``config_get``, a scene get is
        still served entirely by the legacy path: ``ha_config_get_scene`` skips
        the caps probe, so the component WS never receives a ``config_get`` for
        ``domain=scene`` (a ``HomeAssistantScene`` has no raw storage body in
        memory — its ``scene_config.states`` is runtime State objects)."""
        monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)
        client = GetRoutingClient(
            scene_envelope={
                "success": True,
                "scene_id": "movie_night",
                "config": {
                    "id": "movie_night",
                    "name": "Movie Night",
                    "entities": {"light.living_room": {"state": "on"}},
                },
            },
            categories={"scene": "cat-x"},
            registry_list=[
                {"entity_id": "scene.movie_night", "unique_id": "movie_night"}
            ],
        )
        # A real component WS advertising full caps (config_get included). The
        # scene tool must never reach for it.
        ws = _real_component_ws(FakeHass())
        with patch.object(
            component_api, "get_websocket_client", AsyncMock(return_value=ws)
        ):
            resp = await ConfigSceneTools(client).ha_config_get_scene(
                scene_id="movie_night"
            )
        assert resp["success"] is True
        assert resp["scene_id"] == "movie_night"
        # No ``config_get`` frame was ever sent for a scene...
        scene_config_get_calls = [
            c
            for c in ws.send_command.call_args_list
            if c.args
            and c.args[0] == "ha_mcp_tools/config_get"
            and c.kwargs.get("domain") == "scene"
        ]
        assert scene_config_get_calls == []
        # ...and, more strongly, the component WS was never touched at all.
        assert ws.send_command.await_count == 0
        assert client.get_scene_config.await_count == 1


# --- overview -------------------------------------------------------------------
class TestOverviewSeam:
    def _hass(self) -> FakeHass:
        return FakeHass(
            states=[
                FakeState("light.lamp", "on", friendly_name="Lamp"),
                FakeState("sensor.temp", "21", friendly_name="Temp"),
            ],
            services=FakeServices({"light": {"turn_on": {}, "turn_off": {}}}),
            config=FakeConfig(
                data={
                    "version": "2026.7.0",
                    "location_name": "Home",
                    "time_zone": "UTC",
                    "language": "en",
                    "state": "RUNNING",
                    "country": "US",
                    "unit_system": {"temperature": "°C"},
                    "components": ["light", "sensor"],
                    "internal_url": "http://homeassistant.local:8123",
                }
            ),
            data={"persistent_notification": {}},
        )

    @pytest.mark.asyncio
    async def test_overview_assembled_from_real_slices(
        self, tmp_path, monkeypatch
    ) -> None:
        hass = self._hass()
        monkeypatch.setattr(
            wsapi,
            "_resolve_registries",
            lambda h: make_view(
                entity={
                    "light.lamp": FakeRegEntry(
                        "light.lamp",
                        area_id="a1",
                        device_id="d1",
                        entity_category=None,
                        hidden_by=None,
                    )
                },
                areas=[FakeArea("a1", "Office", floor_id=None)],
                devices=[FakeDevice("d1", name="Lamp Device", area_id="a1")],
            ),
        )
        _setup_visibility_disabled(tmp_path, monkeypatch)
        client = OverviewRoutingClient()
        ws = _real_component_ws(hass)
        tool = _build_overview_tool(client)
        with _PatchBothWs(ws):
            resp = await tool()
        assert resp["success"] is True
        # The server's existing assembly ran over the REAL raw slices: the two
        # states must be counted and the domain summary populated.
        assert resp["system_summary"]["total_entities"] == 2
        assert client.total_legacy_fetches() == 0, (
            "overview must be fully component-served"
        )


# --- helpers_list ----------------------------------------------------------------
class TestHelpersListSeam:
    @pytest.mark.asyncio
    async def test_collection_helper_records_shaped_from_real_output(
        self, monkeypatch
    ) -> None:
        hass = FakeHass(
            states=[
                FakeState(
                    "input_boolean.guest_mode", "off", friendly_name="Current Guest"
                )
            ]
        )
        monkeypatch.setattr(
            wsapi,
            "_resolve_registries",
            lambda h: make_view(
                entity={
                    "input_boolean.guest_mode": FakeRegEntry(
                        "input_boolean.guest_mode",
                        name="Current Guest",
                        original_name="Old Name",
                        unique_id="guest_mode",
                    )
                }
            ),
        )
        client = HelpersRoutingClient()
        ws = _real_component_ws(hass)
        tool = _build_list_helpers(client)
        with _patch_helpers_ws(ws):
            resp = await tool(helper_type="input_boolean")
        assert resp["success"] is True
        assert resp["count"] == 1
        rec = resp["helpers"][0]
        # #1794 additive fix through the REAL component output: storage id
        # preserved, current entity_id + name layered on.
        assert rec["id"] == "guest_mode"
        assert rec["entity_id"] == "input_boolean.guest_mode"
        assert rec["name"] == "Current Guest"
        assert client.list_calls == 0

    @pytest.mark.asyncio
    async def test_tag_falls_back_to_legacy_via_real_covered_types(
        self, monkeypatch
    ) -> None:
        """The REAL component response's covered_types excludes tag — the tool
        must serve tag from the legacy list, not report an empty component
        result as 'no tags exist'."""
        hass = FakeHass(states=[])
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: make_view())
        client = HelpersRoutingClient()  # its tag/list serves a fixed record
        ws = _real_component_ws(hass)
        tool = _build_list_helpers(client)
        with _patch_helpers_ws(ws):
            resp = await tool(helper_type="tag")
        assert resp["success"] is True
        assert [h["id"] for h in resp["helpers"]] == ["tag-42"]
        assert client.list_calls >= 1, "tag must be served by legacy"
