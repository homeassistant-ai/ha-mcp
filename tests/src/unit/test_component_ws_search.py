"""Unit tests for the ha_mcp_tools in-process WebSocket command surface.

Mirrors the established component-test pattern (``test_caller_token_auth.py`` /
``test_custom_component_filesystem.py``): the ``homeassistant.*`` imports are
stubbed with ``MagicMock`` and the pure ``_do_*`` functions are exercised with
fake hass / registry objects injected through the ``_resolve_registries`` seam.

Covers the v1.1.1 command surface (info / search / overview / helpers_list /
states / blueprint_get / device_get / device_list / entity_enrich / exposure;
config_get was withdrawn pre-release). Highlights:
* ``_do_info`` handshake shape + manifest/const version parity (drift guard);
  ``info`` advertising every shipped capability.
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

import asyncio
import functools
import json
import logging
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
    def __init__(
        self,
        entity_id,
        state="on",
        friendly_name=None,
        last_changed="2026-07-16T00:00:00+00:00",
        last_updated="2026-07-16T00:00:00+00:00",
        **attrs,
    ):
        self.entity_id = entity_id
        self.state = state
        self.attributes = dict(attrs)
        if friendly_name is not None:
            self.attributes["friendly_name"] = friendly_name
        self.last_changed = last_changed
        self.last_updated = last_updated

    def as_dict(self):
        # Mirrors core ``State.as_dict()`` / the REST ``/api/states/<id>`` shape.
        # Timestamps are already ISO strings here — the real WS transport encodes
        # core's datetimes to the same isoformat, so the server sees plain JSON
        # either way (see websocket_api._do_states byte-parity note).
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "attributes": dict(self.attributes),
            "last_changed": self.last_changed,
            "last_updated": self.last_updated,
            "context": {"id": "01ABC", "parent_id": None, "user_id": None},
        }


class FakeStates:
    def __init__(self, states):
        self._states = list(states)
        self._by_id = {getattr(s, "entity_id", None): s for s in self._states}

    def async_all(self):
        return list(self._states)

    def get(self, entity_id):
        return self._by_id.get(entity_id)


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

    async def async_add_executor_job(self, func, *args):
        # The real hass runs ``func`` in a thread pool; the tests run it inline
        # (the point under test is that the WS prep offloads it, not the pool).
        return func(*args)


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

    @property
    def as_partial_dict(self):
        # Mirrors core ``RegistryEntry.as_partial_dict`` (one
        # ``config/entity_registry/list`` element): the shape device_get's
        # include_entities join returns VERBATIM. Sets rendered to lists as core
        # does; timestamps fixed floats (value irrelevant to the consumers).
        return {
            "area_id": self.area_id,
            "categories": dict(self.categories),
            "config_entry_id": self.config_entry_id,
            "config_subentry_id": None,
            "created_at": 0.0,
            "device_id": self.device_id,
            "disabled_by": self.disabled_by,
            "entity_category": self.entity_category,
            "entity_id": self.entity_id,
            "has_entity_name": False,
            "hidden_by": self.hidden_by,
            "icon": None,
            "id": self.unique_id,
            "labels": sorted(self.labels),
            "modified_at": 0.0,
            "name": self.name,
            "options": {},
            "original_name": self.original_name,
            "platform": self.platform,
            "translation_key": None,
            "unique_id": self.unique_id,
        }


class FakeEntityReg:
    def __init__(self, entries):
        self._entries = dict(entries)
        # Real HA exposes ``registry.entities`` as a mapping; overview /
        # helpers_list iterate it, while search uses ``async_get``.
        self.entities = dict(entries)

    def async_get(self, entity_id):
        return self._entries.get(entity_id)


class FakeErModule:
    """Faithful stand-in for the ``entity_registry`` module's device index.

    ``_do_device_get(include_entities)`` calls ``er.async_entries_for_device``;
    the real ``er`` is MagicMock-stubbed at import, so tests monkeypatch
    ``wsapi.er`` with this. Mirrors core's filter — disabled entities are excluded
    unless ``include_disabled_entities`` (the component passes True, matching
    ``config/entity_registry/list``)."""

    @staticmethod
    def async_entries_for_device(registry, device_id, include_disabled_entities=False):
        out = []
        for entry in registry.entities.values():
            if getattr(entry, "device_id", None) != device_id:
                continue
            if not include_disabled_entities and getattr(entry, "disabled_by", None):
                continue
            out.append(entry)
        return out


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
        identifiers=(),
        connections=(),
        config_entries=(),
        disabled_by=None,
        sw_version=None,
        hw_version=None,
        serial_number=None,
        via_device_id=None,
        model_id=None,
        configuration_url=None,
        entry_type=None,
        primary_config_entry=None,
    ):
        self.id = device_id
        self.name = name
        self.name_by_user = name_by_user
        self.area_id = area_id
        self.labels = set(labels)
        self.manufacturer = manufacturer
        self.model = model
        # DeviceEntry stores identifiers/connections/config_entries as sets of
        # tuples/strings; the caller passes tuples so they stay hashable.
        self.identifiers = set(identifiers)
        self.connections = set(connections)
        self.config_entries = set(config_entries)
        self.disabled_by = disabled_by
        self.sw_version = sw_version
        self.hw_version = hw_version
        self.serial_number = serial_number
        self.via_device_id = via_device_id
        self.model_id = model_id
        self.configuration_url = configuration_url
        self.entry_type = entry_type
        self.primary_config_entry = primary_config_entry

    @property
    def dict_repr(self):
        # Mirrors core ``DeviceEntry.dict_repr`` (one ``config/device_registry/
        # list`` element): the key set + order the device_get/device_list commands
        # return VERBATIM. Sets are rendered to lists here as core does; timestamps
        # are fixed floats (their exact value is irrelevant to the consumers).
        return {
            "area_id": self.area_id,
            "configuration_url": self.configuration_url,
            "config_entries": list(self.config_entries),
            "config_entries_subentries": {},
            "connections": list(self.connections),
            "created_at": 0.0,
            "disabled_by": self.disabled_by,
            "entry_type": self.entry_type,
            "hw_version": self.hw_version,
            "id": self.id,
            "identifiers": list(self.identifiers),
            "labels": list(self.labels),
            "manufacturer": self.manufacturer,
            "model": self.model,
            "model_id": self.model_id,
            "modified_at": 0.0,
            "name_by_user": self.name_by_user,
            "name": self.name,
            "primary_config_entry": self.primary_config_entry,
            "serial_number": self.serial_number,
            "sw_version": self.sw_version,
            "via_device_id": self.via_device_id,
        }


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


class FakeCollectionEntity:
    """Stand-in for a CollectionEntity (input_boolean/schedule/counter/…).

    Real collection helpers keep their full storage config on the entity as
    ``_config`` (a schedule's weekday blocks, an input_datetime's has_date, …),
    reachable via the domain's EntityComponent in ``hass.data['entity_components']``.
    """

    def __init__(self, entity_id, config, unique_id=None):
        self.entity_id = entity_id
        self._config = dict(config)
        self.unique_id = unique_id if unique_id is not None else config.get("id")


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
            "overview",
            "helpers_list",
            "states",
            "blueprint_get",
            "device_get",
            "device_list",
            "entity_enrich",
            "exposure",
        ]
        # config_get was withdrawn before release (raw_config freshness lags the
        # config file between write and reload) — it must not be advertised.
        assert "config_get" not in info["capabilities"]
        assert info["limits"] == {"max_results": 500, "max_body_bytes": 1_000_000}

    def test_manifest_version_parity(self):
        """The manifest version and COMPONENT_VERSION must not drift."""
        manifest = json.loads(
            (
                _REPO_ROOT / "custom_components" / "ha_mcp_tools" / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        assert manifest["version"] == COMPONENT_VERSION == "1.1.1"


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

    def test_computed_name_alias_sentinel_is_dropped(self, monkeypatch):
        """HA core's aliases can carry the COMPUTED_NAME sentinel
        (entity_registry.ComputedNameType._singleton — "the computed entity
        name is an alias"). Blind str() published it as a literal
        'ComputedNameType._singleton' alias on every carrying entity and made
        it a scored match text. Only real string aliases may surface."""
        from enum import Enum

        class ComputedNameType(Enum):  # mirrors homeassistant.helpers.entity_registry
            _singleton = 0

        states = [FakeState("light.lamp", "on", "Desk Lamp")]
        entry = FakeRegEntry(
            "light.lamp",
            aliases={"reading light", ComputedNameType._singleton},
        )
        view = make_view(entity={"light.lamp": entry})
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
        h = FakeHass(states=states)

        res = wsapi._do_search(h, {"query": "reading light"})
        assert res["entities"][0]["aliases"] == ["reading light"]

        # The sentinel must not be a match text either.
        assert not wsapi._do_search(h, {"query": "singleton"})["entities"]

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

    def test_scene_matches_by_name_config_never_emitted(self, empty_view):
        scene = FakeSceneEntity(
            "scene.movie",
            "Movie Night",
            unique_id="scn-1",
            scene_config={
                "id": "scn-1",
                "name": "Movie Night",
                "icon": "mdi:movie",
                "states": {"light.tv": object(), "media_player.lr": object()},
            },
        )
        h = FakeHass(data={"scene": FakeComponent([scene])})
        res = wsapi._do_search(h, {"query": "movie", "include_config": True})
        assert res["scenes"]
        rec = res["scenes"][0]
        assert rec["name"] == "Movie Night"
        assert rec["source"] == "storage"
        assert rec["match_in_name"] is True
        # Scenes never emit a component-served body, even under include_config.
        assert rec["config"] is None

    def test_scene_matches_by_entity_reference(self, empty_view):
        # The entity-id KEYS of scene_config.states are the match corpus, so a
        # query for an entity a scene touches finds it ("which scenes touch X").
        scene = FakeSceneEntity(
            "scene.evening",
            "Evening",
            unique_id="scn-2",
            scene_config={
                "id": "scn-2",
                "name": "Evening",
                "states": {"light.porch": object()},
            },
        )
        h = FakeHass(data={"scene": FakeComponent([scene])})
        res = wsapi._do_search(h, {"query": "light.porch", "include_config": True})
        assert res["scenes"], "a scene must match on an entity-id key of its states"
        assert res["scenes"][0]["match_in_config"] is True
        assert res["scenes"][0]["config"] is None

    def test_scene_state_values_not_in_corpus_or_response(self, empty_view):
        # Runtime State-object VALUES must never reach scoring or the response —
        # only the entity-id keys and id/name/icon do (no stringified garbage).
        class _RuntimeState:
            def __repr__(self):
                return "<state light.tv=scenegarbagevalue>"

        scene = FakeSceneEntity(
            "scene.movie",
            "Movie Night",
            unique_id="scn-1",
            scene_config={
                "id": "scn-1",
                "name": "Movie Night",
                "states": {"light.tv": _RuntimeState()},
            },
        )
        h = FakeHass(data={"scene": FakeComponent([scene])})
        by_value = wsapi._do_search(
            h, {"query": "scenegarbagevalue", "include_config": True}
        )
        assert not by_value["scenes"], "State values must not be in the match corpus"
        by_name = wsapi._do_search(h, {"query": "movie", "include_config": True})
        assert by_name["scenes"]
        assert "scenegarbagevalue" not in json.dumps(by_name)

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

    def test_collection_helper_search_matches_storage_body(self, empty_view):
        # Search must match a schedule on a value that lives ONLY in the storage
        # ``_config`` body (a weekday block time), not in the state attributes.
        state = FakeState("schedule.work", "on", friendly_name="Work")
        sched = FakeCollectionEntity(
            "schedule.work",
            {
                "id": "work",
                "name": "Work",
                "monday": [{"from": "07:07:07", "to": "09:00:00"}],
            },
            unique_id="work",
        )
        h = FakeHass(
            states=[state],
            data={"entity_components": {"schedule": FakeComponent([sched])}},
        )
        res = wsapi._do_search(
            h,
            {
                "query": "07:07:07",
                "search_types": ["helper"],
                "include_config": True,
            },
        )
        coll = [x for x in res["helpers"] if x["kind"] == "collection"]
        assert coll, "a storage-body-only value must match the collection helper"
        assert coll[0]["match_in_config"] is True
        assert "07:07:07" in json.dumps(coll[0]["config"])

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

    The scrub set is loaded off the event loop by ``_search_prep`` and passed into
    the pure ``_do_search`` (see :class:`TestSearchPrep` / :class:`TestSecretLoader`
    for the loader); these tests inject it directly via ``secret_values``.
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

    def test_secret_value_scrubbed_but_normal_token_matches(self, empty_view):
        h = self._yaml_automation_hass(self._SECRET)
        scrub = frozenset({self._SECRET})

        by_secret = wsapi._do_search(
            h,
            {"query": self._SECRET, "search_types": ["automation"]},
            secret_values=scrub,
        )
        assert not by_secret["automations"], (
            "a query equal to a resolved secret must not match (probe oracle)"
        )

        by_token = wsapi._do_search(
            h,
            {"query": "normalbodytoken", "search_types": ["automation"]},
            secret_values=scrub,
        )
        assert any(a["match_in_config"] for a in by_token["automations"]), (
            "a non-secret body token must still match after scrubbing"
        )

    def test_secret_scrubbed_in_fuzzy_mode(self, empty_view):
        h = self._yaml_automation_hass(self._SECRET)
        res = wsapi._do_search(
            h,
            {"query": self._SECRET, "search_types": ["automation"], "exact": False},
            secret_values=frozenset({self._SECRET}),
        )
        assert not res["automations"], "fuzzy mode must also scrub the secret leaf"

    def test_flow_helper_option_secret_scrubbed(self, empty_view):
        entry = FakeConfigEntry(
            "template",
            title="Sun Sensor",
            options={"state": self._SECRET, "name": "Sun Sensor"},
            entry_id="e1",
        )
        h = FakeHass(config_entries=[entry])
        res = wsapi._do_search(
            h,
            {"query": self._SECRET, "search_types": ["helper"]},
            secret_values=frozenset({self._SECRET}),
        )
        assert not [x for x in res["helpers"] if x["kind"] == "flow"], (
            "a flow-helper option equal to a secret must not match"
        )

    def test_empty_scrub_set_lets_the_value_match(self, empty_view):
        # No scrub set (the default — entity-only search, or an absent secrets.yaml
        # that degraded to empty) means the value is not blocked. Proves the scrub
        # is what blocks, and its absence is safe.
        h = self._yaml_automation_hass(self._SECRET)
        res = wsapi._do_search(
            h, {"query": self._SECRET, "search_types": ["automation"]}
        )
        assert res["automations"], "an empty scrub set must not block the match"


# =============================================================================
# secret loader — off-loop read of secrets.yaml; absent silent, broken warns
# =============================================================================
class TestSecretLoader:
    """``_load_secret_values`` reads ``secrets.yaml`` (run in the executor by
    ``_search_prep``) and degrades safely: an ABSENT file is silent (the common
    case), a present-but-unreadable/malformed file logs ONE warning; both yield
    an empty set. Only string values are collected."""

    _SECRET = "s3cr3tprobevaluexyz"
    _LOGGER_NAME = "custom_components.ha_mcp_tools.websocket_api"

    def _hass(self, tmp_path):
        h = FakeHass()
        h.config = FakeConfig(tmp_path)
        return h

    def test_valid_secrets_collected(self, tmp_path):
        (tmp_path / "secrets.yaml").write_text(
            f"api_password: {self._SECRET}\nother: value2\n", encoding="utf-8"
        )
        assert wsapi._load_secret_values(self._hass(tmp_path)) == frozenset(
            {self._SECRET, "value2"}
        )

    def test_missing_file_is_silent(self, tmp_path, caplog):
        # No secrets.yaml written → FileNotFoundError → empty set, no warning.
        with caplog.at_level(logging.WARNING, logger=self._LOGGER_NAME):
            result = wsapi._load_secret_values(self._hass(tmp_path))
        assert result == frozenset()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == [], (
            "an absent secrets.yaml must not warn"
        )

    def test_malformed_file_warns_once(self, tmp_path, caplog):
        (tmp_path / "secrets.yaml").write_text("{not: valid: yaml: [", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger=self._LOGGER_NAME):
            result = wsapi._load_secret_values(self._hass(tmp_path))
        assert result == frozenset()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "a malformed secrets.yaml must warn exactly once"

    def test_non_string_values_ignored(self, tmp_path):
        # A numeric scalar is not a plaintext-leak leaf; only strings are collected
        # and the load must not choke on the non-string.
        (tmp_path / "secrets.yaml").write_text(
            f"port: 8123\napi_password: {self._SECRET}\n", encoding="utf-8"
        )
        assert wsapi._load_secret_values(self._hass(tmp_path)) == frozenset(
            {self._SECRET}
        )

    def test_no_usable_config_path_degrades(self):
        # A hass without a callable config.path degrades to an empty set, no raise.
        assert wsapi._load_secret_values(FakeHass()) == frozenset()


# =============================================================================
# search prep — off-loop secret load, skipped for entity-only searches
# =============================================================================
class TestSearchPrep:
    """The ``search`` command's async pre-step loads the scrub set via the
    executor ONLY when a config/helper surface is requested; an entity-only
    search skips the file read entirely (perf gate)."""

    def _run(self, hass, msg):
        return asyncio.run(wsapi._search_prep(hass, msg))

    def test_entity_only_skips_executor(self, monkeypatch, tmp_path):
        calls = {"n": 0}

        async def _spy(func, *args):
            calls["n"] += 1
            return func(*args)

        h = FakeHass(states=[FakeState("light.k", "on", "Kitchen")])
        h.config = FakeConfig(tmp_path)
        monkeypatch.setattr(h, "async_add_executor_job", _spy)
        extra = self._run(h, {"search_types": ["entity"]})
        assert extra == {"secret_values": frozenset()}
        assert calls["n"] == 0, "entity-only search must not read secrets.yaml"

    def test_config_surface_loads_via_executor(self, monkeypatch, tmp_path):
        (tmp_path / "secrets.yaml").write_text(
            "api_password: sekret\n", encoding="utf-8"
        )
        calls = {"n": 0}

        async def _spy(func, *args):
            calls["n"] += 1
            return func(*args)

        h = FakeHass()
        h.config = FakeConfig(tmp_path)
        monkeypatch.setattr(h, "async_add_executor_job", _spy)
        extra = self._run(h, {"search_types": ["automation"]})
        assert calls["n"] == 1, "a config surface must offload the secrets read"
        assert extra["secret_values"] == frozenset({"sekret"})

    def test_default_search_types_load_via_executor(self, tmp_path):
        # No search_types → defaults to ALL (includes config surfaces) → loads.
        (tmp_path / "secrets.yaml").write_text(
            "api_password: sekret\n", encoding="utf-8"
        )
        h = FakeHass()
        h.config = FakeConfig(tmp_path)
        extra = self._run(h, {})
        assert extra["secret_values"] == frozenset({"sekret"})


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
            # The handler is a coroutine (it awaits the search prep's executor
            # offload); drive it to completion the way the WS layer would.
            asyncio.run(func(hass, connection, msg))

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
    "ha_mcp_tools/overview",
    "ha_mcp_tools/helpers_list",
    "ha_mcp_tools/states",
    "ha_mcp_tools/blueprint_get",
    "ha_mcp_tools/device_get",
    "ha_mcp_tools/device_list",
    "ha_mcp_tools/entity_enrich",
    "ha_mcp_tools/exposure",
]

# Minimal well-formed message body per command (Required fields) so the admin
# gate / async_response wrappers reach the pure handler. ``states`` requires
# ``entity_ids``; ``blueprint_get`` requires ``domain`` + ``path``; ``device_get``
# requires ``device_id``.
_CMD_MSG_EXTRA: dict[str, dict[str, object]] = {
    "ha_mcp_tools/states": {"entity_ids": []},
    "ha_mcp_tools/blueprint_get": {"domain": "automation", "path": "x.yaml"},
    "ha_mcp_tools/device_get": {"device_id": "d1"},
    "ha_mcp_tools/entity_enrich": {"entity_ids": []},
    # exposure: no entity_id (list mode) — the registries resolve empty here so
    # no per-entity settings lookup runs, keeping the admin-gate probe pure.
}


class TestRegistrationAndAdminGate:
    def test_all_commands_registered(self, functional_ws):
        assert set(functional_ws.registered) == {
            wsapi.WS_INFO,
            wsapi.WS_SEARCH,
            wsapi.WS_OVERVIEW,
            wsapi.WS_HELPERS_LIST,
            wsapi.WS_STATES,
            wsapi.WS_BLUEPRINT_GET,
            wsapi.WS_DEVICE_GET,
            wsapi.WS_DEVICE_LIST,
            wsapi.WS_ENTITY_ENRICH,
            wsapi.WS_EXPOSURE,
        }
        # config_get is withdrawn: no handler is registered for it.
        assert "ha_mcp_tools/config_get" not in functional_ws.registered

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
    """Voluptuous validation for overview / helpers_list."""

    def _schema(self, monkeypatch, schema_fn):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        return _REAL_VOL.Schema(schema_fn())

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

    def test_states_requires_entity_ids_list(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._states_schema)
        out = schema({"type": wsapi.WS_STATES, "entity_ids": ["light.a", "light.b"]})
        assert out["entity_ids"] == ["light.a", "light.b"]

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "ha_mcp_tools/states"},  # entity_ids required
            {"type": "ha_mcp_tools/states", "entity_ids": "light.a"},  # not a list
            {"type": "ha_mcp_tools/states", "entity_ids": [1, 2]},  # not strings
        ],
    )
    def test_states_malformed_rejected(self, monkeypatch, bad):
        schema = self._schema(monkeypatch, wsapi._states_schema)
        with pytest.raises(_REAL_VOL.Invalid):
            schema(bad)

    def test_blueprint_get_valid(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._blueprint_get_schema)
        out = schema(
            {
                "type": wsapi.WS_BLUEPRINT_GET,
                "domain": "script",
                "path": "user/x.yaml",
            }
        )
        assert out["domain"] == "script"
        assert out["path"] == "user/x.yaml"

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "ha_mcp_tools/blueprint_get", "path": "x.yaml"},  # domain required
            {"type": "ha_mcp_tools/blueprint_get", "domain": "automation"},  # path req
            # domain must be one of the blueprint domains
            {"type": "ha_mcp_tools/blueprint_get", "domain": "scene", "path": "x.yaml"},
        ],
    )
    def test_blueprint_get_malformed_rejected(self, monkeypatch, bad):
        schema = self._schema(monkeypatch, wsapi._blueprint_get_schema)
        with pytest.raises(_REAL_VOL.Invalid):
            schema(bad)

    def test_device_get_requires_device_id(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._device_get_schema)
        out = schema({"type": wsapi.WS_DEVICE_GET, "device_id": "d1"})
        assert out["device_id"] == "d1"
        # include_entities defaults False (device-only reads stay minimal).
        assert out["include_entities"] is False

    def test_device_get_accepts_include_entities(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._device_get_schema)
        out = schema(
            {"type": wsapi.WS_DEVICE_GET, "device_id": "d1", "include_entities": True}
        )
        assert out["include_entities"] is True

    def test_device_get_include_entities_must_be_bool(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._device_get_schema)
        with pytest.raises(_REAL_VOL.Invalid):
            schema(
                {
                    "type": wsapi.WS_DEVICE_GET,
                    "device_id": "d1",
                    "include_entities": "yes",
                }
            )

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "ha_mcp_tools/device_get"},  # device_id required
            {"type": "ha_mcp_tools/device_get", "device_id": 5},  # not a string
        ],
    )
    def test_device_get_malformed_rejected(self, monkeypatch, bad):
        schema = self._schema(monkeypatch, wsapi._device_get_schema)
        with pytest.raises(_REAL_VOL.Invalid):
            schema(bad)

    def test_device_list_valid(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._device_list_schema)
        out = schema({"type": wsapi.WS_DEVICE_LIST})
        assert out["type"] == wsapi.WS_DEVICE_LIST

    def test_device_list_rejects_extra_keys(self, monkeypatch):
        schema = self._schema(monkeypatch, wsapi._device_list_schema)
        with pytest.raises(_REAL_VOL.Invalid):
            schema({"type": wsapi.WS_DEVICE_LIST, "area_id": "x"})


# =============================================================================
# states — bulk State.as_dict() read + missing list
# =============================================================================
class TestStates:
    def test_found_and_missing_split(self):
        hass = FakeHass(
            states=[
                FakeState("light.a", "on", friendly_name="A"),
                FakeState("sensor.b", "21", friendly_name="B"),
            ]
        )
        res = wsapi._do_states(
            hass, {"entity_ids": ["light.a", "sensor.b", "light.ghost"]}
        )
        assert set(res["states"]) == {"light.a", "sensor.b"}
        assert res["missing"] == ["light.ghost"]

    def test_body_is_state_as_dict_verbatim(self):
        """The per-id body is core ``State.as_dict()`` unmodified (REST parity)."""
        state = FakeState("light.a", "on", friendly_name="A", brightness=128)
        res = wsapi._do_states(FakeHass(states=[state]), {"entity_ids": ["light.a"]})
        assert res["states"]["light.a"] == state.as_dict()
        # Timestamps pass through untouched (no _plainify str() mangling).
        assert res["states"]["light.a"]["last_changed"] == "2026-07-16T00:00:00+00:00"

    def test_empty_request(self):
        res = wsapi._do_states(FakeHass(states=[FakeState("light.a")]), {})
        assert res == {"states": {}, "missing": []}

    def test_all_missing_when_no_state_machine(self):
        # A hass with no usable states.get degrades every id to missing, never raises.
        res = wsapi._do_states(FakeHass(states=[]), {"entity_ids": ["light.a"]})
        assert res == {"states": {}, "missing": ["light.a"]}

    def test_duplicate_ids_map_once(self):
        res = wsapi._do_states(
            FakeHass(states=[FakeState("light.a")]),
            {"entity_ids": ["light.a", "light.a"]},
        )
        assert list(res["states"]) == ["light.a"]
        assert res["missing"] == []

    def test_unserializable_state_goes_to_missing(self):
        """A live state whose ``as_dict()`` returns None (core drift) is routed to
        ``missing`` rather than emitting a null state indistinguishable from a real
        value (issue #1813 F5)."""

        class _NullState:
            entity_id = "sensor.x"

            def as_dict(self):
                return None

        res = wsapi._do_states(
            FakeHass(states=[_NullState()]), {"entity_ids": ["sensor.x"]}
        )
        assert res == {"states": {}, "missing": ["sensor.x"]}


# =============================================================================
# blueprint_get — jailed file read, !input preserved, !secret neutralized
# =============================================================================
class TestBlueprintGet:
    _MOTION_LIGHT = (
        "blueprint:\n"
        "  name: Motion Light\n"
        "  description: Turn on a light on motion.\n"
        "  domain: automation\n"
        "  input:\n"
        "    motion_sensor:\n"
        "      name: Motion Sensor\n"
        "      selector:\n"
        "        entity:\n"
        "          domain: binary_sensor\n"
        "trigger:\n"
        "  - platform: state\n"
        "    entity_id: !input motion_sensor\n"
        "    to: 'on'\n"
        "action:\n"
        "  - service: light.turn_on\n"
        "    entity_id: !input target_light\n"
    )

    def _write_blueprint(self, tmp_path, domain, rel_path, text):
        target = tmp_path / "blueprints" / domain / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def _hass(self, tmp_path):
        return FakeHass(config=FakeConfig(base_dir=tmp_path))

    def test_reads_full_body_metadata_and_config(self, tmp_path):
        self._write_blueprint(
            tmp_path, "automation", "user/motion.yaml", self._MOTION_LIGHT
        )
        body = wsapi._read_blueprint_file(
            self._hass(tmp_path), "automation", "user/motion.yaml"
        )
        res = wsapi._do_blueprint_get(
            self._hass(tmp_path), {"domain": "automation"}, body=body
        )
        assert res["metadata"]["name"] == "Motion Light"
        assert res["config"]["blueprint"]["domain"] == "automation"
        # The body (triggers/actions) core's blueprint/list never returns.
        assert res["config"]["trigger"][0]["platform"] == "state"
        assert res["config"]["action"][0]["service"] == "light.turn_on"

    def test_input_tag_preserved_as_marker(self, tmp_path):
        self._write_blueprint(
            tmp_path, "automation", "user/motion.yaml", self._MOTION_LIGHT
        )
        body = wsapi._read_blueprint_file(
            self._hass(tmp_path), "automation", "user/motion.yaml"
        )
        assert body["trigger"][0]["entity_id"] == {"__input__": "motion_sensor"}
        assert body["action"][0]["entity_id"] == {"__input__": "target_light"}

    def test_secret_tag_neutralized_never_resolved(self, tmp_path):
        text = (
            "blueprint:\n"
            "  name: Has Secret\n"
            "  domain: automation\n"
            "action:\n"
            "  - service: notify.notify\n"
            "    data:\n"
            "      token: !secret my_api_token\n"
        )
        self._write_blueprint(tmp_path, "automation", "sneaky.yaml", text)
        body = wsapi._read_blueprint_file(
            self._hass(tmp_path), "automation", "sneaky.yaml"
        )
        # The !secret leaf is None — never a resolved plaintext value.
        assert body["action"][0]["data"] == {"token": None}
        assert "my_api_token" not in json.dumps(body)

    @pytest.mark.parametrize(
        "evil",
        [
            "../../secrets.yaml",
            "../../../etc/passwd",
            "/etc/passwd",
            "user/../../escape.yaml",
        ],
    )
    def test_path_traversal_rejected(self, tmp_path, evil):
        # Even if the target exists outside the jail, it must never be read.
        (tmp_path / "secrets.yaml").write_text("db_pw: hunter2\n", encoding="utf-8")
        body = wsapi._read_blueprint_file(self._hass(tmp_path), "automation", evil)
        assert body is None

    def test_missing_file_returns_none(self, tmp_path):
        assert (
            wsapi._read_blueprint_file(self._hass(tmp_path), "automation", "nope.yaml")
            is None
        )

    def test_do_blueprint_get_without_body(self):
        res = wsapi._do_blueprint_get(FakeHass(), {"domain": "automation"}, body=None)
        assert res == {"metadata": None, "config": None}

    @pytest.mark.asyncio
    async def test_prep_offloads_read_and_feeds_do(self, tmp_path):
        self._write_blueprint(
            tmp_path,
            "script",
            "user/s.yaml",
            "blueprint:\n  name: S\n  domain: script\nsequence:\n  - delay: 1\n",
        )
        hass = self._hass(tmp_path)
        extra = await wsapi._blueprint_get_prep(
            hass, {"domain": "script", "path": "user/s.yaml"}
        )
        res = wsapi._do_blueprint_get(hass, {"domain": "script"}, **extra)
        assert res["metadata"]["name"] == "S"
        assert res["config"]["sequence"] == [{"delay": 1}]


# =============================================================================
# config_get — withdrawn before release (raw_config freshness lag)
# =============================================================================
class TestConfigGetWithdrawn:
    """``config_get`` was withdrawn before release: it served an entity's
    ``raw_config``, whose freshness lags the config file between a write and the
    next completed reload, so a get racing a reload returned a stale body. The
    command, its schema, its capability, and its domain gate are all gone — the
    get tools serve automation/script reads from the legacy REST path (which
    reads the fresh config file). These pin that nothing component-side still
    exposes it (issue #1813 tracks a possible file-reading redesign)."""

    def test_capability_not_advertised(self):
        assert "config_get" not in wsapi.CAPABILITIES

    def test_no_command_constant_schema_or_domain_gate(self):
        assert not hasattr(wsapi, "WS_CONFIG_GET")
        assert not hasattr(wsapi, "_config_get_schema")
        assert not hasattr(wsapi, "CONFIG_GET_DOMAINS")

    def test_no_handler_function(self):
        assert not hasattr(wsapi, "_do_config_get")


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

    def test_body_from_storage_config_surfaces_schedule_blocks(self, empty_view):
        # Regression for the live e2e schedule-update failure: a schedule's weekday
        # blocks live in the storage ``_config`` (reached via the EntityComponent),
        # NOT the live state attributes, so listing must surface them + the real id.
        state = FakeState(
            "schedule.work",
            "on",
            friendly_name="Work",
            next_event="2026-07-13T09:00:00+00:00",
        )
        sched = FakeCollectionEntity(
            "schedule.work",
            {
                "id": "work",
                "name": "Work",
                "monday": [
                    {"from": "07:00:00", "to": "09:00:00"},
                    {"from": "17:00:00", "to": "19:00:00"},
                ],
            },
            unique_id="work",
        )
        h = FakeHass(
            states=[state],
            data={"entity_components": {"schedule": FakeComponent([sched])}},
        )
        res = wsapi._do_helpers_list(h, {})
        rec = next(r for r in res["helpers"] if r["helper_type"] == "schedule")
        assert rec["storage_id"] == "work"
        # The weekday blocks are absent from the state attributes but present here.
        assert "monday" not in state.attributes
        assert len(rec["config"]["monday"]) == 2

    def test_body_falls_back_to_attrs_without_storage_entity(self, empty_view):
        # A collection helper with no reachable entity (_config absent, e.g. a
        # YAML-defined input_boolean or input_number's _attr_* layout) falls back
        # to the state-attributes body.
        states = [FakeState("input_boolean.legacy", "off", friendly_name="Legacy Flag")]
        res = wsapi._do_helpers_list(
            FakeHass(states=states), {}
        )  # no entity_components
        rec = next(r for r in res["helpers"] if r["helper_type"] == "input_boolean")
        assert rec["config"]["friendly_name"] == "Legacy Flag"
        assert rec["storage_id"] == "legacy"

    def test_rename_current_name_wins_over_storage_body_name(self, monkeypatch):
        # With a storage body present, the record ``name`` is still the CURRENT
        # display name, not the (possibly stale) name inside the storage config.
        state = FakeState(
            "input_boolean.guest_mode", "off", friendly_name="Current Guest"
        )
        ent = FakeCollectionEntity(
            "input_boolean.guest_mode",
            {"id": "guest_mode", "name": "Old Name"},
            unique_id="guest_mode",
        )
        view = make_view(
            entity={
                "input_boolean.guest_mode": FakeRegEntry(
                    "input_boolean.guest_mode", name="Current Guest"
                )
            }
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
        h = FakeHass(
            states=[state],
            data={"entity_components": {"input_boolean": FakeComponent([ent])}},
        )
        rec = next(
            r
            for r in wsapi._do_helpers_list(h, {})["helpers"]
            if r["kind"] == "collection"
        )
        assert rec["name"] == "Current Guest"
        assert rec["storage_id"] == "guest_mode"

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

    def test_repairs_exclude_inactive_issue_registry_entries(self, monkeypatch):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: self._view())
        registry = FakeIssueRegistry(
            [
                FakeIssue("active_issue", "mqtt"),
                FakeIssue("inactive_issue", "zwave", active=False),
            ]
        )
        monkeypatch.setattr(wsapi, "ir", FakeIssueRegModule(registry))

        res = wsapi._do_overview(self._hass(), {})

        assert [repair["issue_id"] for repair in res["repairs"]] == ["active_issue"]

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
        # Missing/None registries are "nothing here", not a failure: no slice_errors.
        assert res["slice_errors"] == []

    def test_slice_errors_empty_when_clean(self, monkeypatch):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: self._view())
        monkeypatch.setattr(wsapi, "ir", FakeIssueRegModule(FakeIssueRegistry([])))
        res = wsapi._do_overview(self._hass(), {})
        assert res["slice_errors"] == []

    def test_slice_errors_records_degraded_slice(self, monkeypatch):
        # A registry accessor that RAISES (vs a missing/None registry) is named in
        # slice_errors so the server can tell "empty" from "failed"; the slice
        # still degrades to a usable empty default and other slices are unaffected.
        class _RaisingEntityReg:
            @property
            def entities(self):
                raise RuntimeError("registry read blew up")

            def async_get(self, entity_id):
                return None

        view = wsapi._RegistryView(entity=_RaisingEntityReg())
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
        monkeypatch.setattr(wsapi, "ir", FakeIssueRegModule(FakeIssueRegistry([])))
        res = wsapi._do_overview(self._hass(), {})
        assert "entity_registry" in res["slice_errors"]
        assert res["entity_registry"] == []
        assert res["states"], "an unrelated slice must still be populated"


# =============================================================================
# device_get + device_list — raw DeviceEntry.dict_repr reads
# =============================================================================
def _zha_device():
    """A DeviceEntry-ish fake carrying the fields the server transforms read."""
    return FakeDevice(
        "dev-1",
        name="Kitchen Sensor",
        name_by_user="Kitchen",
        area_id="a1",
        labels=("important",),
        manufacturer="Aqara",
        model="T1",
        identifiers=(("zha", "00:11:22:33:44:55:66:77"),),
        connections=(("zigbee", "00:11:22:33:44:55:66:77"),),
        config_entries=("cfg-1",),
        disabled_by=None,
        sw_version="1.2.3",
        via_device_id="coordinator-1",
    )


class TestDeviceGet:
    def test_found_returns_dict_repr_verbatim(self, monkeypatch):
        dev = _zha_device()
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: make_view(devices=[dev])
        )
        res = wsapi._do_device_get(FakeHass(), {"device_id": "dev-1"})
        # The body is DeviceEntry.dict_repr UNMODIFIED — the byte-parity contract
        # with one config/device_registry/list element.
        assert res["device"] == dev.dict_repr
        assert res["device"]["id"] == "dev-1"
        assert res["device"]["name_by_user"] == "Kitchen"
        assert res["device"]["config_entries"] == ["cfg-1"]
        assert res["device"]["identifiers"] == [("zha", "00:11:22:33:44:55:66:77")]

    def test_missing_device_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: make_view(devices=[_zha_device()])
        )
        res = wsapi._do_device_get(FakeHass(), {"device_id": "ghost"})
        assert res == {"device": None}

    def test_absent_device_id_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: make_view(devices=[_zha_device()])
        )
        res = wsapi._do_device_get(FakeHass(), {})
        assert res == {"device": None}

    def test_unserializable_dict_repr_degrades_to_none(self, monkeypatch):
        class _BadDevice:
            id = "dev-x"

            @property
            def dict_repr(self):
                raise RuntimeError("core drift")

        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: make_view(devices=[_BadDevice()])
        )
        res = wsapi._do_device_get(FakeHass(), {"device_id": "dev-x"})
        assert res == {"device": None}


