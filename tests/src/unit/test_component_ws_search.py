"""Unit tests for the ha_mcp_tools in-process WebSocket command surface.

Mirrors the established component-test pattern (``test_caller_token_auth.py`` /
``test_custom_component_filesystem.py``): the ``homeassistant.*`` imports are
stubbed with ``MagicMock`` and the pure ``_do_*`` functions are exercised with
fake hass / registry objects injected through the ``_resolve_registries`` seam.

Covers all five v1.1.0 commands (info / search / config_get / overview /
helpers_list). Highlights:
* ``_do_info`` handshake shape + manifest/const version parity (drift guard);
  ``info`` advertising all four capabilities.
* search: entity joins (name / alias / area / floor / label / domain / device);
  YAML config body indexed but NEVER emitted; storage body only under
  ``include_config``; flow-helper ``options`` indexed while ``entry.data`` never
  leaks; pagination / include_hidden / match-all / search_types gating; scorer
  parity against the server's ``_match_exact_search_entity`` / ``calculate_ratio``.
* config_get: storage-item full payload; YAML -> structured not-found with the
  body ABSENT everywhere; id / entity_id / slug resolution.
* overview: the raw slices (states / services / three registries / config /
  notifications / repairs) shaped for the server's existing overview logic.
* helpers_list: collection + flow helpers; ``entry.data`` negative scan; rename
  (issue #1794) shows current values; helper_types filter.
* admin gate + async_response on all five registered commands.
* malformed-params rejection via voluptuous for every command schema.
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


class FakeServices:
    """Stand-in for ``hass.services`` (``async_services`` mapping)."""

    def __init__(self, services):
        # services: {domain: {service_name: <anything>}}
        self._services = {d: dict(s) for d, s in dict(services).items()}

    def async_services(self):
        return dict(self._services)


class FakeHass:
    def __init__(
        self, states=(), data=None, config_entries=(), services=None, config=None
    ):
        self.states = FakeStates(states)
        self.data = dict(data or {})
        self.config_entries = FakeConfigEntries(config_entries)
        if services is not None:
            self.services = services
        if config is not None:
            self.config = config


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
        unique_id=None,
        original_name=None,
        categories=None,
        config_entry_id=None,
        entity_category=None,
        platform=None,
        disabled_by=None,
    ):
        self.entity_id = entity_id
        self.aliases = set(aliases)
        self.area_id = area_id
        self.device_id = device_id
        self.labels = set(labels)
        self.hidden_by = hidden_by
        self.name = name
        self.unique_id = unique_id
        self.original_name = original_name
        self.categories = dict(categories or {})
        self.config_entry_id = config_entry_id
        self.entity_category = entity_category
        self.platform = platform
        self.disabled_by = disabled_by


class FakeEntityReg:
    def __init__(self, entries):
        self._entries = dict(entries)
        # Real HA exposes ``registry.entities`` as a mapping; overview /
        # helpers_list iterate it, while search uses ``async_get``.
        self.entities = dict(entries)

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

    def async_list_areas(self):
        return list(self._areas.values())


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
        # Real HA exposes ``registry.devices`` as a mapping (overview reads it).
        self.devices = dict(self._devices)

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


class FakeConfig:
    """Stand-in for ``hass.config``: ``path()`` roots at a temp dir; ``as_dict()``
    returns the injected HA-config payload (overview's system-info slice)."""

    def __init__(self, base_dir=None, data=None):
        self._base = Path(base_dir) if base_dir is not None else None
        self._data = dict(data or {})

    def path(self, *parts):
        return str(self._base.joinpath(*parts))

    def as_dict(self):
        return dict(self._data)


class FakeIssue:
    """Stand-in for an issue-registry ``IssueEntry`` (repairs slice)."""

    def __init__(
        self,
        issue_id,
        domain,
        *,
        severity="warning",
        translation_key=None,
        dismissed_version=None,
        is_fixable=True,
        breaks_in_ha_version=None,
        created=None,
        issue_domain=None,
        translation_placeholders=None,
        learn_more_url=None,
        active=True,
    ):
        self.issue_id = issue_id
        self.domain = domain
        self.severity = severity
        self.translation_key = translation_key
        self.dismissed_version = dismissed_version
        self.is_fixable = is_fixable
        self.breaks_in_ha_version = breaks_in_ha_version
        self.created = created
        self.issue_domain = issue_domain
        self.translation_placeholders = translation_placeholders
        self.learn_more_url = learn_more_url
        self.active = active


class FakeIssueRegistry:
    """Stand-in for the issue registry: ``.issues`` maps (domain, id) -> IssueEntry."""

    def __init__(self, issues):
        self.issues = {(i.domain, i.issue_id): i for i in issues}


class FakeIssueRegModule:
    """Stand-in for the ``issue_registry`` module (``async_get`` seam)."""

    def __init__(self, registry):
        self._registry = registry

    def async_get(self, hass):
        return self._registry


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
        assert info["capabilities"] == [
            "search",
            "config_get",
            "overview",
            "helpers_list",
        ]
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

    def test_underscore_space_query_equivalence(self, empty_view):
        """Separator-normalized fuzzy matching: ``input_boolean`` and
        ``input boolean`` queries must return the same result set (mirrors
        e2e test_fuzzy_search_underscore_space_equivalence — the server's
        BM25 tokenizes both sides, so the component's tier scorer compares
        separator-normalized forms in fuzzy mode)."""
        states = [
            FakeState("input_boolean.guests", "off", "We Have Guests"),
            FakeState("input_boolean.dark_mode", "on", "Dark Mode"),
            FakeState("light.kitchen", "on", "Kitchen"),
        ]
        h = FakeHass(states=states)
        underscore = wsapi._do_search(
            h, {"query": "input_boolean", "exact": False, "limit": 20}
        )
        space = wsapi._do_search(
            h, {"query": "input boolean", "exact": False, "limit": 20}
        )
        ids_u = {e["entity_id"] for e in underscore["entities"]}
        ids_s = {e["entity_id"] for e in space["entities"]}
        assert ids_u == ids_s and len(ids_u) == 2, f"underscore={ids_u} space={ids_s}"
        assert underscore["entity_total_matches"] == space["entity_total_matches"] == 2
        # Exact mode keeps raw substring semantics (server parity): the space
        # form matches nothing exactly.
        exact_space = wsapi._do_search(
            h, {"query": "input boolean", "exact": True, "limit": 20}
        )
        assert exact_space["entity_total_matches"] == 0

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

    def test_collection_helper_body_indexed_by_option_value(self, empty_view):
        """Mirror e2e ``test_deep_search_helper``: an input_select's option value
        lives in the state attributes, not the name, and must be searchable +
        report ``match_in_config`` (parity with the legacy ``<type>/list`` body
        search) and, under include_config, emit that body."""
        states = [
            FakeState(
                "input_select.house_mode",
                "day",
                friendly_name="House Mode",
                options=["day", "deep_search_option_a", "night"],
            )
        ]
        res = wsapi._do_search(
            FakeHass(states=states),
            {
                "query": "deep_search_option_a",
                "search_types": ["helper"],
                "include_config": True,
            },
        )
        coll = [x for x in res["helpers"] if x["kind"] == "collection"]
        assert coll, "option-value query must find the input_select helper"
        rec = coll[0]
        assert rec["helper_type"] == "input_select"
        assert rec["match_in_config"] is True
        assert rec["match_in_name"] is False
        assert "deep_search_option_a" in json.dumps(rec["config"])

    def test_collection_helper_body_withheld_without_include_config(self, empty_view):
        states = [
            FakeState(
                "input_select.house_mode",
                "day",
                friendly_name="House Mode",
                options=["deep_search_option_a"],
            )
        ]
        res = wsapi._do_search(
            FakeHass(states=states),
            {"query": "deep_search_option_a", "search_types": ["helper"]},
        )
        coll = [x for x in res["helpers"] if x["kind"] == "collection"]
        assert coll and coll[0]["config"] is None

    def test_flow_helper_mappingproxy_options_indexed(self, empty_view):
        """Regression: ``ConfigEntry.options`` is a ``MappingProxyType`` in live
        HA, not a ``dict``. The old ``isinstance(..., dict)`` guard dropped it to
        ``{}`` so a template helper's body was never searchable — e2e
        ``test_deep_search_finds_ui_template_helper`` /
        ``test_deep_search_flow_helper_fuzzy_probes_config`` timed out. A body
        token must match through a MappingProxy in both exact and fuzzy mode."""
        from types import MappingProxyType

        marker = "deepsearchtemplatebody4471"
        entry = FakeConfigEntry(
            "template",
            title="Deep Search Template Helper",
            options={
                "name": "Deep Search Template Helper",
                "state": "{{ states('sensor." + marker + "') }}",
            },
            data={},
            entry_id="e1",
        )
        # Reproduce the live-HA type exactly: options is a read-only proxy.
        entry.options = MappingProxyType(dict(entry.options))
        h = FakeHass(config_entries=[entry])

        for exact in (True, False):
            res = wsapi._do_search(
                h,
                {
                    "query": marker,
                    "search_types": ["helper"],
                    "exact": exact,
                    "include_config": True,
                },
            )
            flow = [x for x in res["helpers"] if x["kind"] == "flow"]
            assert flow, f"body token must match through MappingProxy (exact={exact})"
            assert flow[0]["entry_id"] == "e1"
            assert flow[0]["helper_type"] == "template"
            assert flow[0]["match_in_config"] is True
            assert marker in json.dumps(flow[0]["options"])


def test_flow_helper_domains_cover_server_flow_helper_types():
    """The component must index every domain the server routes as a flow helper,
    or a UI-created helper of a covered type would be invisible to the component
    path (e2e ``test_deep_search_finds_non_template_flow_helpers``)."""
    from ha_mcp.tools.config_entry_flow import FLOW_HELPER_TYPES

    missing = set(FLOW_HELPER_TYPES) - wsapi.FLOW_HELPER_DOMAINS
    assert not missing, f"component FLOW_HELPER_DOMAINS misses server types: {missing}"


# =============================================================================
# secret scrub — resolved !secret plaintext is BLOCKED from the match corpus
# =============================================================================
class TestSecretScrub:
    """A resolved ``!secret`` value in a config body must never produce a match,
    so ``ha_search`` cannot be used as a probe oracle (query a suspected secret,
    confirm it via ``match_in_config``). The value is blocked, not just unemitted.
    """

    _SECRET = "s3cr3tprobevaluexyz"

    def _yaml_automation_hass(self, secret_value):
        # A YAML-defined automation (unique_id=None → body never emitted) whose
        # body carries a resolved secret next to a normal, non-secret token.
        auto = FakeConfigEntity(
            "automation.leaky",
            "Leaky Auto",
            unique_id=None,
            raw_config={
                "alias": "Leaky Auto",
                "action": [
                    {
                        "service": "notify.x",
                        "data": {
                            "api_password": secret_value,
                            "message": "normalbodytoken",
                        },
                    }
                ],
            },
        )
        return FakeHass(data={"automation": FakeComponent([auto])})

    def _write_secrets(self, tmp_path, **values):
        body = "".join(f"{k}: {v}\n" for k, v in values.items())
        (tmp_path / "secrets.yaml").write_text(body, encoding="utf-8")

    def test_secret_value_scrubbed_but_normal_token_matches(self, empty_view, tmp_path):
        self._write_secrets(tmp_path, api_password=self._SECRET)
        h = self._yaml_automation_hass(self._SECRET)
        h.config = FakeConfig(tmp_path)

        by_secret = wsapi._do_search(
            h, {"query": self._SECRET, "search_types": ["automation"]}
        )
        assert not by_secret["automations"], (
            "a query equal to a resolved secret must not match (probe oracle)"
        )

        by_token = wsapi._do_search(
            h, {"query": "normalbodytoken", "search_types": ["automation"]}
        )
        assert any(a["match_in_config"] for a in by_token["automations"]), (
            "a non-secret body token must still match after scrubbing"
        )

    def test_secret_scrubbed_in_fuzzy_mode(self, empty_view, tmp_path):
        self._write_secrets(tmp_path, api_password=self._SECRET)
        h = self._yaml_automation_hass(self._SECRET)
        h.config = FakeConfig(tmp_path)
        res = wsapi._do_search(
            h,
            {"query": self._SECRET, "search_types": ["automation"], "exact": False},
        )
        assert not res["automations"], "fuzzy mode must also scrub the secret leaf"

    def test_flow_helper_option_secret_scrubbed(self, empty_view, tmp_path):
        self._write_secrets(tmp_path, tmpl_secret=self._SECRET)
        entry = FakeConfigEntry(
            "template",
            title="Sun Sensor",
            options={"state": self._SECRET, "name": "Sun Sensor"},
            entry_id="e1",
        )
        h = FakeHass(config_entries=[entry])
        h.config = FakeConfig(tmp_path)
        res = wsapi._do_search(h, {"query": self._SECRET, "search_types": ["helper"]})
        assert not [x for x in res["helpers"] if x["kind"] == "flow"], (
            "a flow-helper option equal to a secret must not match"
        )

    def test_missing_secrets_file_degrades_without_scrubbing(
        self, empty_view, tmp_path
    ):
        # No secrets.yaml under tmp_path → no scrub → the value matches (proves
        # the degrade-off path runs without error).
        h = self._yaml_automation_hass(self._SECRET)
        h.config = FakeConfig(tmp_path)
        res = wsapi._do_search(
            h, {"query": self._SECRET, "search_types": ["automation"]}
        )
        assert res["automations"], (
            "absent secrets.yaml must degrade to no scrubbing without error"
        )

    def test_malformed_secrets_file_degrades_without_scrubbing(
        self, empty_view, tmp_path
    ):
        (tmp_path / "secrets.yaml").write_text("{not: valid: yaml: [", encoding="utf-8")
        h = self._yaml_automation_hass(self._SECRET)
        h.config = FakeConfig(tmp_path)
        res = wsapi._do_search(
            h, {"query": self._SECRET, "search_types": ["automation"]}
        )
        assert res["automations"], (
            "malformed secrets.yaml must degrade to no scrubbing without error"
        )

    def test_non_string_secret_values_ignored(self, empty_view, tmp_path):
        # A numeric secret is not a plaintext-leak leaf; only string values are
        # collected, so loading must not choke on it.
        (tmp_path / "secrets.yaml").write_text(
            f"port: 8123\napi_password: {self._SECRET}\n", encoding="utf-8"
        )
        h = self._yaml_automation_hass(self._SECRET)
        h.config = FakeConfig(tmp_path)
        res = wsapi._do_search(
            h, {"query": self._SECRET, "search_types": ["automation"]}
        )
        assert not res["automations"], (
            "string secret still scrubbed alongside a non-string one"
        )

    def test_entity_only_search_skips_secrets_read(
        self, empty_view, tmp_path, monkeypatch
    ):
        # Entity-only searches never touch a config body, so the secrets.yaml read
        # is skipped entirely (perf gate).
        calls = {"n": 0}

        def _spy(hass):
            calls["n"] += 1
            return frozenset()

        monkeypatch.setattr(wsapi, "_load_secret_values", _spy)
        h = FakeHass(states=[FakeState("light.k", "on", "Kitchen")])
        h.config = FakeConfig(tmp_path)
        wsapi._do_search(h, {"query": "kitchen", "search_types": ["entity"]})
        assert calls["n"] == 0, "entity-only search must not read secrets.yaml"


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
# match_type taxonomy parity (Group 3 — #1166 / #1170 finding 8)
# =============================================================================
class TestMatchTypeTaxonomy:
    """The component's fuzzy match_type must mirror the server's taxonomy —
    ``alias_match`` for an alias-driven hit, the ``_get_match_type`` tiers
    otherwise — while exact mode keeps the server's flat ``exact_match``.
    """

    def _match_type(self, monkeypatch, entity_id, friendly, aliases, query, *, exact):
        view = make_view(entity={entity_id: FakeRegEntry(entity_id, aliases=aliases)})
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
        recs = wsapi._search_entities(
            FakeHass(states=[FakeState(entity_id, "on", friendly)]),
            view,
            query.lower(),
            match_all=False,
            exact=exact,
            include_hidden=True,
            domain_filter=None,
            area_filter=None,
            state_filter=None,
        )
        assert recs, f"expected a hit for {query!r}"
        return recs[0]["match_type"]

    def test_alias_match_labeled(self, monkeypatch):
        # Mirror e2e test_search_finds_entity_by_alias_issue_1170: a fuzzy query
        # equal to an alias the id/name don't carry is labeled alias_match.
        mt = self._match_type(
            monkeypatch,
            "input_boolean.alias_src",
            "Alias Source",
            {"e2e1170aliasabcd"},
            "e2e1170aliasabcd",
            exact=False,
        )
        assert mt == "alias_match"

    def test_exact_mode_is_flat_exact_match(self, monkeypatch):
        mt = self._match_type(
            monkeypatch, "light.kitchen", "Kitchen Light", (), "kitchen", exact=True
        )
        assert mt == "exact_match"

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("light.kitchen", "exact_id"),
            ("kitchen light", "exact_name"),
            ("light", "exact_domain"),
            ("kitch", "partial_id"),
            ("chen ligh", "partial_name"),
        ],
    )
    def test_fuzzy_tier_mapping(self, monkeypatch, query, expected):
        mt = self._match_type(
            monkeypatch, "light.kitchen", "Kitchen Light", (), query, exact=False
        )
        assert mt == expected

    def test_alias_match_parity_with_server_engine(self, monkeypatch):
        """Cross-check the alias case against the server's own
        ``FuzzyEntitySearcher``: both label it ``alias_match`` for the same
        entity, so the taxonomies cannot silently drift."""
        from ha_mcp.utils.fuzzy_search import FuzzyEntitySearcher

        entity_id, friendly, alias = (
            "input_boolean.alias_src",
            "Alias Source",
            "e2e1170aliasabcd",
        )
        server_matches, _ = FuzzyEntitySearcher().search_entities(
            [
                {
                    "entity_id": entity_id,
                    "attributes": {"friendly_name": friendly},
                    "state": "on",
                    "_aliases": [alias],
                }
            ],
            alias,
        )
        server_mt = next(
            m["match_type"] for m in server_matches if m["entity_id"] == entity_id
        )
        component_mt = self._match_type(
            monkeypatch, entity_id, friendly, {alias}, alias, exact=False
        )
        assert component_mt == server_mt == "alias_match"


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


