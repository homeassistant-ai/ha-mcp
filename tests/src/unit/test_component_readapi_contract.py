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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools import (
    component_api,
    component_devices,
    tools_blueprints,
    tools_config_helpers,
    tools_entities,
    tools_search,
    tools_voice_assistant,
)
from ha_mcp.tools.radio.zigbee import _resolve_ieee
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import (
    FakeArea,
    FakeConfig,
    FakeConfigEntry,
    FakeDevice,
    FakeErModule,
    FakeFloor,
    FakeHass,
    FakeLabel,
    FakeRegEntry,
    FakeServices,
    FakeState,
    make_view,
    wsapi,
)
from .test_config_get_component_routing import (
    RoutingClient as GetRoutingClient,
)
from .test_ha_config_list_helpers_component_routing import (
    RoutingClient as HelpersRoutingClient,
)
from .test_ha_config_list_helpers_component_routing import (
    _build_list_helpers,
)
from .test_ha_get_blueprint_component_routing import (
    RoutingClient as BlueprintRoutingClient,
)
from .test_ha_get_blueprint_component_routing import (
    _build_get_blueprint,
)
from .test_ha_get_device_component_routing import (
    RoutingClient as GetDeviceRoutingClient,
)
from .test_ha_get_device_component_routing import (
    _build_get_device,
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
    _build_exposure,
)
from .test_ha_get_state_component_routing import (
    RoutingClient as StateRoutingClient,
)
from .test_ha_get_state_component_routing import (
    _build_get_state,
)
from .test_ha_overview_component_routing import (
    OverviewRoutingClient,
    _build_overview_tool,
    _setup_visibility_disabled,
)
from .test_ha_remove_device_component_routing import (
    RoutingClient as RemoveDeviceRoutingClient,
)
from .test_ha_remove_device_component_routing import (
    _build_remove_device,
)
from .test_radio_zigbee_component_routing import (
    RoutingClient as ZigbeeRoutingClient,
)

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