class TestDeviceList:
    def test_lists_all_dict_reprs(self, monkeypatch):
        d1 = FakeDevice("d1", name="One")
        d2 = FakeDevice("d2", name="Two", area_id="a2")
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: make_view(devices=[d1, d2])
        )
        res = wsapi._do_device_list(FakeHass(), {})
        by_id = {d["id"]: d for d in res["devices"]}
        assert set(by_id) == {"d1", "d2"}
        assert by_id["d1"] == d1.dict_repr
        assert by_id["d2"]["area_id"] == "a2"

    def test_empty_registry(self, monkeypatch):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: make_view())
        assert wsapi._do_device_list(FakeHass(), {}) == {"devices": []}

    def test_skips_unserializable_entry(self, monkeypatch, caplog):
        class _BadDevice:
            id = "bad"

            @property
            def dict_repr(self):
                raise RuntimeError("core drift")

        good = FakeDevice("good", name="Good")
        monkeypatch.setattr(
            wsapi,
            "_resolve_registries",
            lambda h: make_view(devices=[_BadDevice(), good]),
        )
        with caplog.at_level(logging.WARNING):
            res = wsapi._do_device_list(FakeHass(), {})
        assert [d["id"] for d in res["devices"]] == ["good"]
        # The skip is logged (with the offending id), not silent (issue #1813 F5).
        assert any(
            "skipping device" in r.getMessage() and "bad" in r.getMessage()
            for r in caplog.records
        )