_ALL_COMMANDS = [
    "ha_mcp_tools/info",
    "ha_mcp_tools/search",
    "ha_mcp_tools/config_get",
    "ha_mcp_tools/overview",
    "ha_mcp_tools/helpers_list",
]

# Minimal well-formed message body per command (Required fields) so the admin
# gate / async_response wrappers reach the pure handler.
_CMD_MSG_EXTRA = {
    "ha_mcp_tools/config_get": {"domain": "automation", "item_id": "nope"},
}


class TestRegistrationAndAdminGate:
    def test_all_commands_registered(self, functional_ws):
        assert set(functional_ws.registered) == {
            wsapi.WS_INFO,
            wsapi.WS_SEARCH,
            wsapi.WS_CONFIG_GET,
            wsapi.WS_OVERVIEW,
            wsapi.WS_HELPERS_LIST,
        }

    @pytest.mark.parametrize("command", _ALL_COMMANDS)
    def test_non_admin_rejected(self, functional_ws, command):
        handler = functional_ws.registered[command]
        conn = _FakeConnection(is_admin=False)
        with pytest.raises(_Unauthorized):
            handler(FakeHass(), conn, {"id": 1, "type": command})

    @pytest.mark.parametrize("command", _ALL_COMMANDS)
    def test_no_user_rejected(self, functional_ws, command):
        handler = functional_ws.registered[command]
        conn = _FakeConnection(has_user=False)
        with pytest.raises(_Unauthorized):
            handler(FakeHass(), conn, {"id": 2, "type": command})

    @pytest.mark.parametrize("command", _ALL_COMMANDS)
    def test_admin_call_sends_result(self, functional_ws, command):
        handler = functional_ws.registered[command]
        conn = _FakeConnection(is_admin=True)
        msg = {"id": 9, "type": command, **_CMD_MSG_EXTRA.get(command, {})}
        handler(FakeHass(), conn, msg)
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


