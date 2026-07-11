"""Unit tests for the ha_mcp_tools in-process WebSocket search API.

Mirrors the established component-test pattern (``test_caller_token_auth.py`` /
``test_custom_component_filesystem.py``): the ``homeassistant.*`` imports are
stubbed with ``MagicMock`` and the pure ``_do_*`` functions are exercised with
fake hass / registry objects injected through the ``_resolve_registries`` seam.

Coverage:
* ``_do_info`` handshake shape + manifest/const version parity (drift guard).
* entity joins (name / alias / area / floor / label / domain / device).
* config matching: a YAML-loaded automation is indexed but its body is NEVER
  emitted; a storage-backed body is emitted only under ``include_config``.
* flow-helper ``options`` indexed while ``entry.data`` never appears anywhere in
  the serialized response (data-minimization contract).
* pagination, include_hidden, match-all, search_types gating.
* admin gate present on both registered commands.
* scorer parity against the server's ``_match_exact_search_entity`` /
  ``calculate_ratio`` (golden corpus) so the two scorers never drift.
* malformed-params rejection via voluptuous.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Force the REAL voluptuous into sys.modules (sibling unit modules stub it with
# a MagicMock at import time). Captured for the schema / registration tests,
# which need a working validator and functional decorators.
sys.modules.pop("voluptuous", None)
import voluptuous as _REAL_VOL  # noqa: E402

# Stub the HA modules the component imports. The pure search functions never
# touch them (registries arrive via the monkeypatched _resolve_registries seam).
for _mod in (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.persistent_notification",
    "homeassistant.config",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.storage",
    "homeassistant.loader",
):
    sys.modules.setdefault(_mod, MagicMock())

from custom_components.ha_mcp_tools import websocket_api as wsapi  # noqa: E402
from custom_components.ha_mcp_tools.const import COMPONENT_VERSION  # noqa: E402

# Server-side scoring path the component must stay in parity with.
from ha_mcp.tools.tools_search import _match_exact_search_entity  # noqa: E402
from ha_mcp.utils.fuzzy_search import calculate_ratio  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]


# =============================================================================
# Fakes
# =============================================================================
class FakeState:
    def __init__(self, entity_id, state="on", friendly_name=None, **attrs):
        self.entity_id = entity_id
        self.state = state
        self.attributes = dict(attrs)
        if friendly_name is not None:
            self.attributes["friendly_name"] = friendly_name


class FakeStates:
    def __init__(self, states):
        self._states = list(states)

    def async_all(self):
        return list(self._states)


class FakeConfigEntries:
    def __init__(self, entries):
        self._entries = list(entries)

    def async_entries(self):
        return list(self._entries)


class FakeHass:
    def __init__(self, states=(), data=None, config_entries=()):
        self.states = FakeStates(states)
        self.data = dict(data or {})
        self.config_entries = FakeConfigEntries(config_entries)


class FakeRegEntry:
    def __init__(
        self,
        entity_id,
        aliases=(),
        area_id=None,
        device_id=None,
        labels=(),
        hidden_by=None,
        name=None,
    ):
        self.entity_id = entity_id
        self.aliases = set(aliases)
        self.area_id = area_id
        self.device_id = device_id
        self.labels = set(labels)
        self.hidden_by = hidden_by
        self.name = name


class FakeEntityReg:
    def __init__(self, entries):
        self._entries = dict(entries)

    def async_get(self, entity_id):
        return self._entries.get(entity_id)


class FakeArea:
    def __init__(self, area_id, name, floor_id=None):
        self.id = area_id
        self.name = name
        self.floor_id = floor_id


class FakeAreaReg:
    def __init__(self, areas):
        self._areas = {a.id: a for a in areas}

    def async_get_area(self, area_id):
        return self._areas.get(area_id)


class FakeFloor:
    def __init__(self, floor_id, name):
        self.floor_id = floor_id
        self.name = name


class FakeFloorReg:
    def __init__(self, floors):
        self._floors = {f.floor_id: f for f in floors}

    def async_get_floor(self, floor_id):
        return self._floors.get(floor_id)


class FakeLabel:
    def __init__(self, label_id, name):
        self.label_id = label_id
        self.name = name


class FakeLabelReg:
    def __init__(self, labels):
        self._labels = {label.label_id: label for label in labels}

    def async_get_label(self, label_id):
        return self._labels.get(label_id)


class FakeDevice:
    def __init__(
        self,
        device_id,
        name=None,
        name_by_user=None,
        area_id=None,
        labels=(),
        manufacturer=None,
        model=None,
    ):
        self.id = device_id
        self.name = name
        self.name_by_user = name_by_user
        self.area_id = area_id
        self.labels = set(labels)
        self.manufacturer = manufacturer
        self.model = model


class FakeDeviceReg:
    def __init__(self, devices):
        self._devices = {d.id: d for d in devices}

    def async_get(self, device_id):
        return self._devices.get(device_id)


class FakeConfigEntity:
    """Stand-in for an AutomationEntity / ScriptEntity (raw_config-bearing)."""

    def __init__(self, entity_id, name=None, unique_id=None, raw_config=None):
        self.entity_id = entity_id
        self.name = name
        self.unique_id = unique_id
        self.raw_config = raw_config


class FakeSceneEntity:
    """Stand-in for HomeAssistantScene (scene_config, not raw_config)."""

    def __init__(self, entity_id, name=None, unique_id=None, scene_config=None):
        self.entity_id = entity_id
        self.name = name
        self.unique_id = unique_id
        self.scene_config = scene_config


class FakeComponent:
    def __init__(self, entities):
        self.entities = list(entities)


class FakeConfigEntry:
    def __init__(self, domain, title="", options=None, data=None, entry_id="entry"):
        self.domain = domain
        self.title = title
        self.options = dict(options or {})
        self.data = dict(data or {})
        self.entry_id = entry_id


def make_view(entity=None, areas=(), floors=(), labels=(), devices=()):
    return wsapi._RegistryView(
        entity=FakeEntityReg(entity or {}),
        area=FakeAreaReg(areas),
        floor=FakeFloorReg(floors),
        label=FakeLabelReg(labels),
        device=FakeDeviceReg(devices),
    )


@pytest.fixture
def empty_view(monkeypatch):
    """Patch _resolve_registries to an all-None view (states-only join)."""
    monkeypatch.setattr(
        wsapi, "_resolve_registries", lambda hass: wsapi._RegistryView()
    )


# =============================================================================
# info
# =============================================================================
class TestInfo:
    def test_shape(self):
        info = wsapi._do_info()
        assert info["schema_version"] == 1
        assert info["component_version"] == COMPONENT_VERSION
        assert info["capabilities"] == ["search"]
        assert info["limits"] == {"max_results": 500, "max_body_bytes": 1_000_000}

    def test_manifest_version_parity(self):
        """The manifest version and COMPONENT_VERSION must not drift."""
        manifest = json.loads(
            (
                _REPO_ROOT / "custom_components" / "ha_mcp_tools" / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        assert manifest["version"] == COMPONENT_VERSION == "1.1.0"


# =============================================================================
# entity joins
# =============================================================================
class TestEntityJoins:
    def test_joins_and_matches_all_dimensions(self, monkeypatch):
        states = [FakeState("light.lamp", "on", "Desk Lamp")]
        entry = FakeRegEntry(
            "light.lamp",
            aliases={"reading light"},
            area_id="a1",
            device_id="d1",
            labels={"lb1"},
        )
        view = make_view(
            entity={"light.lamp": entry},
            areas=[FakeArea("a1", "Office", floor_id="f1")],
            floors=[FakeFloor("f1", "Upstairs")],
            labels=[FakeLabel("lb1", "Favorites")],
            devices=[
                FakeDevice(
                    "d1",
                    name="Lamp Device",
                    manufacturer="Acme",
                    model="X1",
                    area_id="a9",
                    labels={"lb2"},
                )
            ],
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
        h = FakeHass(states=states)

        by_alias = wsapi._do_search(h, {"query": "reading light"})
        assert by_alias["entities"][0]["entity_id"] == "light.lamp"
        assert by_alias["entities"][0]["score"] == 100  # exact alias match

        projected = by_alias["entities"][0]
        assert projected["area"] == "Office"
        assert projected["floor"] == "Upstairs"
        assert "reading light" in projected["aliases"]
        assert "Favorites" in projected["labels"]

        for query in ("office", "upstairs", "favorites", "acme", "desk lamp"):
            res = wsapi._do_search(h, {"query": query})
            assert res["entities"], f"expected a hit for {query!r}"
            assert res["entities"][0]["entity_id"] == "light.lamp"

    def test_domain_filter_applies_to_entities(self, monkeypatch):
        states = [
            FakeState("light.kitchen", "on", "Kitchen"),
            FakeState("switch.kitchen", "on", "Kitchen"),
        ]
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda hass: wsapi._RegistryView()
        )
        res = wsapi._do_search(
            FakeHass(states=states), {"query": "kitchen", "domain_filter": "light"}
        )
        ids = {e["entity_id"] for e in res["entities"]}
        assert ids == {"light.kitchen"}

    def test_state_filter(self, monkeypatch):
        states = [
            FakeState("light.a", "on", "Lamp A"),
            FakeState("light.b", "off", "Lamp B"),
        ]
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda hass: wsapi._RegistryView()
        )
        res = wsapi._do_search(
            FakeHass(states=states), {"query": "lamp", "state_filter": "off"}
        )
        assert {e["entity_id"] for e in res["entities"]} == {"light.b"}

    def test_area_filter_by_name_or_id(self, monkeypatch):
        states = [FakeState("light.lamp", "on", "Lamp")]
        view = make_view(
            entity={"light.lamp": FakeRegEntry("light.lamp", area_id="a1")},
            areas=[FakeArea("a1", "Office")],
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
        h = FakeHass(states=states)
        assert wsapi._do_search(h, {"query": "lamp", "area_filter": "Office"})[
            "entities"
        ]
        assert wsapi._do_search(h, {"query": "lamp", "area_filter": "a1"})["entities"]
        assert not wsapi._do_search(h, {"query": "lamp", "area_filter": "Garage"})[
            "entities"
        ]

    def test_include_hidden_penalty_and_exclusion(self, monkeypatch):
        states = [
            FakeState("light.v", "on", "Visible"),
            FakeState("light.h", "on", "Hidden One"),
        ]
        view = make_view(entity={"light.h": FakeRegEntry("light.h", hidden_by="user")})
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
        h = FakeHass(states=states)

        shown = wsapi._do_search(h, {"query": "light", "include_hidden": True})
        scores = {e["entity_id"]: e["score"] for e in shown["entities"]}
        assert scores["light.v"] == 80
        assert scores["light.h"] == 60  # 80 - HIDDEN_SCORE_PENALTY

        filtered = wsapi._do_search(h, {"query": "light", "include_hidden": False})
        assert {e["entity_id"] for e in filtered["entities"]} == {"light.v"}

    def test_pagination_per_surface(self, empty_view):
        states = [FakeState(f"light.l{i}", "on", f"Lamp {i}") for i in range(5)]
        h = FakeHass(states=states)
        page1 = wsapi._do_search(h, {"query": "lamp", "limit": 2, "offset": 0})
        assert len(page1["entities"]) == 2
        assert page1["entity_total_matches"] == 5
        assert page1["entity_has_more"] is True
        last = wsapi._do_search(h, {"query": "lamp", "limit": 2, "offset": 4})
        assert len(last["entities"]) == 1
        assert last["entity_has_more"] is False

    def test_match_all_on_empty_query(self, empty_view):
        states = [FakeState("light.a", "on", "A"), FakeState("light.b", "on", "B")]
        res = wsapi._do_search(FakeHass(states=states), {})
        assert res["entity_total_matches"] == 2
        assert all(
            e["match_type"] == "match_all" and e["score"] == 100
            for e in res["entities"]
        )


# =============================================================================
# config surfaces — YAML body withholding, storage emission
# =============================================================================
class TestConfigSurfaces:
    def _hass(self):
        yaml_auto = FakeConfigEntity(
            "automation.pkg",
            "Package Auto",
            unique_id=None,
            raw_config={
                "alias": "Package Auto",
                "action": [{"service": "notify.x", "data": {"message": "YAMLSECRET"}}],
            },
        )
        storage_auto = FakeConfigEntity(
            "automation.ui",
            "UI Auto",
            unique_id="uid-1",
            raw_config={
                "id": "uid-1",
                "alias": "UI Auto",
                "action": [{"service": "light.turn_on"}],
            },
        )
        return FakeHass(data={"automation": FakeComponent([yaml_auto, storage_auto])})

    def test_yaml_body_never_emitted_storage_body_emitted(self, empty_view):
        h = self._hass()
        res = wsapi._do_search(h, {"query": "auto", "include_config": True})
        yaml_rec = next(a for a in res["automations"] if a["source"] == "yaml")
        storage_rec = next(a for a in res["automations"] if a["source"] == "storage")
        assert yaml_rec["config"] is None
        assert yaml_rec["id"] is None
        assert storage_rec["config"] is not None
        assert storage_rec["config"]["id"] == "uid-1"
        assert "YAMLSECRET" not in json.dumps(res)

    def test_yaml_body_indexed_for_matching_but_withheld(self, empty_view):
        h = self._hass()
        res = wsapi._do_search(h, {"query": "yamlsecret", "include_config": True})
        hits = [a for a in res["automations"] if a["source"] == "yaml"]
        assert hits, "YAML body should be indexed for matching"
        assert hits[0]["match_in_config"] is True
        assert hits[0]["config"] is None
        assert "YAMLSECRET" not in json.dumps(res)

    def test_storage_body_withheld_without_include_config(self, empty_view):
        h = self._hass()
        res = wsapi._do_search(h, {"query": "auto"})  # include_config defaults False
        for rec in res["automations"]:
            assert rec["config"] is None

    def test_scene_matches_via_scene_config(self, empty_view):
        scene = FakeSceneEntity(
            "scene.movie",
            "Movie Night",
            unique_id="scn-1",
            scene_config={"id": "scn-1", "name": "Movie Night", "icon": "mdi:movie"},
        )
        h = FakeHass(data={"scene": FakeComponent([scene])})
        res = wsapi._do_search(h, {"query": "movie", "include_config": True})
        assert res["scenes"]
        rec = res["scenes"][0]
        assert rec["name"] == "Movie Night"
        assert rec["source"] == "storage"
        assert rec["config"]["id"] == "scn-1"

    def test_config_combined_pagination(self, empty_view):
        autos = [
            FakeConfigEntity(
                f"automation.a{i}",
                f"Auto {i}",
                f"u{i}",
                {"id": f"u{i}", "alias": f"Auto {i}"},
            )
            for i in range(3)
        ]
        scenes = [
            FakeSceneEntity(
                "scene.s0", "Auto Scene", "sid0", {"id": "sid0", "name": "Auto Scene"}
            )
        ]
        h = FakeHass(
            data={
                "automation": FakeComponent(autos),
                "scene": FakeComponent(scenes),
            }
        )
        res = wsapi._do_search(h, {"query": "auto", "limit": 2, "offset": 0})
        assert res["config_total_matches"] == 4
        assert res["config_has_more"] is True
        assert len(res["automations"]) + len(res["scenes"]) == 2

    def test_search_types_gating(self, empty_view):
        states = [FakeState("light.k", "on", "Kitchen")]
        auto = FakeConfigEntity(
            "automation.k",
            "Kitchen Auto",
            "uid",
            {"id": "uid", "alias": "Kitchen Auto"},
        )
        h = FakeHass(states=states, data={"automation": FakeComponent([auto])})
        only_entity = wsapi._do_search(
            h, {"query": "kitchen", "search_types": ["entity"]}
        )
        assert only_entity["entities"] and not only_entity["automations"]
        only_auto = wsapi._do_search(
            h, {"query": "kitchen", "search_types": ["automation"]}
        )
        assert only_auto["automations"] and not only_auto["entities"]

    def test_inaccessible_component_counted_in_diagnostics(self, empty_view):
        # No "automation" key in hass.data -> component inaccessible.
        res = wsapi._do_search(
            FakeHass(), {"query": "x", "search_types": ["automation"]}
        )
        assert res["automations"] == []
        assert res["diagnostics"]["config_components_inaccessible"] == 1


# =============================================================================
# helpers — flow-helper options indexed; entry.data must never leak
# =============================================================================
class TestHelpers:
    def test_flow_helper_options_indexed_data_never_leaks(self, empty_view):
        entry = FakeConfigEntry(
            "template",
            title="Sun Sensor",
            options={"state": "{{ is_state('sun.sun', 'above_horizon') }}"},
            data={"api_key": "DATA_SECRET_XYZ"},
            entry_id="e1",
        )
        h = FakeHass(config_entries=[entry])

        res = wsapi._do_search(h, {"query": "sun", "include_config": True})
        flow = [x for x in res["helpers"] if x["kind"] == "flow"]
        assert flow
        assert flow[0]["helper_type"] == "template"
        assert flow[0]["entry_id"] == "e1"
        assert flow[0]["options"] == {
            "state": "{{ is_state('sun.sun', 'above_horizon') }}"
        }
        serialized = json.dumps(res)
        assert "DATA_SECRET_XYZ" not in serialized
        assert "api_key" not in serialized

        by_option = wsapi._do_search(h, {"query": "above_horizon"})
        assert [x for x in by_option["helpers"] if x["kind"] == "flow"]

    def test_flow_helper_options_withheld_without_include_config(self, empty_view):
        entry = FakeConfigEntry(
            "template",
            title="Sun Sensor",
            options={"state": "x"},
            data={},
            entry_id="e1",
        )
        res = wsapi._do_search(FakeHass(config_entries=[entry]), {"query": "sun"})
        flow = [x for x in res["helpers"] if x["kind"] == "flow"]
        assert flow and flow[0]["options"] is None

    def test_collection_helper_indexed(self, empty_view):
        states = [FakeState("input_boolean.guest_mode", "off", "Guest Mode")]
        res = wsapi._do_search(
            FakeHass(states=states), {"query": "guest", "search_types": ["helper"]}
        )
        coll = [x for x in res["helpers"] if x["kind"] == "collection"]
        assert coll
        assert coll[0]["helper_type"] == "input_boolean"
        assert coll[0]["object_id"] == "guest_mode"
        assert coll[0]["config"] is None


# =============================================================================
# scorer parity (golden corpus)
# =============================================================================
_PARITY_CORPUS = [
    ("light.kitchen", "Kitchen Light"),
    ("light.kitchen_ceiling", "Kitchen Ceiling"),
    ("switch.kitchen", "Kitchen Switch"),
    ("sensor.temperature", "Temperature"),
    ("light.bedroom", "Bedroom Light"),
]
_PARITY_HIDDEN = {"light.bedroom"}
_PARITY_QUERIES = [
    "kitchen",
    "light.kitchen",
    "temperature",
    "bedroom",
    "kitchen light",
]


class TestScorerParity:
    def _server_ranking(self, query_lower):
        ranked = []
        for entity_id, friendly in _PARITY_CORPUS:
            entity = {
                "entity_id": entity_id,
                "attributes": {"friendly_name": friendly},
                "state": "on",
            }
            match = _match_exact_search_entity(
                entity, query_lower, None, set(), _PARITY_HIDDEN, True
            )
            if match:
                ranked.append((match["entity_id"], match["score"]))
        ranked.sort(key=lambda x: (-x[1], x[0]))
        return ranked

    def _component_ranking(self, query_lower):
        states = [FakeState(eid, "on", fn) for eid, fn in _PARITY_CORPUS]
        view = make_view(
            entity={
                eid: FakeRegEntry(
                    eid, hidden_by=("user" if eid in _PARITY_HIDDEN else None)
                )
                for eid, _ in _PARITY_CORPUS
            }
        )
        recs = wsapi._search_entities(
            FakeHass(states=states),
            view,
            query_lower,
            match_all=False,
            exact=True,
            include_hidden=True,
            domain_filter=None,
            area_filter=None,
            state_filter=None,
        )
        recs.sort(key=lambda r: (-r["score"], r["entity_id"]))
        return [(r["entity_id"], r["score"]) for r in recs]

    def test_exact_mode_ranked_ids_and_scores_match_server(self):
        for query in _PARITY_QUERIES:
            ql = query.lower()
            assert self._component_ranking(ql) == self._server_ranking(ql), (
                f"parity broke for query={query!r}"
            )

    def test_fuzzy_tiers_match_calculate_ratio(self):
        # exact token -> 100
        assert wsapi._text_tier("kitchen", ["kitchen"], fuzzy=True) == 100
        # substring -> 80 (not the fuzzy ratio)
        assert wsapi._text_tier("kit", ["kitchen"], fuzzy=True) == 80
        # typo within threshold -> the server's calculate_ratio value exactly
        typo = wsapi._text_tier("kitchne", ["kitchen"], fuzzy=True)
        assert typo == calculate_ratio("kitchne", "kitchen")
        assert typo >= wsapi.FUZZY_THRESHOLD
        # below threshold -> no match
        assert wsapi._text_tier("zzzzzzzz", ["kitchen"], fuzzy=True) is None
        # exact mode never fuzzy-matches a typo
        assert wsapi._text_tier("kitchne", ["kitchen"], fuzzy=False) is None

    def test_config_exact_is_binary_100(self):
        # config name substring => 100 (not the entity 80 tier)
        scored = wsapi._config_score(
            "kit",
            "automation.kit",
            "Kitchen Auto",
            {"alias": "Kitchen Auto"},
            exact=True,
        )
        assert scored is not None
        total, in_name, in_config = scored
        assert total == 100 and in_name is True


# =============================================================================
# registration, admin gate, malformed params (functional decorators)
# =============================================================================
class _Unauthorized(Exception):
    pass


class _FakeUser:
    def __init__(self, is_admin):
        self.is_admin = is_admin


class _FakeConnection:
    def __init__(self, is_admin=True, has_user=True):
        self.user = _FakeUser(is_admin) if has_user else None
        self.results = {}

    def send_result(self, msg_id, result):
        self.results[msg_id] = result


class _FakeWSApi:
    """Functional stand-in for homeassistant.components.websocket_api."""

    def __init__(self):
        self.registered = {}

    def websocket_command(self, schema):
        command = next(v for k, v in schema.items() if str(k) == "type")

        def decorate(func):
            func._ws_command = command
            func._ws_schema = schema
            return func

        return decorate

    def require_admin(self, func):
        @functools.wraps(func)
        def wrapper(hass, connection, msg):
            user = connection.user
            if user is None or not user.is_admin:
                raise _Unauthorized()
            return func(hass, connection, msg)

        return wrapper

    def async_response(self, func):
        @functools.wraps(func)
        def wrapper(hass, connection, msg):
            coro = func(hass, connection, msg)
            try:
                coro.send(None)
            except StopIteration:
                return
            coro.close()
            raise AssertionError("handler awaited unexpectedly")

        return wrapper

    def async_register_command(self, hass, handler):
        self.registered[handler._ws_command] = handler


@pytest.fixture
def functional_ws(monkeypatch):
    fake = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", fake)
    monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
    monkeypatch.setattr(
        wsapi, "_resolve_registries", lambda hass: wsapi._RegistryView()
    )
    wsapi.async_register_commands(FakeHass())
    return fake


class TestRegistrationAndAdminGate:
    def test_both_commands_registered(self, functional_ws):
        assert set(functional_ws.registered) == {wsapi.WS_INFO, wsapi.WS_SEARCH}

    @pytest.mark.parametrize("command", ["ha_mcp_tools/info", "ha_mcp_tools/search"])
    def test_non_admin_rejected(self, functional_ws, command):
        handler = functional_ws.registered[command]
        conn = _FakeConnection(is_admin=False)
        with pytest.raises(_Unauthorized):
            handler(FakeHass(), conn, {"id": 1, "type": command})

    @pytest.mark.parametrize("command", ["ha_mcp_tools/info", "ha_mcp_tools/search"])
    def test_no_user_rejected(self, functional_ws, command):
        handler = functional_ws.registered[command]
        conn = _FakeConnection(has_user=False)
        with pytest.raises(_Unauthorized):
            handler(FakeHass(), conn, {"id": 2, "type": command})

    @pytest.mark.parametrize("command", ["ha_mcp_tools/info", "ha_mcp_tools/search"])
    def test_admin_call_sends_result(self, functional_ws, command):
        handler = functional_ws.registered[command]
        conn = _FakeConnection(is_admin=True)
        handler(FakeHass(), conn, {"id": 9, "type": command})
        assert 9 in conn.results
        assert isinstance(conn.results[9], dict)


class TestSchemaValidation:
    def _schema(self, monkeypatch):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        return _REAL_VOL.Schema(wsapi._search_schema())

    def test_valid_params_apply_defaults(self, monkeypatch):
        schema = self._schema(monkeypatch)
        out = schema({"type": wsapi.WS_SEARCH, "query": "kitchen"})
        assert out["exact"] is True
        assert out["include_hidden"] is True
        assert out["include_config"] is False
        assert out["limit"] == wsapi.DEFAULT_LIMIT
        assert out["offset"] == 0

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "ha_mcp_tools/search", "limit": 0},
            {"type": "ha_mcp_tools/search", "limit": 9999},
            {"type": "ha_mcp_tools/search", "offset": -1},
            {"type": "ha_mcp_tools/search", "search_types": ["bogus"]},
            {"type": "ha_mcp_tools/search", "exact": "yes"},
        ],
    )
    def test_malformed_params_rejected(self, monkeypatch, bad):
        schema = self._schema(monkeypatch)
        with pytest.raises(_REAL_VOL.Invalid):
            schema(bad)