# =============================================================================
# device_get include_entities — the per-device entity join
# =============================================================================
class TestDeviceGetEntities:
    def _view_with_entities(self):
        return make_view(
            devices=[FakeDevice("dev-1", name="D1")],
            entity={
                "sensor.a": FakeRegEntry(
                    "sensor.a", device_id="dev-1", platform="zha", name="A"
                ),
                "update.a": FakeRegEntry("update.a", device_id="dev-1", platform="zha"),
                "sensor.disabled": FakeRegEntry(
                    "sensor.disabled", device_id="dev-1", disabled_by="user"
                ),
                "sensor.other": FakeRegEntry("sensor.other", device_id="dev-2"),
            },
        )

    def test_include_entities_joins_device_rows(self, monkeypatch):
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: self._view_with_entities()
        )
        monkeypatch.setattr(wsapi, "er", FakeErModule())
        res = wsapi._do_device_get(
            FakeHass(), {"device_id": "dev-1", "include_entities": True}
        )
        assert res["device"]["id"] == "dev-1"
        ids = {e["entity_id"] for e in res["entities"]}
        # dev-1's entities (incl. the disabled one); dev-2's is excluded.
        assert ids == {"sensor.a", "update.a", "sensor.disabled"}
        # Rows are the raw as_partial_dict shape (config/entity_registry/list parity).
        row = next(e for e in res["entities"] if e["entity_id"] == "sensor.a")
        assert row["device_id"] == "dev-1"
        assert row["platform"] == "zha"
        assert row["name"] == "A"

    def test_disabled_entities_included(self, monkeypatch):
        # include_disabled_entities=True is what the component passes — matching
        # config/entity_registry/list, which lists disabled entities too.
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: self._view_with_entities()
        )
        monkeypatch.setattr(wsapi, "er", FakeErModule())
        res = wsapi._do_device_get(
            FakeHass(), {"device_id": "dev-1", "include_entities": True}
        )
        assert any(
            e["entity_id"] == "sensor.disabled" and e["disabled_by"] == "user"
            for e in res["entities"]
        )

    def test_entities_omitted_when_not_requested(self, monkeypatch):
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: self._view_with_entities()
        )
        monkeypatch.setattr(wsapi, "er", FakeErModule())
        res = wsapi._do_device_get(FakeHass(), {"device_id": "dev-1"})
        assert "entities" not in res

    def test_entities_empty_for_unknown_device(self, monkeypatch):
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: self._view_with_entities()
        )
        monkeypatch.setattr(wsapi, "er", FakeErModule())
        res = wsapi._do_device_get(
            FakeHass(), {"device_id": "ghost", "include_entities": True}
        )
        assert res["device"] is None
        assert res["entities"] == []