class TestNewCommandSchemas:
    """Voluptuous validation for config_get / overview / helpers_list."""

    def _schema(self, monkeypatch, schema_fn):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        return _REAL_VOL.Schema(schema_fn())

    def test_config_get_valid(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._config_get_schema)
        out = schema(
            {"type": wsapi.WS_CONFIG_GET, "domain": "script", "item_id": "abc"}
        )
        assert out["domain"] == "script"
        assert out["item_id"] == "abc"

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "ha_mcp_tools/config_get", "domain": "light", "item_id": "x"},
            # scene is excluded from config_get (legacy-only; no in-memory body).
            {"type": "ha_mcp_tools/config_get", "domain": "scene", "item_id": "x"},
            {"type": "ha_mcp_tools/config_get", "domain": "automation"},  # no item_id
            {"type": "ha_mcp_tools/config_get", "item_id": "x"},  # no domain
        ],
    )
    def test_config_get_malformed_rejected(self, monkeypatch, bad):
        schema = self._schema(monkeypatch, wsapi._config_get_schema)
        with pytest.raises(_REAL_VOL.Invalid):
            schema(bad)

    def test_overview_defaults(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._overview_schema)
        out = schema({"type": wsapi.WS_OVERVIEW})
        assert out["include_notifications"] is True
        assert out["include_repairs"] is True

    def test_overview_malformed_rejected(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._overview_schema)
        with pytest.raises(_REAL_VOL.Invalid):
            schema({"type": wsapi.WS_OVERVIEW, "include_notifications": "yes"})

    def test_helpers_list_defaults(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._helpers_list_schema)
        out = schema({"type": wsapi.WS_HELPERS_LIST})
        assert out["include_flow_helpers"] is True

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "ha_mcp_tools/helpers_list", "helper_types": "template"},
            {"type": "ha_mcp_tools/helpers_list", "include_flow_helpers": "yes"},
        ],
    )
    def test_helpers_list_malformed_rejected(self, monkeypatch, bad):
        schema = self._schema(monkeypatch, wsapi._helpers_list_schema)
        with pytest.raises(_REAL_VOL.Invalid):
            schema(bad)