def _real_component_ws(hass: FakeHass) -> AsyncMock:
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
    body. All three get tools serve their reads from the legacy REST/WS pipeline
    (which reads the fresh config file). Even against a REAL component WS whose
    ``info`` advertises ``config_get``, the get tools skip the caps probe and
    never send a frame — the component WS is never touched. Scenes were already
    legacy-only (a ``HomeAssistantScene`` has no raw storage body in memory);
    automation/script now share the pattern.
    """

    @pytest.mark.asyncio
    async def test_automation_get_stays_legacy_with_full_caps(self) -> None:
        client = GetRoutingClient(
            automation_config={
                "id": "uid-1",
                "alias": "UI Auto",
                "action": [{"service": "light.turn_on"}],
            },
        )
        # A real component WS advertising full caps (config_get included). The
        # automation tool must never reach for it.
        ws = _real_component_ws(FakeHass())
        with patch.object(
            component_api, "get_websocket_client", AsyncMock(return_value=ws)
        ):
            resp = await AutomationConfigTools(client).ha_config_get_automation("uid-1")
        assert resp["success"] is True
        assert resp["config"]["alias"] == "UI Auto"
        # The tool's canonicalization (singular -> plural root keys) runs on the
        # legacy path.
        assert resp["config"]["actions"] == [{"service": "light.turn_on"}]
        # The component WS was never touched; the legacy config fetch served it.
        assert ws.send_command.await_count == 0
        assert client.get_automation_config.await_count == 1

    @pytest.mark.asyncio
    async def test_script_get_stays_legacy_with_full_caps(self) -> None:
        """Script analog: the legacy REST envelope is returned under ``config``
        (storage key + category injected). Scripts run NO root-key
        canonicalization (unlike automations' action->actions), so the storage
        ``sequence`` body passes through byte-exact."""
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
        assert resp["config"]["script_id"] == "morning"
        assert resp["config"]["category"] == "cat-s"
        # raw_config served byte-exact — the sequence body is untouched.
        assert resp["config"]["config"] == {
            "alias": "Morning Script",
            "sequence": [{"delay": {"seconds": 5}}],
        }
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
        with patch_ws(ws, tools_search):
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
        with patch_ws(ws, tools_config_helpers):
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
        with patch_ws(ws, tools_search):
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
        with patch_ws(ws, tools_search):
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
        """ha_get_blueprint merges the REAL executor-read, jailed file body under
        ``config`` (``!input`` preserved as a marker) over the list metadata."""
        target = tmp_path / "blueprints" / "automation" / "user" / "motion.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_MOTION_BLUEPRINT, encoding="utf-8")

        client = BlueprintRoutingClient()  # its blueprint/list serves user/motion.yaml
        ws = _real_component_ws(self._hass(tmp_path))
        tool = _build_get_blueprint(client)
        with patch_ws(ws, tools_blueprints):
            resp = await tool(path="user/motion.yaml", domain="automation")

        assert resp["success"] is True
        assert resp["metadata"]["name"] == "Motion Light"
        assert resp["config"]["trigger"][0]["entity_id"] == {
            "__input__": "motion_sensor"
        }
        assert resp["config"]["action"] == [{"service": "light.turn_on"}]

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
        with patch_ws(ws, tools_blueprints):
            resp = await tool(path=evil, domain="automation")

        assert resp["success"] is True
        # The real jail blocked the read — no body, and the secret never leaked.
        assert "config" not in resp
        assert "hunter2" not in json.dumps(resp)


# --- device_get / device_list ---------------------------------------------------
class TestDeviceSeam:
    """The raw ``DeviceEntry.dict_repr`` + entity-row shapes reconcile every consumer.

    One REAL ``_do_device_get`` output drives consumer sites through their real
    server code: ``ha_get_device`` (its device transform AND its per-device entity
    join via ``include_entities``), ``ha_remove_device`` (its ``config_entries``
    read), and ``_resolve_ieee`` (its ``identifiers`` read). A drift on either side
    of a seam — the component emitting a different key set, or a consumer reading a
    field the raw shape does not carry — fails here. Each consumer is served the
    device in one in-process ``device_get`` frame, never the whole-registry dump;
    the entity join carries ``config/entity_registry/list``-shaped rows so the
    device's entity list needs no separate entity-registry dump either.
    """

    def _device(self) -> FakeDevice:
        return FakeDevice(
            "dev-1",
            name="Kitchen Sensor",
            name_by_user="Kitchen",
            area_id="a1",
            labels=("important",),
            manufacturer="Aqara",
            model="T1",
            identifiers=(("zha", "00:11:22:33:44:55:66:77"),),
            config_entries=("cfg-1",),
        )

    def _entities(self) -> dict[str, Any]:
        return {
            "sensor.kitchen": FakeRegEntry(
                "sensor.kitchen", device_id="dev-1", platform="zha", name="Kitchen Temp"
            ),
            "update.kitchen": FakeRegEntry(
                "update.kitchen", device_id="dev-1", platform="zha"
            ),
        }

    @pytest.mark.asyncio
    async def test_get_device_transform_from_real_component(self, monkeypatch) -> None:
        dev = self._device()
        monkeypatch.setattr(
            wsapi,
            "_resolve_registries",
            lambda h: make_view(devices=[dev], entity=self._entities()),
        )
        # _do_device_get(include_entities) reads the device's entities through core's
        # er.async_entries_for_device index — stubbed MagicMock at import, so pin the
        # faithful fake here.
        monkeypatch.setattr(wsapi, "er", FakeErModule())
        client = GetDeviceRoutingClient()
        # A zha device triggers the ZHA metrics enricher (a dedicated path outside
        # the device_get seam); serve it an empty result so it is a graceful no-op.
        base_send = client.send_websocket_message

        async def _send(msg: Any) -> dict[str, Any]:
            if msg.get("type") == "zha/devices":
                return {"success": True, "result": []}
            return await base_send(msg)

        client.send_websocket_message = _send  # type: ignore[assignment]
        ws = _real_component_ws(FakeHass())
        get_device = _build_get_device(client)
        with patch_ws(ws, component_devices):
            resp = await get_device(device_id="dev-1")

        assert resp["success"] is True
        info = resp["device"]
        # Every field the transform surfaces was read out of the raw dict_repr.
        assert info["device_id"] == "dev-1"
        assert info["name"] == "Kitchen"  # name_by_user preferred over name
        assert info["area_id"] == "a1"
        assert info["labels"] == ["important"]
        assert info["config_entries"] == ["cfg-1"]
        assert info["integration_type"] == "zha"
        assert info["ieee_address"] == "00:11:22:33:44:55:66:77"
        # The device's entity list came from the include_entities join over the REAL
        # component output — the whole device AND entity registries were never dumped.
        assert resp["entity_count"] == 2
        assert {e["entity_id"] for e in resp["entities"]} == {
            "sensor.kitchen",
            "update.kitchen",
        }
        assert client.device_list_calls == 0
        assert client.entity_list_calls == 0

        # Pin the RAW entity-row shape the component emits (config/entity_registry/list
        # parity, device_id carried) so the join survives byte-for-byte across the seam.
        raw = wsapi._do_device_get(
            FakeHass(), {"device_id": "dev-1", "include_entities": True}
        )
        raw_row = next(e for e in raw["entities"] if e["entity_id"] == "sensor.kitchen")
        assert raw_row["device_id"] == "dev-1"
        assert raw_row["platform"] == "zha"
        assert raw_row["name"] == "Kitchen Temp"

    @pytest.mark.asyncio
    async def test_remove_device_config_entries_from_real_component(
        self, monkeypatch
    ) -> None:
        dev = self._device()
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: make_view(devices=[dev])
        )
        settings = MagicMock()
        settings.enable_auto_backup = False
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings", lambda: settings
        )
        client = RemoveDeviceRoutingClient()
        ws = _real_component_ws(FakeHass())
        remove_device = _build_remove_device(client)
        with patch_ws(ws, component_devices):
            resp = await remove_device(device_id="dev-1")

        assert resp["success"] is True
        # ha_remove_device read config_entries straight out of the raw shape.
        assert client.remove_calls == ["cfg-1"]
        assert client.device_list_calls == 0

    @pytest.mark.asyncio
    async def test_resolve_ieee_from_real_component(self, monkeypatch) -> None:
        dev = self._device()
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: make_view(devices=[dev])
        )
        client = ZigbeeRoutingClient()
        ws = _real_component_ws(FakeHass())
        with patch_ws(ws, component_devices):
            ieee = await _resolve_ieee(client, "dev-1")

        # _resolve_ieee parsed the identifiers out of the same raw shape.
        assert ieee == "00:11:22:33:44:55:66:77"
        assert client.device_list_calls == 0


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


# --- exposure -------------------------------------------------------------------
class TestExposureSeam:
    """``ha_get_entity_exposure`` served + enriched by the REAL ``_do_exposure``.

    Drives the real list/single exposure command (its should_expose filter and
    registry-join enrichment) through the real server shaper, pinning that the
    legacy ``exposed_to`` keys stay byte-identical while the ``entity_info`` /
    single-entity enrichment is additive.
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
        monkeypatch.setattr(wsapi, "_legacy_exposed_entity_ids", lambda h: [])

    @pytest.mark.asyncio
    async def test_single_exposure_enriched_from_real_component(
        self, monkeypatch
    ) -> None:
        self._patch(
            monkeypatch,
            {"light.lamp": {"conversation": {"should_expose": True}}},
        )
        client = ExposureRoutingClient({"light.lamp": {"conversation": True}})
        hass = FakeHass(states=[FakeState("light.lamp", "on", friendly_name="Lamp")])
        ws = _real_component_ws(hass)
        tool = _build_exposure(client)
        with patch_ws(ws, tools_voice_assistant):
            resp = await tool(entity_id="light.lamp")

        # Legacy keys byte-identical.
        assert resp["exposed_to"] == {
            "conversation": True,
            "cloud.alexa": False,
            "cloud.google_assistant": False,
        }
        assert resp["is_exposed_anywhere"] is True
        # Additive enrichment from the real join.
        assert resp["friendly_name"] == "Lamp"
        assert resp["domain"] == "light"
        assert resp["area"] == "Office"
        assert resp["floor"] == "Upstairs"
        # The legacy expose_entity/list was never touched.
        assert client.legacy_calls == 0

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
        tool = _build_exposure(client)
        with patch_ws(ws, tools_voice_assistant):
            resp = await tool()

        assert resp["exposed_entities"] == {"light.lamp": {"conversation": True}}
        assert set(resp["entity_info"]) == {"light.lamp"}
        assert resp["entity_info"]["light.lamp"]["area"] == "Office"
        assert client.legacy_calls == 0