# A stand-in for core's ``HomeAssistantError`` whose type NAME matches, so the
# ``exposure`` guardrail (``_is_unknown_entity_error`` keys off the name, not an
# isinstance against the MagicMock-stubbed ``homeassistant.exceptions``) fires.
class _UnknownEntityError(Exception):
    pass


_UnknownEntityError.__name__ = "HomeAssistantError"


class TestIsUnknownEntityError:
    """``_is_unknown_entity_error`` matches ONLY core's 'Unknown entity' raise: the
    type name alone is too wide, so a same-type store-read failure is not swallowed
    (issue #1813 F4)."""

    def test_matches_unknown_entity_message(self):
        assert wsapi._is_unknown_entity_error(_UnknownEntityError("Unknown entity"))
        # Case-insensitive, substring anywhere in the message.
        assert wsapi._is_unknown_entity_error(
            _UnknownEntityError("unknown entity light.x")
        )

    def test_rejects_same_type_other_message(self):
        # Right type name, unrelated fault → NOT a match (must propagate).
        assert not wsapi._is_unknown_entity_error(
            _UnknownEntityError("settings store read failed")
        )

    def test_rejects_other_type_same_message(self):
        assert not wsapi._is_unknown_entity_error(ValueError("Unknown entity"))


# =============================================================================
# entity_enrich
# =============================================================================
class TestEntityEnrich:
    """``ha_mcp_tools/entity_enrich`` — the shared registry join for a set of ids."""

    def _view(self):
        return make_view(
            entity={
                "light.lamp": FakeRegEntry(
                    "light.lamp",
                    aliases={"reading light"},
                    area_id="a1",
                    labels={"lb1"},
                ),
                # No own area/labels — must inherit both from device d1.
                "switch.plug": FakeRegEntry("switch.plug", device_id="d1"),
            },
            areas=[
                FakeArea("a1", "Office", floor_id="f1"),
                FakeArea("a9", "Garage", floor_id="f2"),
            ],
            floors=[FakeFloor("f1", "Upstairs"), FakeFloor("f2", "Downstairs")],
            labels=[FakeLabel("lb1", "Favorites"), FakeLabel("lb2", "Auto")],
            devices=[FakeDevice("d1", area_id="a9", labels={"lb2"})],
        )

    def test_resolves_names_for_each_id(self, monkeypatch):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: self._view())
        res = wsapi._do_entity_enrich(FakeHass(), {"entity_ids": ["light.lamp"]})
        rec = res["entities"]["light.lamp"]
        assert rec == {
            "area": "Office",
            "floor": "Upstairs",
            "labels": ["Favorites"],
            "aliases": ["reading light"],
        }

    def test_device_inherited_area_and_labels(self, monkeypatch):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: self._view())
        res = wsapi._do_entity_enrich(FakeHass(), {"entity_ids": ["switch.plug"]})
        rec = res["entities"]["switch.plug"]
        assert rec["area"] == "Garage"  # inherited from device d1
        assert rec["floor"] == "Downstairs"
        assert rec["labels"] == ["Auto"]  # inherited from device d1
        assert rec["aliases"] == []

    def test_unknown_id_kept_with_empty_fields(self, monkeypatch):
        """A registry-less id is not dropped — the caller keys the result back."""
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: self._view())
        res = wsapi._do_entity_enrich(FakeHass(), {"entity_ids": ["light.ghost"]})
        assert res["entities"]["light.ghost"] == {
            "area": None,
            "floor": None,
            "labels": [],
            "aliases": [],
        }

    def test_empty_id_list(self, monkeypatch):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: self._view())
        assert wsapi._do_entity_enrich(FakeHass(), {"entity_ids": []}) == {
            "entities": {}
        }

    def test_reuses_the_search_join(self, monkeypatch):
        """entity_enrich and the search record derive from the same join, so their
        area/floor/labels/aliases agree for the same entity (no drift)."""
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: self._view())
        enrich = wsapi._do_entity_enrich(FakeHass(), {"entity_ids": ["light.lamp"]})[
            "entities"
        ]["light.lamp"]
        record = wsapi._entity_record(
            FakeState("light.lamp", "on", "Lamp"), self._view()
        )
        for key in ("area", "floor", "labels", "aliases"):
            assert enrich[key] == record[key]