# =============================================================================
# config_get — storage-only body fetch; YAML -> structured not-found (no body)
# =============================================================================
class TestConfigGet:
    def _storage_hass(self):
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
        yaml_auto = FakeConfigEntity(
            "automation.pkg",
            "Package Auto",
            unique_id=None,
            raw_config={
                "alias": "Package Auto",
                "action": [{"service": "notify.x", "data": {"message": "YAMLSECRET"}}],
            },
        )
        h = FakeHass(
            states=[FakeState("automation.ui", "on", "UI Auto Live")],
            data={"automation": FakeComponent([storage_auto, yaml_auto])},
        )
        return h

    def _view(self):
        return make_view(
            entity={
                "automation.ui": FakeRegEntry(
                    "automation.ui", categories={"automation": "cat-morning"}
                )
            }
        )

    def test_storage_item_full_payload(self, monkeypatch):
        h = self._storage_hass()
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: self._view())
        res = wsapi._do_config_get(h, {"domain": "automation", "item_id": "uid-1"})
        assert res["found"] is True
        assert res["source"] == "storage"
        assert res["domain"] == "automation"
        assert res["item_id"] == "uid-1"
        assert res["entity_id"] == "automation.ui"
        # Current friendly_name comes from the live state, not the storage name.
        assert res["friendly_name"] == "UI Auto Live"
        assert res["config"]["id"] == "uid-1"
        assert res["category"] == "cat-morning"

    @pytest.mark.parametrize("item_id", ["uid-1", "automation.ui", "ui"])
    def test_resolves_by_id_entity_id_or_slug(self, monkeypatch, item_id):
        h = self._storage_hass()
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: self._view())
        res = wsapi._do_config_get(h, {"domain": "automation", "item_id": item_id})
        assert res["found"] is True
        assert res["entity_id"] == "automation.ui"

    def test_yaml_item_structured_not_found_no_body(self, empty_view):
        h = self._storage_hass()
        res = wsapi._do_config_get(
            h, {"domain": "automation", "item_id": "automation.pkg"}
        )
        assert res["found"] is False
        assert res["source"] == "yaml"
        assert res["entity_id"] == "automation.pkg"
        # The YAML body must never appear anywhere in the response.
        assert "config" not in res
        assert "YAMLSECRET" not in json.dumps(res)

    def test_absent_item_not_found(self, empty_view):
        h = self._storage_hass()
        res = wsapi._do_config_get(h, {"domain": "automation", "item_id": "ghost"})
        assert res["found"] is False
        assert res["source"] is None
        assert "config" not in res

    def test_inaccessible_component_not_found(self, empty_view):
        res = wsapi._do_config_get(
            FakeHass(), {"domain": "script", "item_id": "whatever"}
        )
        assert res["found"] is False
        assert res["source"] is None

    def test_scene_domain_rejected_by_schema(self, monkeypatch):
        """Scenes are NOT a valid config_get domain — the voluptuous schema
        rejects ``domain='scene'``. ``ha_config_get_scene`` stays on its legacy
        REST path because a ``HomeAssistantScene`` holds no raw storage body in
        memory (``scene_config.states`` is runtime State objects). config_get is
        automation/script only."""
        assert "scene" not in wsapi.CONFIG_GET_DOMAINS
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        schema = _REAL_VOL.Schema(wsapi._config_get_schema())
        with pytest.raises(_REAL_VOL.Invalid):
            schema(
                {
                    "type": wsapi.WS_CONFIG_GET,
                    "domain": "scene",
                    "item_id": "movie_night",
                }
            )


