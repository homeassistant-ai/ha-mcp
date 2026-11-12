"""Cross-seam contract tests for the component read API (non-search commands).

Like ``test_component_search_contract.py``, these wire the REAL component
functions (``_do_overview`` / ``_do_helpers_list``, driven against fake hass
objects) underneath the mocked WS transport and then invoke the REAL server
tools — so a vocabulary or shape drift on either side of a seam fails here
rather than shipping a component-served response the consumer mis-shapes. The
component and consumer test suites each verify their own side against the design
doc; this file is the bridge that verifies them against each other.

The config_get seam is the exception: the component's ``config_get`` was
withdrawn before release (its ``raw_config`` freshness lags the config file
between a write and the next completed reload), so those pins assert the get
tools serve automation/script/scene reads from the legacy path and never touch
the component WS.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.tools import (
    blueprint_sources,
    component_api,
    entity_exposure,
    tools_config_helpers,
    tools_entities,
)
from ha_mcp.tools.search import component as search_component
from ha_mcp.tools.search import overview as search_overview
from ha_mcp.tools.search import state as search_state
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import (
    FakeArea,
    FakeChildDevice,
    FakeConfig,
    FakeConfigEntry,
    FakeDevice,
    FakeFloor,
    FakeHass,
    FakeLabel,
    FakeRegEntry,
    FakeServices,
    FakeState,
    make_view,
    stored_helpers,
    wsapi,
)
from .test_config_get_component_routing import RoutingClient as GetRoutingClient
from .test_ha_config_list_helpers_component_routing import (
    RoutingClient as HelpersRoutingClient,
)
from .test_ha_config_list_helpers_component_routing import (
    _build_list_helpers,
)
from .test_ha_get_entity_component_routing import (
    RoutingClient as EntityRoutingClient,
)
from .test_ha_get_entity_component_routing import (
    _build_get_entity,
    _raw_entry,
)
from .test_ha_get_entity_exposure_component_routing import (
    RoutingClient as ExposureRoutingClient,
)
from .test_ha_get_entity_exposure_component_routing import (
    _listing as _exposure_listing,
)
from .test_ha_get_state_component_routing import (
    RoutingClient as StateRoutingClient,
)
from .test_ha_get_state_component_routing import (
    _build_get_state,
)
from .test_ha_manage_blueprints_component_routing import (
    RoutingClient as BlueprintRoutingClient,
)
from .test_ha_manage_blueprints_component_routing import (
    _build_get_blueprint,
)
from .test_ha_overview_component_routing import (
    OverviewRoutingClient,
    _build_overview_tool,
    _setup_visibility_disabled,
)
from .test_ha_search_component_routing import _build_ha_search

_REAL_FNS = {
    "ha_mcp_tools/overview": wsapi._do_overview,
    "ha_mcp_tools/helpers_list": wsapi._do_helpers_list,
    "ha_mcp_tools/states": wsapi._do_states,
    "ha_mcp_tools/blueprint_get": wsapi._do_blueprint_get,
    "ha_mcp_tools/device_get": wsapi._do_device_get,
    "ha_mcp_tools/device_list": wsapi._do_device_list,
    "ha_mcp_tools/entity_enrich": wsapi._do_entity_enrich,
    "ha_mcp_tools/exposure": wsapi._do_exposure,
}

# Commands whose blocking work lives in an async prep pre-step (run here so the
# seam exercises the real executor-offloaded jail/read, then the pure handler).
_PREP_FNS = {
    "ha_mcp_tools/blueprint_get": wsapi._blueprint_get_prep,
}


def _real_component_ws(hass: Any) -> AsyncMock:
    """A WS mock whose commands are served by the REAL component functions.

    ``info`` returns the real ``_do_info()`` (so the caps probe sees the real
    capability list), and each read command runs the real ``_do_*`` (after its
    real async ``prep``, when it has one) against ``hass`` — the seam under test
    is everything between that return value and the tool response.
    """
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info()}
        fn = _REAL_FNS[command_type]
        params = dict(kwargs)
        prep = _PREP_FNS.get(command_type)
        extra = await prep(hass, params) if prep is not None else {}
        return {"success": True, "result": fn(hass, params, **extra)}

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


# --- config_get (withdrawn: automation/script/scene gets are legacy-only) ------
class TestConfigGetSeam:
    """The component's ``config_get`` was withdrawn before release — it served an
    entity's ``raw_config``, whose freshness lags the config file between a write
    and the next completed reload, so a get racing a reload returned a stale
    body. All three get tools serve the body from the legacy REST pipeline
    (which reads the fresh config file). Even against a REAL component WS whose
    ``info`` advertises ``config_get``, no ``config_get`` frame is sent; the
    script and scene gets skip the caps probe entirely, and the automation get
    asks the component only for the registry side. Scenes were already
    legacy-only (a ``HomeAssistantScene`` has no raw storage body in memory);
    automation/script now share the pattern.
    """

    @pytest.mark.asyncio
    async def test_automation_get_stays_legacy_with_full_caps(self) -> None:
        stored = {
            "id": "uid-1",
            "alias": "UI Auto",
            "action": [{"service": "light.turn_on"}],
        }
        client = GetRoutingClient(automation_config=dict(stored))
        # A real component WS advertising full caps (config_get included). The
        # automation tool may probe it for the registry side, never the body.
        ws = _real_component_ws(FakeHass())
        with patch.object(
            component_api, "get_websocket_client", AsyncMock(return_value=ws)
        ):
            resp = await AutomationConfigTools(client).ha_config_get_automation("uid-1")
        # The stored body, as Core stores it, from the legacy config fetch.
        assert resp["config"] == stored
        assert client.get_automation_config.await_count == 1
        sent = [c.args[0] for c in ws.send_command.call_args_list]
        assert "ha_mcp_tools/config_get" not in sent

    @pytest.mark.asyncio
    async def test_script_get_stays_legacy_with_full_caps(self) -> None:
        """Script analog: the legacy path returns the script BODY under
        ``config`` (category injected), the storage key staying top-level.
        The storage ``sequence`` body passes through byte-exact."""
        client = GetRoutingClient(
            script_envelope={
                "success": True,
                "script_id": "morning",
                "config": {
                    "alias": "Morning Script",
                    "sequence": [{"delay": {"seconds": 5}}],
                },
            },
            categories={"script": "cat-s"},
        )
        ws = _real_component_ws(FakeHass())
        with patch.object(
            component_api, "get_websocket_client", AsyncMock(return_value=ws)
        ):
            resp = await ConfigScriptTools(client).ha_config_get_script("morning")
        assert resp["success"] is True
        assert resp["script_id"] == "morning"
        assert resp["config"]["category"] == "cat-s"
        assert "script_id" not in resp["config"]
        # raw_config served byte-exact — the sequence body is untouched.
        assert resp["config"]["alias"] == "Morning Script"
        assert resp["config"]["sequence"] == [{"delay": {"seconds": 5}}]
        assert ws.send_command.await_count == 0
        assert client.get_script_config.await_count == 1

    @pytest.mark.asyncio
    async def test_scene_get_stays_legacy_with_full_caps(self, monkeypatch) -> None:
        """With the real component advertising ``config_get``, a scene get is
        still served entirely by the legacy path: ``ha_config_get_scene`` skips
        the caps probe, so the component WS is never touched (a
        ``HomeAssistantScene`` has no raw storage body in memory — its
        ``scene_config.states`` is runtime State objects)."""
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
        # The component WS was never touched at all — no caps probe, no frame.
        assert ws.send_command.await_count == 0
        assert client.get_scene_config.await_count == 1


# --- overview -------------------------------------------------------------------
class TestOverviewSeam:
    def _hass(self, states: list[FakeState] | None = None) -> FakeHass:
        return FakeHass(
            states=states
            if states is not None
            else [
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
        with patch_ws(ws, search_overview):
            resp = await tool()
        assert resp["success"] is True
        # The server's existing assembly ran over the REAL raw slices: the two
        # states must be counted and the domain summary populated.
        assert resp["system_summary"]["total_entities"] == 2
        assert client.total_legacy_fetches() == 0, (
            "overview must be fully component-served"
        )

    @pytest.mark.asyncio
    async def test_child_device_counts_in_parent_effective_area(
        self, tmp_path, monkeypatch
    ) -> None:
        hass = self._hass(
            [FakeState("sensor.child", "21", friendly_name="Child Sensor")]
        )
        monkeypatch.setattr(
            wsapi,
            "_resolve_registries",
            lambda h: make_view(
                entity={
                    "sensor.child": FakeRegEntry("sensor.child", device_id="child")
                },
                areas=[FakeArea("office", "Office", floor_id="upstairs")],
                floors=[FakeFloor("upstairs", "Upstairs")],
                devices=[FakeDevice("parent", area_id="office")],
                child_devices=[FakeChildDevice("child", "parent")],
            ),
        )
        _setup_visibility_disabled(tmp_path, monkeypatch)
        client = OverviewRoutingClient()
        tool = _build_overview_tool(client)
        with patch_ws(_real_component_ws(hass), search_overview):
            resp = await tool()

        assert resp["success"] is True
        assert resp["area_analysis"]["office"]["count"] == 1
        assert client.total_legacy_fetches() == 0


# --- search ---------------------------------------------------------------------
class TestSearchSeam:
    @pytest.mark.asyncio
    async def test_child_device_effective_area_and_floor_reach_public_tool(
        self, tmp_path, monkeypatch
    ) -> None:
        view = make_view(
            entity={"sensor.child": FakeRegEntry("sensor.child", device_id="child")},
            areas=[FakeArea("office", "Office", floor_id="upstairs")],
            floors=[FakeFloor("upstairs", "Upstairs")],
            devices=[FakeDevice("parent", area_id="office")],
            child_devices=[FakeChildDevice("child", "parent")],
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: view)
        _setup_visibility_disabled(tmp_path, monkeypatch)
        client = OverviewRoutingClient()
        hass = FakeHass(
            states=[FakeState("sensor.child", "21", friendly_name="Child Sensor")]
        )
        tool = _build_ha_search(client)
        ws = AsyncMock()

        async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
            if command_type == "ha_mcp_tools/info":
                return {"success": True, "result": wsapi._do_info()}
            if command_type == "ha_mcp_tools/search":
                return {
                    "success": True,
                    "result": wsapi._do_search(hass, kwargs),
                }
            raise AssertionError(f"unexpected component command {command_type!r}")

        ws.send_command = AsyncMock(side_effect=_send)
        with patch_ws(ws, search_component):
            resp = await tool(
                query="child sensor",
                result_fields=["entity_id", "area", "floor"],
            )

        assert resp["success"] is True
        assert resp["entities"] == [
            {"entity_id": "sensor.child", "area": "Office", "floor": "Upstairs"}
        ]
        assert client.total_legacy_fetches() == 0


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
            ],
            data=stored_helpers(
                {"input_boolean": [{"id": "guest_mode", "name": "Old"}]}
            ),
        )
        monkeypatch.setattr(
            wsapi,
            "_resolve_registries",
            lambda h: make_view(
                entity={
                    "input_boolean.guest_mode": FakeRegEntry(
                        "input_boolean.guest_mode",
                        unique_id="guest_mode",
                        platform="input_boolean",
                    )
                }
            ),
        )
        client = HelpersRoutingClient()
        ws = _real_component_ws(hass)
        tool = _build_list_helpers(client)
        with patch_ws(ws, tools_config_helpers):
            resp = await tool(helper_type="input_boolean")
        (rec,) = resp["helpers"]
        # #1794 via the REAL component output: storage id + current entity_id/name.
        assert rec["id"] == "guest_mode"
        assert rec["entity_id"] == "input_boolean.guest_mode"
        assert rec["name"] == "Current Guest"
        assert client.list_calls == 0

    @pytest.mark.asyncio
    async def test_flow_helper_records_shaped_from_real_output(
        self, monkeypatch
    ) -> None:
        """A flow helper (template) round-trips the REAL ``_do_helpers_list`` flow
        output through the REAL ``ha_config_list_helpers``: entry_id + current
        registry name + options, with ``entry.data`` never surfacing and no
        legacy fetch (flow types are component-only)."""
        entry = FakeConfigEntry(
            "template",
            title="Creation Title",
            options={"state": "{{ is_state('sun.sun', 'above_horizon') }}"},
            data={"api_key": "DATA_SECRET_XYZ"},
            entry_id="cfg1",
        )
        hass = FakeHass(config_entries=[entry])
        monkeypatch.setattr(
            wsapi,
            "_resolve_registries",
            lambda h: make_view(
                entity={
                    "binary_sensor.sun_up": FakeRegEntry(
                        "binary_sensor.sun_up",
                        name="Sun Is Up",  # current display name (post-rename)
                        config_entry_id="cfg1",
                    )
                }
            ),
        )
        client = HelpersRoutingClient()
        ws = _real_component_ws(hass)
        tool = _build_list_helpers(client)
        with patch_ws(ws, tools_config_helpers):
            resp = await tool(helper_type="template")

        assert resp["success"] is True
        assert resp["count"] == 1
        (rec,) = resp["helpers"]
        assert rec["helper_type"] == "template"
        assert rec["entry_id"] == "cfg1"
        assert rec["entity_id"] == "binary_sensor.sun_up"
        assert rec["name"] == "Sun Is Up"
        assert rec["options"] == {"state": "{{ is_state('sun.sun', 'above_horizon') }}"}
        # Flow helpers carry no storage id; entry.data must never leak.
        assert "id" not in rec
        assert "DATA_SECRET_XYZ" not in json.dumps(resp)
        # A flow type is component-only: the legacy list is never consulted.
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
        with patch_ws(ws, tools_config_helpers):
            resp = await tool(helper_type="tag")
        assert resp["success"] is True
        assert [h["id"] for h in resp["helpers"]] == ["tag-42"]
        assert client.list_calls >= 1, "tag must be served by legacy"


# --- states ---------------------------------------------------------------------
class TestStatesSeam:
    @pytest.mark.asyncio
    async def test_bulk_states_shaped_from_real_output(self) -> None:
        """A bulk ha_get_state runs over the REAL ``_do_states``: hits carry the
        component's ``State.as_dict()`` body verbatim, a missing id lands in
        ``errors`` as ENTITY_NOT_FOUND, and no legacy REST GET fires."""
        hass = FakeHass(
            states=[
                FakeState("light.lamp", "on", friendly_name="Lamp"),
                FakeState("sensor.temp", "21", friendly_name="Temp"),
            ]
        )
        client = StateRoutingClient()
        ws = _real_component_ws(hass)
        tool = _build_get_state(client)
        with patch_ws(ws, search_state):
            resp = await tool(["light.lamp", "sensor.temp", "light.ghost"])

        data = resp["data"]
        assert data["success"] is True
        assert set(data["states"]) == {"light.lamp", "sensor.temp"}
        # The per-id body is the component's real as_dict() output (REST parity).
        assert data["states"]["light.lamp"]["state"] == "on"
        assert data["states"]["light.lamp"]["attributes"]["friendly_name"] == "Lamp"
        assert data["error_count"] == 1
        assert data["errors"][0]["entity_id"] == "light.ghost"
        assert data["errors"][0]["error"]["code"] == "ENTITY_NOT_FOUND"
        assert client.get_state_calls == 0, "states must be fully component-served"

    @pytest.mark.asyncio
    async def test_single_state_shaped_from_real_output(self) -> None:
        """Single-entity ha_get_state served by the REAL ``_do_states``."""
        hass = FakeHass(states=[FakeState("light.lamp", "on", friendly_name="Lamp")])
        client = StateRoutingClient()
        ws = _real_component_ws(hass)
        tool = _build_get_state(client)
        with patch_ws(ws, search_state):
            resp = await tool("light.lamp")
        assert resp["data"]["entity_id"] == "light.lamp"
        assert resp["data"]["state"] == "on"
        assert client.get_state_calls == 0


# --- blueprint_get --------------------------------------------------------------
_MOTION_BLUEPRINT = (
    "blueprint:\n"
    "  name: Motion Light\n"
    "  domain: automation\n"
    "  input:\n"
    "    motion_sensor:\n"
    "      name: Motion Sensor\n"
    "trigger:\n"
    "  - platform: state\n"
    "    entity_id: !input motion_sensor\n"
    "action:\n"
    "  - service: light.turn_on\n"
)


class TestBlueprintGetSeam:
    def _hass(self, tmp_path: Any) -> FakeHass:
        return FakeHass(config=FakeConfig(base_dir=tmp_path))

    @pytest.mark.asyncio
    async def test_full_body_merged_from_real_file_read(self, tmp_path) -> None:
        """ha_manage_blueprints(action="get") merges the REAL executor-read, jailed
        file body under
        ``config`` (``!input`` preserved as a marker) over the list metadata."""
        target = tmp_path / "blueprints" / "automation" / "user" / "motion.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_MOTION_BLUEPRINT, encoding="utf-8")

        client = BlueprintRoutingClient()  # its blueprint/list serves user/motion.yaml
        ws = _real_component_ws(self._hass(tmp_path))
        tool = _build_get_blueprint(client)
        with patch_ws(ws, blueprint_sources):
            resp = await tool(path="user/motion.yaml", domain="automation")

        assert resp["success"] is True
        data = resp["data"]
        assert data["metadata"]["name"] == "Motion Light"
        assert data["config"]["trigger"][0]["entity_id"] == {
            "__input__": "motion_sensor"
        }
        assert data["config"]["action"] == [{"service": "light.turn_on"}]
        # The raw text round-trips the file byte for byte, tagged with the tier
        # that produced it — this is what a caller edits and saves back.
        assert data["yaml"] == _MOTION_BLUEPRINT
        assert data["yaml_source"] == "component"

    @pytest.mark.asyncio
    async def test_path_traversal_rejected_by_real_jail(self, tmp_path) -> None:
        """A blueprint whose list key escapes the jail is never read: the REAL
        jail returns a null body and the tool serves metadata-only."""
        # A secret sitting outside the blueprints jail.
        (tmp_path / "secrets.yaml").write_text("db_pw: hunter2\n", encoding="utf-8")
        evil = "../../secrets.yaml"

        class _EvilListClient(BlueprintRoutingClient):
            async def send_websocket_message(
                self, msg: dict[str, Any]
            ) -> dict[str, Any]:
                self.list_calls += 1
                if msg.get("type") == "blueprint/list":
                    return {"success": True, "result": {evil: {"metadata": {}}}}
                return {"success": False, "error": "unexpected"}

        client = _EvilListClient()
        ws = _real_component_ws(self._hass(tmp_path))
        tool = _build_get_blueprint(client)
        with patch_ws(ws, blueprint_sources):
            resp = await tool(path=evil, domain="automation")

        assert resp["success"] is True
        # The real jail blocked the read — no body, no text, and the secret
        # never leaked. Asserted against ``data``, where the body keys live:
        # checking the top level would pass no matter what the jail did.
        data = resp["data"]
        assert "config" not in data
        assert "yaml" not in data
        assert "yaml_source" not in data
        assert "hunter2" not in json.dumps(resp)


# --- entity_enrich --------------------------------------------------------------
class TestEntityEnrichSeam:
    """``ha_get_entity`` decorated by the REAL ``_do_entity_enrich`` join.

    The base registry record comes from the native ``config/entity_registry/get``;
    the additive area/floor/label NAMES come from running the real component join
    over a fake registry view, so a drift between the component's field names and
    the server's ``_merge_entity_enrichment`` mapping fails here.
    """

    def _view(self):
        return make_view(
            entity={
                "light.lamp": FakeRegEntry(
                    "light.lamp", aliases={"desk"}, area_id="a1", labels={"lb1"}
                )
            },
            areas=[FakeArea("a1", "Office", floor_id="f1")],
            floors=[FakeFloor("f1", "Upstairs")],
            labels=[FakeLabel("lb1", "Favorites")],
        )

    @pytest.mark.asyncio
    async def test_single_get_enriched_from_real_component(self, monkeypatch) -> None:
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: self._view())
        client = EntityRoutingClient(
            {"light.lamp": _raw_entry("light.lamp", area_id="a1", labels=["lb1"])}
        )
        ws = _real_component_ws(FakeHass())
        tool = _build_get_entity(client)
        with patch_ws(ws, tools_entities):
            resp = await tool("light.lamp")

        entry = resp["entity_entry"]
        # Base registry fields untouched.
        assert entry["area_id"] == "a1"
        assert entry["labels"] == ["lb1"]
        # Additive resolved-name enrichment from the real join.
        assert entry["area"] == "Office"
        assert entry["floor"] == "Upstairs"
        assert entry["label_names"] == ["Favorites"]

    @pytest.mark.asyncio
    async def test_bulk_get_enriched_from_real_component(self, monkeypatch) -> None:
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: self._view())
        client = EntityRoutingClient(
            {
                "light.lamp": _raw_entry("light.lamp", area_id="a1", labels=["lb1"]),
                "light.plain": _raw_entry("light.plain", area_id=None, labels=[]),
            }
        )
        ws = _real_component_ws(FakeHass())
        tool = _build_get_entity(client)
        with patch_ws(ws, tools_entities):
            resp = await tool(["light.lamp", "light.plain"])

        by_id = {e["entity_id"]: e for e in resp["entity_entries"]}
        assert by_id["light.lamp"]["area"] == "Office"
        assert by_id["light.lamp"]["label_names"] == ["Favorites"]
        # An entity absent from the fake view still gets empty additive fields.
        assert by_id["light.plain"]["area"] is None
        assert by_id["light.plain"]["label_names"] == []

    @pytest.mark.asyncio
    async def test_core_2026_9_child_device_inherits_parent_area_and_floor(
        self, monkeypatch
    ) -> None:
        parent = FakeDevice("parent-1", area_id="a1")
        child = FakeChildDevice("child-1", "parent-1")
        view = make_view(
            entity={"sensor.child": FakeRegEntry("sensor.child", device_id="child-1")},
            areas=[FakeArea("a1", "Office", floor_id="f1")],
            floors=[FakeFloor("f1", "Upstairs")],
            devices=[parent],
            child_devices=[child],
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: view)
        client = EntityRoutingClient(
            {
                "sensor.child": _raw_entry("sensor.child", area_id=None, labels=[])
                | {"device_id": "child-1"}
            }
        )
        ws = _real_component_ws(FakeHass())
        tool = _build_get_entity(client)
        with patch_ws(ws, tools_entities):
            resp = await tool("sensor.child")

        entry = resp["entity_entry"]
        assert entry["area"] == "Office"
        assert entry["floor"] == "Upstairs"


# --- exposure -------------------------------------------------------------------
class TestExposureSeam:
    """``ha_get_entity``'s exposure listing served + enriched by the REAL ``_do_exposure``.

    Drives the real list exposure command (its should_expose filter and
    registry-join enrichment) through the real server shaper, pinning that the
    ``exposed_entities`` map stays Core's while the ``entity_info`` enrichment
    is additive.
    """

    def _view(self):
        return make_view(
            entity={
                "light.lamp": FakeRegEntry("light.lamp", area_id="a1"),
                "light.attic": FakeRegEntry("light.attic", area_id="a1"),
            },
            areas=[FakeArea("a1", "Office", floor_id="f1")],
            floors=[FakeFloor("f1", "Upstairs")],
        )

    def _patch(self, monkeypatch, settings_map):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: self._view())

        def fake_settings(hass, entity_id):
            return settings_map.get(entity_id, {})

        monkeypatch.setattr(wsapi, "_async_get_entity_settings", fake_settings)
        candidates = {"conversation": dict.fromkeys(settings_map)}
        monkeypatch.setattr(
            wsapi, "_async_get_assistant_settings", lambda h: candidates
        )

    @pytest.mark.asyncio
    async def test_list_exposure_enriched_from_real_component(
        self, monkeypatch
    ) -> None:
        self._patch(
            monkeypatch,
            {
                "light.lamp": {"conversation": {"should_expose": True}},
                # not exposed: filtered out of the list
                "light.attic": {"cloud.alexa": {"should_expose": False}},
            },
        )
        client = ExposureRoutingClient({})
        hass = FakeHass(states=[FakeState("light.lamp", "on", friendly_name="Lamp")])
        ws = _real_component_ws(hass)
        with patch_ws(ws, entity_exposure):
            resp = await _exposure_listing(client)

        assert resp["exposed_entities"] == {"light.lamp": {"conversation": True}}
        assert set(resp["entity_info"]) == {"light.lamp"}
        assert resp["entity_info"]["light.lamp"]["area"] == "Office"
        assert client.legacy_calls == 0

    @pytest.mark.asyncio
    async def test_child_device_effective_area_and_floor_enrich_exposure(
        self, monkeypatch
    ) -> None:
        view = make_view(
            entity={"sensor.child": FakeRegEntry("sensor.child", device_id="child")},
            areas=[FakeArea("office", "Office", floor_id="upstairs")],
            floors=[FakeFloor("upstairs", "Upstairs")],
            devices=[FakeDevice("parent", area_id="office")],
            child_devices=[FakeChildDevice("child", "parent")],
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: view)
        monkeypatch.setattr(
            wsapi,
            "_async_get_entity_settings",
            lambda h, entity_id: {"conversation": {"should_expose": True}},
        )
        monkeypatch.setattr(
            wsapi,
            "_async_get_assistant_settings",
            lambda h: {"conversation": {"sensor.child": {}}},
        )
        client = ExposureRoutingClient({})
        hass = FakeHass(
            states=[FakeState("sensor.child", "21", friendly_name="Child Sensor")]
        )
        with patch_ws(_real_component_ws(hass), entity_exposure):
            resp = await _exposure_listing(client)

        info = resp["entity_info"]["sensor.child"]
        assert info["area"] == "Office"
        assert info["floor"] == "Upstairs"
        assert client.legacy_calls == 0