# =============================================================================
# exposure
# =============================================================================
class TestExposure:
    """``ha_mcp_tools/exposure`` — list + single mode with the enrichment join."""

    def _view(self):
        return make_view(
            entity={
                "light.kitchen": FakeRegEntry("light.kitchen", area_id="a1"),
                "light.attic": FakeRegEntry("light.attic", area_id="a1"),
            },
            areas=[FakeArea("a1", "Kitchen", floor_id="f1")],
            floors=[FakeFloor("f1", "Main")],
        )

    def _patch(self, monkeypatch, settings_map, legacy_ids=()):
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: self._view())

        def fake_settings(hass, entity_id):
            if entity_id not in settings_map:
                raise _UnknownEntityError("Unknown entity")
            return settings_map[entity_id]

        monkeypatch.setattr(wsapi, "_async_get_entity_settings", fake_settings)
        monkeypatch.setattr(
            wsapi, "_legacy_exposed_entity_ids", lambda h: list(legacy_ids)
        )

    def test_single_should_expose_filter(self, monkeypatch):
        """Guardrail 1: only should_expose-true assistants are reported (the raw
        helper returns every assistant that has any stored option)."""
        self._patch(
            monkeypatch,
            {
                "light.kitchen": {
                    "conversation": {"should_expose": True},
                    "cloud.alexa": {"should_expose": False},
                    "cloud.google_assistant": {"some_other_option": "x"},
                }
            },
        )
        states = [FakeState("light.kitchen", "on", "Kitchen Light")]
        res = wsapi._do_exposure(
            FakeHass(states=states), {"entity_id": "light.kitchen"}
        )
        assert res["exposed_entities"] == {"light.kitchen": {"conversation": True}}
        info = res["entity_info"]["light.kitchen"]
        assert info["friendly_name"] == "Kitchen Light"
        assert info["domain"] == "light"
        assert info["area"] == "Kitchen"
        assert info["floor"] == "Main"
        assert info["state"] == "on"

    def test_unknown_entity_degrades_to_not_exposed(self, monkeypatch):
        """Guardrail 2: HomeAssistantError('Unknown entity') → not-exposed default,
        never a raise (the legacy path never raises on a junk id)."""
        self._patch(monkeypatch, {})  # every id is "unknown"
        states = [FakeState("light.ghost", "on", "Ghost")]
        res = wsapi._do_exposure(FakeHass(states=states), {"entity_id": "light.ghost"})
        assert res["exposed_entities"] == {}
        # Enrichment is still provided for the requested id.
        assert res["entity_info"]["light.ghost"]["domain"] == "light"

    def test_missing_state_omits_live_fields(self, monkeypatch):
        """Guardrail 3: no hass.states.get → friendly_name/state omitted, not a crash."""
        self._patch(
            monkeypatch, {"light.attic": {"conversation": {"should_expose": True}}}
        )
        res = wsapi._do_exposure(FakeHass(states=[]), {"entity_id": "light.attic"})
        info = res["entity_info"]["light.attic"]
        assert "friendly_name" not in info
        assert "state" not in info
        assert info["domain"] == "light"
        assert info["area"] == "Kitchen"

    def test_non_unknown_ha_error_propagates(self, monkeypatch):
        """A same-typed HomeAssistantError whose message is NOT 'unknown entity'
        (e.g. a settings-store read failure) propagates instead of being silently
        reported as not-exposed (issue #1813 F4)."""
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: self._view())

        def boom(hass, entity_id):
            raise _UnknownEntityError("settings store read failed")

        monkeypatch.setattr(wsapi, "_async_get_entity_settings", boom)
        with pytest.raises(_UnknownEntityError):
            wsapi._do_exposure(FakeHass(states=[]), {"entity_id": "light.kitchen"})

    def test_list_mode_mirrors_ws_list(self, monkeypatch):
        """List mode walks the registry, keeps only exposed ids, enriches each."""
        self._patch(
            monkeypatch,
            {
                "light.kitchen": {"conversation": {"should_expose": True}},
                "light.attic": {"cloud.alexa": {"should_expose": False}},
            },
        )
        states = [FakeState("light.kitchen", "on", "Kitchen Light")]
        res = wsapi._do_exposure(FakeHass(states=states), {})
        assert res["exposed_entities"] == {"light.kitchen": {"conversation": True}}
        assert set(res["entity_info"]) == {"light.kitchen"}

    def test_list_mode_includes_legacy_store_ids(self, monkeypatch):
        """An exposed entity present only in the legacy store (no registry entry)
        is still enumerated — the union of store ids and registry ids."""
        self._patch(
            monkeypatch,
            {"scene.movie": {"conversation": {"should_expose": True}}},
            legacy_ids=["scene.movie"],
        )
        res = wsapi._do_exposure(FakeHass(states=[]), {})
        assert res["exposed_entities"] == {"scene.movie": {"conversation": True}}