# =============================================================================
# helpers_list — collection (live attrs) + flow (options, never entry.data)
# =============================================================================
class TestHelpersList:
    def test_collection_helper_listed_with_body(self, empty_view):
        states = [
            FakeState(
                "input_select.house_mode",
                "day",
                friendly_name="House Mode",
                options=["day", "night"],
            )
        ]
        res = wsapi._do_helpers_list(FakeHass(states=states), {})
        coll = [h for h in res["helpers"] if h["kind"] == "collection"]
        assert res["count"] == len(res["helpers"]) == 1
        rec = coll[0]
        assert rec["helper_type"] == "input_select"
        assert rec["entity_id"] == "input_select.house_mode"
        assert rec["object_id"] == "house_mode"
        assert rec["name"] == "House Mode"
        assert rec["config"]["options"] == ["day", "night"]

    def test_rename_shows_current_values_issue_1794(self, monkeypatch):
        # The storage collection name is stale ("Old Name"); the current name
        # (state friendly_name + registry override) must win — issue #1794.
        states = [
            FakeState("input_boolean.guest_mode", "off", friendly_name="Current Guest")
        ]
        view = make_view(
            entity={
                "input_boolean.guest_mode": FakeRegEntry(
                    "input_boolean.guest_mode",
                    name="Current Guest",
                    original_name="Old Name",
                    unique_id="guest_mode",
                )
            }
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
        res = wsapi._do_helpers_list(FakeHass(states=states), {})
        rec = next(h for h in res["helpers"] if h["kind"] == "collection")
        assert rec["name"] == "Current Guest"
        assert rec["storage_id"] == "guest_mode"
        assert "Old Name" not in json.dumps(res)

    def test_flow_helper_options_and_entity_data_never_leaks(self, monkeypatch):
        entry = FakeConfigEntry(
            "template",
            title="Sun Sensor",
            options={"state": "{{ is_state('sun.sun', 'above_horizon') }}"},
            data={"api_key": "DATA_SECRET_XYZ"},
            entry_id="e1",
        )
        # A registry entity bound to the config entry supplies the CURRENT
        # entity_id + display name (a rename updates the registry, not the entry
        # title).
        view = make_view(
            entity={
                "binary_sensor.sun_up": FakeRegEntry(
                    "binary_sensor.sun_up",
                    name="Sun Is Up",
                    config_entry_id="e1",
                )
            }
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
        res = wsapi._do_helpers_list(FakeHass(config_entries=[entry]), {})
        flow = [h for h in res["helpers"] if h["kind"] == "flow"]
        assert flow
        rec = flow[0]
        assert rec["helper_type"] == "template"
        assert rec["entry_id"] == "e1"
        assert rec["storage_id"] == "e1"
        assert rec["entity_id"] == "binary_sensor.sun_up"
        assert rec["name"] == "Sun Is Up"
        assert rec["options"] == {"state": "{{ is_state('sun.sun', 'above_horizon') }}"}
        serialized = json.dumps(res)
        assert "DATA_SECRET_XYZ" not in serialized
        assert "api_key" not in serialized

    def test_flow_helper_without_registered_entity(self, empty_view):
        entry = FakeConfigEntry(
            "group", title="Living Room Group", options={"entities": []}, entry_id="e9"
        )
        res = wsapi._do_helpers_list(FakeHass(config_entries=[entry]), {})
        rec = next(h for h in res["helpers"] if h["kind"] == "flow")
        assert rec["entity_id"] is None
        assert rec["name"] == "Living Room Group"

    def test_helper_types_filter(self, empty_view):
        states = [FakeState("input_boolean.guest", "off", "Guest")]
        entry = FakeConfigEntry(
            "template", title="Tmpl", options={"state": "x"}, entry_id="e1"
        )
        h = FakeHass(states=states, config_entries=[entry])
        only_tmpl = wsapi._do_helpers_list(h, {"helper_types": ["template"]})
        assert {r["helper_type"] for r in only_tmpl["helpers"]} == {"template"}
        only_bool = wsapi._do_helpers_list(h, {"helper_types": ["input_boolean"]})
        assert {r["helper_type"] for r in only_bool["helpers"]} == {"input_boolean"}

    def test_include_flow_helpers_false(self, empty_view):
        entry = FakeConfigEntry(
            "template", title="Tmpl", options={"state": "x"}, entry_id="e1"
        )
        res = wsapi._do_helpers_list(
            FakeHass(config_entries=[entry]), {"include_flow_helpers": False}
        )
        assert [r for r in res["helpers"] if r["kind"] == "flow"] == []

    def test_zone_and_person_are_listed_but_search_unaffected(self, empty_view):
        # helpers_list covers zone/person (consumer parity); search must not.
        states = [
            FakeState("zone.home", "zoning", friendly_name="Home"),
            FakeState("person.alice", "home", friendly_name="Alice"),
        ]
        listed = wsapi._do_helpers_list(FakeHass(states=states), {})
        kinds = {r["helper_type"] for r in listed["helpers"]}
        assert kinds == {"zone", "person"}
        assert {"zone", "person"} <= set(listed["covered_types"])
        # search's helper surface excludes zone/person (unchanged behaviour).
        searched = wsapi._do_search(
            FakeHass(states=states), {"query": "home", "search_types": ["helper"]}
        )
        assert searched["helpers"] == []

    def test_covered_types_advertises_the_full_enumerable_universe(self, empty_view):
        # covered_types is the anti-silent-wrong signal the server gates fallback
        # on: it must name every state-machine collection type + every flow type,
        # so the server trusts a genuinely-empty result for those.
        res = wsapi._do_helpers_list(FakeHass(), {})
        covered = set(res["covered_types"])
        # All 11 state-machine collection types the consumer accepts.
        assert {
            "input_boolean",
            "input_number",
            "input_text",
            "input_select",
            "input_datetime",
            "input_button",
            "counter",
            "timer",
            "schedule",
            "zone",
            "person",
        } <= covered
        # Flow types are covered too (they're enumerated by default).
        assert {"template", "group", "utility_meter"} <= covered

    def test_tag_is_not_covered_so_empty_is_not_authoritative(self, empty_view):
        # tag has no state entity, so the component cannot enumerate it. A
        # tag-only request returns empty AND omits tag from covered_types, telling
        # the server to fall back to its legacy tag/list rather than trust it.
        res = wsapi._do_helpers_list(FakeHass(), {"helper_types": ["tag"]})
        assert res["helpers"] == []
        assert "tag" not in res["covered_types"]

    def test_covered_types_excludes_flow_when_disabled(self, empty_view):
        res = wsapi._do_helpers_list(FakeHass(), {"include_flow_helpers": False})
        covered = set(res["covered_types"])
        assert "input_boolean" in covered
        # No flow types are covered when flow enumeration is turned off.
        assert covered.isdisjoint(wsapi.FLOW_HELPER_DOMAINS)


# =============================================================================
# overview — RAW slices (server runs its existing overview logic over them)
# =============================================================================
class _FakeEnum:
    """StrEnum-ish stand-in: ``.value`` is the wire string."""

    def __init__(self, value):
        self.value = value


class TestOverview:
    def _hass(self):
        return FakeHass(
            states=[
                FakeState(
                    "light.lamp", "on", friendly_name="Lamp", device_class="light"
                )
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
                    "allowlist_external_dirs": {"/config/www"},
                    "internal_url": "http://homeassistant.local:8123",
                }
            ),
            data={
                "persistent_notification": {
                    "n1": {
                        "notification_id": "n1",
                        "title": "Heads up",
                        "message": "Something",
                        "created_at": "2026-07-11T00:00:00+00:00",
                    }
                }
            },
        )

    def _view(self):
        return make_view(
            entity={
                "light.lamp": FakeRegEntry(
                    "light.lamp",
                    area_id="a1",
                    device_id="d1",
                    labels={"lb1"},
                    entity_category=_FakeEnum("config"),
                    hidden_by=_FakeEnum("user"),
                )
            },
            areas=[FakeArea("a1", "Office", floor_id="f1")],
            devices=[FakeDevice("d1", name="Lamp Device", area_id="a1")],
        )

    def test_raw_slices_present_and_shaped(self, monkeypatch):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: self._view())
        res = wsapi._do_overview(self._hass(), {})

        # states: bare list, get_states()-shaped
        assert isinstance(res["states"], list)
        st = res["states"][0]
        assert st["entity_id"] == "light.lamp"
        assert st["state"] == "on"
        assert st["attributes"]["friendly_name"] == "Lamp"

        # services: [{domain, services:{name:{}}}]
        assert res["services"] == [
            {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}}
        ]

        # registries: bare lists (NOT the {success, result} WS wrapper)
        assert isinstance(res["entity_registry"], list)
        ent = res["entity_registry"][0]
        assert ent["entity_id"] == "light.lamp"
        assert ent["area_id"] == "a1"
        assert ent["device_id"] == "d1"
        assert ent["labels"] == ["lb1"]
        # enum-ish registry fields are unwrapped to their wire strings
        assert ent["entity_category"] == "config"
        assert ent["hidden_by"] == "user"

        assert res["device_registry"] == [
            {
                "id": "d1",
                "area_id": "a1",
                "labels": [],
                "name": "Lamp Device",
                "name_by_user": None,
                "manufacturer": None,
                "model": None,
            }
        ]
        assert res["area_registry"] == [
            {"area_id": "a1", "name": "Office", "floor_id": "f1"}
        ]

        # config: HA-config fields, no base_url (server supplies that)
        assert res["config"]["version"] == "2026.7.0"
        assert res["config"]["location_name"] == "Home"
        assert "base_url" not in res["config"]
        # a set-valued config field is JSON-plainified to a list
        assert res["config"]["allowlist_external_dirs"] == ["/config/www"]

        # notifications
        assert res["notifications"] == [
            {
                "notification_id": "n1",
                "title": "Heads up",
                "message": "Something",
                "created_at": "2026-07-11T00:00:00+00:00",
            }
        ]

    def test_repairs_ignored_derived_from_dismissed_version(self, monkeypatch):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: self._view())
        registry = FakeIssueRegistry(
            [
                FakeIssue("active_issue", "mqtt", severity=_FakeEnum("warning")),
                FakeIssue("old_issue", "zwave", dismissed_version="2026.1.0"),
            ]
        )
        monkeypatch.setattr(wsapi, "ir", FakeIssueRegModule(registry))
        res = wsapi._do_overview(self._hass(), {})
        by_id = {r["issue_id"]: r for r in res["repairs"]}
        assert by_id["active_issue"]["ignored"] is False
        assert by_id["active_issue"]["severity"] == "warning"
        assert by_id["old_issue"]["ignored"] is True
        assert by_id["old_issue"]["dismissed_version"] == "2026.1.0"

    def test_include_flags_skip_sections(self, monkeypatch):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: self._view())
        monkeypatch.setattr(
            wsapi, "ir", FakeIssueRegModule(FakeIssueRegistry([FakeIssue("x", "y")]))
        )
        res = wsapi._do_overview(
            self._hass(),
            {"include_notifications": False, "include_repairs": False},
        )
        assert res["notifications"] == []
        assert res["repairs"] == []
        # core slices still present
        assert res["states"] and res["entity_registry"]

    def test_degrades_without_registries_or_config(self, monkeypatch):
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda hass: wsapi._RegistryView()
        )
        monkeypatch.setattr(wsapi, "ir", FakeIssueRegModule(FakeIssueRegistry([])))
        res = wsapi._do_overview(FakeHass(), {})
        assert res["entity_registry"] == []
        assert res["device_registry"] == []
        assert res["area_registry"] == []
        assert res["services"] == []
        assert res["config"] == {}
        assert res["notifications"] == []
        assert res["repairs"] == []
