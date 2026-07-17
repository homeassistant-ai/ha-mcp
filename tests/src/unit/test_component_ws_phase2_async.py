"""Unit tests for the Phase 2 async-prep component WS commands (Task 3).

Kept in a SEPARATE file from ``test_component_ws_search.py`` (which Task 2 also
edits in parallel) so the two task branches merge cleanly; the Fake* fixtures and
``make_view`` are imported from that module. Covers the five Task 3 additions:

* ``dashboards`` — list / get / search modes over ``hass.data[LOVELACE_DATA]``,
  loaded off the loop in ``_dashboards_prep``: legacy-parity list rows + additive
  ``mode``; the default dashboard (url_path=None) resolved in ``get``; a YAML-mode
  dashboard never emits a body (``yaml_excluded``); search-match truncation.
* ``services_list`` — REST ``/api/services`` reshape with the coarse SUPERSET
  filter and domain-scoped translation filtering.
* ``reference_data`` — the service index + entity-id universe the reference
  validator's ``build_service_index`` / ``build_entity_set`` consume.
* ``search`` ``visibility`` param — the pure ``_visibility_hidden_set`` mirroring
  the server's ``hidden_entity_ids`` (each dimension in isolation, allow-mode, the
  injectable Assist dimension) + its hard-exclude placement in ``_do_search``.
* ``server_entry`` — the component's own server config entry, reading ONLY the
  entry-type marker key from ``entry.data``.

The ``info`` capability-list / manifest-version / registration-set drift guards in
``test_component_ws_search.py`` are intentionally NOT duplicated here (the
controller reconciles them at the Task 2 + Task 3 merge).
"""

from __future__ import annotations

import asyncio

import pytest

# The service index the server's reference validator builds off reference_data
# (importable without the HA stubs — pure, like ha_mcp.tools.tools_search).
from ha_mcp.tools.reference_validator import build_entity_set, build_service_index

# Import EVERYTHING component-side (including ``wsapi`` itself) THROUGH the sibling
# module — importing it installs the ``homeassistant.*`` MagicMock stubs and forces
# the REAL voluptuous into sys.modules FIRST, so a direct ``custom_components``
# import (which loads homeassistant) is never hoisted above the stub install.
# Mirrors test_component_search_contract.py's ``wsapi`` re-import.
from . import test_component_ws_search as _base
from .test_component_ws_search import (
    FakeConfigEntry,
    FakeDevice,
    FakeHass,
    FakeRegEntry,
    FakeServices,
    FakeState,
    _FakeConnection,
    _FakeWSApi,
    _Unauthorized,
    make_view,
    wsapi,
)

_REAL_VOL = _base._REAL_VOL


# =============================================================================
# Lovelace fakes (LovelaceConfig stand-ins + the hass.data container)
# =============================================================================
class FakeDashboard:
    """Stand-in for a core ``LovelaceConfig`` (LovelaceStorage / LovelaceYAML).

    ``config`` is the stored collection item + url_path (list-mode metadata);
    ``mode`` is ``storage``/``yaml``; ``async_load(force)`` returns the body (or
    raises ``load_error`` to exercise the fail-soft paths).
    """

    def __init__(
        self, url_path, mode="storage", config=None, body=None, load_error=None
    ):
        self._url_path = url_path
        self.mode = mode
        self.config = config
        self._body = body
        self._load_error = load_error

    @property
    def url_path(self):
        return self._url_path

    async def async_load(self, force):
        if self._load_error is not None:
            raise self._load_error
        return self._body


class FakeLovelaceData:
    """Stand-in for core's ``LovelaceData`` dataclass (only ``.dashboards`` read)."""

    def __init__(self, dashboards):
        self.dashboards = dict(dashboards)


def _storage_dash(url_path, title, *, body=None, icon="mdi:home", **extra):
    config = {
        "id": f"id-{url_path}",
        "url_path": url_path,
        "title": title,
        "icon": icon,
        "show_in_sidebar": True,
        "require_admin": False,
    }
    config.update(extra)
    return FakeDashboard(url_path, "storage", config=config, body=body)


@pytest.fixture
def patch_dashboards(monkeypatch):
    """Return a setter that patches ``_lovelace_dashboards_map`` to a fixed map."""

    def _set(dashboards_map):
        monkeypatch.setattr(
            wsapi, "_lovelace_dashboards_map", lambda hass: dashboards_map
        )

    return _set


def _run_dashboards(hass, msg):
    extra = asyncio.run(wsapi._dashboards_prep(hass, msg))
    return wsapi._do_dashboards(hass, msg, **extra)


# =============================================================================
# capability presence (a light check, NOT the reconciled drift guard)
# =============================================================================
class TestCapabilityPresence:
    def test_new_capabilities_advertised(self):
        for cap in (
            "dashboards",
            "services_list",
            "reference_data",
            "search_visibility",
            "server_entry",
        ):
            assert cap in wsapi.CAPABILITIES

    def test_component_version_bumped(self):
        assert wsapi.COMPONENT_VERSION == "1.2.0"


# =============================================================================
# dashboards — list
# =============================================================================
class TestDashboardsList:
    def test_list_rows_mirror_legacy_shape_plus_mode(self, patch_dashboards):
        dmap = {
            "home": _storage_dash("home", "Home"),
            "energy-dash": _storage_dash("energy-dash", "Energy", icon="mdi:flash"),
        }
        patch_dashboards(dmap)
        res = _run_dashboards(FakeHass(), {"mode": "list"})
        assert res["available"] is True
        rows = {r["url_path"]: r for r in res["dashboards"]}
        assert set(rows) == {"home", "energy-dash"}
        home = rows["home"]
        assert home == {
            "id": "id-home",
            "url_path": "home",
            "title": "Home",
            "icon": "mdi:home",
            "show_in_sidebar": True,
            "require_admin": False,
            "mode": "storage",
        }

    def test_list_tags_yaml_mode_so_server_can_exclude(self, patch_dashboards):
        dmap = {
            "home": _storage_dash("home", "Home"),
            "yaml-dash": FakeDashboard(
                "yaml-dash",
                "yaml",
                config={"url_path": "yaml-dash", "title": "YAML", "icon": None},
            ),
        }
        patch_dashboards(dmap)
        res = _run_dashboards(FakeHass(), {"mode": "list"})
        modes = {r["url_path"]: r["mode"] for r in res["dashboards"]}
        assert modes == {"home": "storage", "yaml-dash": "yaml"}

    def test_list_omits_default_dashboard(self, patch_dashboards):
        # The default dashboard has the None url_path key; the legacy list omits
        # it and the server special-cases the built-in as always-existing.
        dmap = {
            None: _storage_dash("lovelace", "Default"),
            "home": _storage_dash("home", "Home"),
        }
        patch_dashboards(dmap)
        res = _run_dashboards(FakeHass(), {"mode": "list"})
        assert [r["url_path"] for r in res["dashboards"]] == ["home"]

    def test_unavailable_when_lovelace_absent(self, patch_dashboards):
        patch_dashboards(None)
        res = _run_dashboards(FakeHass(), {"mode": "list"})
        assert res == {"mode": "list", "available": False, "dashboards": []}


# =============================================================================
# dashboards — get
# =============================================================================
class TestDashboardsGet:
    def test_get_storage_body(self, patch_dashboards):
        body = {"title": "Home", "views": [{"title": "Main", "cards": []}]}
        patch_dashboards({"home": _storage_dash("home", "Home", body=body)})
        res = _run_dashboards(FakeHass(), {"mode": "get", "url_path": "home"})
        assert res["status"] == "ok"
        assert res["url_path"] == "home"
        assert res["config"] == body

    def test_get_default_dashboard_via_none_url_path(self, patch_dashboards):
        body = {"views": [{"title": "Overview"}]}
        patch_dashboards({None: _storage_dash("lovelace", "Default", body=body)})
        # url_path absent -> the default dashboard (None key).
        res = _run_dashboards(FakeHass(), {"mode": "get"})
        assert res["status"] == "ok"
        assert res["config"] == body

    def test_get_yaml_dashboard_never_emits_body(self, patch_dashboards):
        # A YAML dashboard's async_load would return a body carrying possibly
        # resolved !secret plaintext — it must never be emitted.
        yaml_dash = FakeDashboard(
            "yaml-dash",
            "yaml",
            config={"url_path": "yaml-dash"},
            body={"views": [{"cards": [{"type": "markdown", "content": "secret"}]}]},
        )
        patch_dashboards({"yaml-dash": yaml_dash})
        res = _run_dashboards(FakeHass(), {"mode": "get", "url_path": "yaml-dash"})
        assert res["status"] == "yaml_excluded"
        assert res["config"] is None

    def test_get_missing_dashboard(self, patch_dashboards):
        patch_dashboards({"home": _storage_dash("home", "Home", body={})})
        res = _run_dashboards(FakeHass(), {"mode": "get", "url_path": "nope"})
        assert res["status"] == "not_found"
        assert res["config"] is None

    def test_get_load_error_degrades_to_not_found(self, patch_dashboards):
        broken = FakeDashboard(
            "home", "storage", config={"url_path": "home"}, load_error=RuntimeError("x")
        )
        patch_dashboards({"home": broken})
        res = _run_dashboards(FakeHass(), {"mode": "get", "url_path": "home"})
        assert res["status"] == "not_found"
        assert res["config"] is None

    def test_get_unavailable(self, patch_dashboards):
        patch_dashboards(None)
        res = _run_dashboards(FakeHass(), {"mode": "get", "url_path": "home"})
        assert res == {"mode": "get", "available": False}


# =============================================================================
# dashboards — search
# =============================================================================
class TestDashboardsSearch:
    def _dmap(self):
        body = {
            "title": "Home",
            "views": [
                {
                    "title": "Living",
                    "cards": [
                        {
                            "type": "entities",
                            "entities": ["light.kitchen", "light.hall"],
                        },
                        {
                            "type": "vertical-stack",
                            "cards": [
                                {"type": "camera", "camera_image": "camera.front_door"}
                            ],
                        },
                    ],
                    "sections": [
                        {"cards": [{"type": "markdown", "content": "kitchen notes"}]}
                    ],
                }
            ],
        }
        return {"home": _storage_dash("home", "Home", body=body)}

    def test_search_matches_entity_and_reports_path(self, patch_dashboards):
        patch_dashboards(self._dmap())
        res = _run_dashboards(FakeHass(), {"mode": "search", "query": "light.kitchen"})
        assert res["truncated"] is False
        matched = [m for m in res["matches"] if m["matched_value"] == "light.kitchen"]
        assert matched
        m = matched[0]
        assert m["url_path"] == "home"
        assert m["view_index"] == 0
        assert m["view_title"] == "Living"
        assert m["card_type"] == "entities"
        assert m["matched_field"] == "entities"
        assert m["card_path"] == "views[0].cards[0]"

    def test_search_matches_nested_card_and_section(self, patch_dashboards):
        patch_dashboards(self._dmap())
        # Nested camera card (inside vertical-stack) is attributed to itself.
        cam = wsapi._search_dashboard_docs(
            asyncio.run(wsapi._dashboard_search_docs(self._dmap())), "camera.front_door"
        )[0]
        assert cam
        cam_match = next(m for m in cam if m["matched_value"] == "camera.front_door")
        assert cam_match["card_type"] == "camera"
        assert cam_match["card_path"] == "views[0].cards[1].cards[0]"
        # Section card (plain string content).
        res = _run_dashboards(FakeHass(), {"mode": "search", "query": "kitchen notes"})
        sec = next(m for m in res["matches"] if m["matched_field"] == "content")
        assert sec["card_path"] == "views[0].sections[0].cards[0]"

    def test_search_empty_query_matches_nothing(self, patch_dashboards):
        patch_dashboards(self._dmap())
        res = _run_dashboards(FakeHass(), {"mode": "search", "query": ""})
        assert res["matches"] == []
        assert res["truncated"] is False

    def test_search_skips_yaml_dashboards(self, patch_dashboards):
        yaml_dash = FakeDashboard(
            "yaml-dash",
            "yaml",
            config={"url_path": "yaml-dash"},
            body={
                "views": [{"cards": [{"type": "entities", "entities": ["light.x"]}]}]
            },
        )
        patch_dashboards({"yaml-dash": yaml_dash})
        res = _run_dashboards(FakeHass(), {"mode": "search", "query": "light.x"})
        assert res["matches"] == []

    def test_search_truncates_at_cap(self, patch_dashboards):
        entities = [f"light.e{i}" for i in range(wsapi._DASHBOARD_MATCH_CAP + 25)]
        body = {"views": [{"cards": [{"type": "entities", "entities": entities}]}]}
        patch_dashboards({"home": _storage_dash("home", "Home", body=body)})
        res = _run_dashboards(FakeHass(), {"mode": "search", "query": "light.e"})
        assert res["truncated"] is True
        assert len(res["matches"]) == wsapi._DASHBOARD_MATCH_CAP


# =============================================================================
# _lovelace_dashboards_map resolution (fallback key + unavailable)
# =============================================================================
class TestDashboardsMapResolution:
    def test_resolves_via_lovelace_data_container(self):
        data = {"lovelace": FakeLovelaceData({"home": _storage_dash("home", "Home")})}
        dmap = wsapi._lovelace_dashboards_map(FakeHass(data=data))
        assert dmap is not None
        assert set(dmap) == {"home"}

    def test_none_when_absent(self):
        assert wsapi._lovelace_dashboards_map(FakeHass()) is None


# =============================================================================
# dashboards — schema
# =============================================================================
class TestDashboardsSchema:
    def _schema(self, monkeypatch):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        return _REAL_VOL.Schema(wsapi._dashboards_schema())

    def test_defaults_to_list_mode(self, monkeypatch):
        out = self._schema(monkeypatch)({"type": wsapi.WS_DASHBOARDS})
        assert out["mode"] == "list"

    def test_accepts_none_url_path(self, monkeypatch):
        out = self._schema(monkeypatch)(
            {"type": wsapi.WS_DASHBOARDS, "mode": "get", "url_path": None}
        )
        assert out["url_path"] is None

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "ha_mcp_tools/dashboards", "mode": "bogus"},
            {"type": "ha_mcp_tools/dashboards", "url_path": 5},
        ],
    )
    def test_malformed_rejected(self, monkeypatch, bad):
        with pytest.raises(_REAL_VOL.Invalid):
            self._schema(monkeypatch)(bad)


# =============================================================================
# services_list — reshape + coarse superset filter
# =============================================================================
_DESCRIPTIONS = {
    "light": {
        "turn_on": {"name": "Turn on", "description": "Turn a light on", "fields": {}},
        "turn_off": {"name": "Turn off", "description": "Turn a light off"},
    },
    "climate": {
        "set_temperature": {
            "name": "Set temperature",
            "description": "Set the target temperature",
        }
    },
}
_TRANSLATIONS = {
    "component.light.services.turn_on.name": "Turn on",
    "component.light.services.turn_on.description": "Switch a light on",
    "component.climate.services.set_temperature.name": "Set temperature",
}


class TestServicesList:
    def _do(self, params):
        return wsapi._do_services_list(
            FakeHass(),
            params,
            descriptions=_DESCRIPTIONS,
            translations=_TRANSLATIONS,
        )

    def test_rest_shape_and_no_filter(self):
        res = self._do({})
        by_domain = {e["domain"]: e for e in res["services"]}
        assert set(by_domain) == {"light", "climate"}
        # REST /api/services element shape: {domain, services: {name: descdict}}.
        assert set(by_domain["light"]["services"]) == {"turn_on", "turn_off"}
        assert by_domain["light"]["services"]["turn_on"]["name"] == "Turn on"

    def test_domain_filter_exact(self):
        res = self._do({"domain": "climate"})
        assert [e["domain"] for e in res["services"]] == ["climate"]

    def test_query_matches_domain(self):
        res = self._do({"query": "climate"})
        assert {e["domain"] for e in res["services"]} == {"climate"}

    def test_query_matches_service_name(self):
        res = self._do({"query": "turn_on"})
        assert {e["domain"] for e in res["services"]} == {"light"}

    def test_query_matches_description_string(self):
        res = self._do({"query": "target temperature"})
        assert {e["domain"] for e in res["services"]} == {"climate"}

    def test_query_matches_translation_string(self):
        # "Switch a light on" only appears in the translations, not the description.
        res = self._do({"query": "switch a light"})
        assert {e["domain"] for e in res["services"]} == {"light"}

    def test_superset_keeps_a_server_matching_service(self):
        # A query the server's own filter would keep (a service-name substring) is
        # never dropped by the coarse filter (superset property).
        res = self._do({"query": "turn"})
        assert "light" in {e["domain"] for e in res["services"]}

    def test_translations_filtered_to_kept_domains(self):
        res = self._do({"query": "turn_on"})  # keeps light only
        assert all(key.split(".")[1] == "light" for key in res["translations"])
        assert (
            "component.climate.services.set_temperature.name" not in res["translations"]
        )

    def test_prep_feeds_do(self, monkeypatch):
        async def _fake_desc(hass):
            return _DESCRIPTIONS

        async def _fake_trans(hass, language):
            assert language == "en"
            return _TRANSLATIONS

        monkeypatch.setattr(wsapi, "_fetch_service_descriptions", _fake_desc)
        monkeypatch.setattr(wsapi, "_fetch_service_translations", _fake_trans)
        extra = asyncio.run(
            wsapi._services_list_prep(FakeHass(), {"type": wsapi.WS_SERVICES_LIST})
        )
        res = wsapi._do_services_list(FakeHass(), {"query": "climate"}, **extra)
        assert {e["domain"] for e in res["services"]} == {"climate"}


class TestServicesListSchema:
    def _schema(self, monkeypatch):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        return _REAL_VOL.Schema(wsapi._services_list_schema())

    def test_language_default(self, monkeypatch):
        out = self._schema(monkeypatch)({"type": wsapi.WS_SERVICES_LIST})
        assert out["language"] == "en"

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "ha_mcp_tools/services_list", "domain": 5},
            {"type": "ha_mcp_tools/services_list", "language": 5},
        ],
    )
    def test_malformed_rejected(self, monkeypatch, bad):
        with pytest.raises(_REAL_VOL.Invalid):
            self._schema(monkeypatch)(bad)


# =============================================================================
# reference_data — service index + entity universe
# =============================================================================
class TestReferenceData:
    def _hass(self):
        services = FakeServices(
            {
                "light": {"turn_on": object(), "turn_off": object()},
                "climate": {"set_temperature": object()},
            }
        )
        states = [FakeState("light.a"), FakeState("sensor.b")]
        return FakeHass(states=states, services=services)

    def test_service_index_and_entity_set_parity(self):
        res = wsapi._do_reference_data(self._hass(), {})
        # The REST-list services feed build_service_index unchanged.
        assert build_service_index(res["services"]) == {
            "light": {"turn_on", "turn_off"},
            "climate": {"set_temperature"},
        }
        # entity_ids feed build_entity_set. build_entity_set expects the /api/states
        # dict shape, but reference_data returns a flat id list; assert the ids.
        assert set(res["entity_ids"]) == {"light.a", "sensor.b"}
        assert build_entity_set([{"entity_id": eid} for eid in res["entity_ids"]]) == {
            "light.a",
            "sensor.b",
        }

    def test_service_bodies_are_empty_dicts(self):
        res = wsapi._do_reference_data(self._hass(), {})
        for entry in res["services"]:
            assert all(body == {} for body in entry["services"].values())

    def test_include_states_false(self):
        res = wsapi._do_reference_data(self._hass(), {"include_states": False})
        assert res["entity_ids"] == []
        assert res["services"]  # services still present

    def test_no_services(self):
        res = wsapi._do_reference_data(FakeHass(), {})
        assert res["services"] == []


class TestReferenceDataSchema:
    def _schema(self, monkeypatch):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        return _REAL_VOL.Schema(wsapi._reference_data_schema())

    def test_include_states_default(self, monkeypatch):
        out = self._schema(monkeypatch)({"type": wsapi.WS_REFERENCE_DATA})
        assert out["include_states"] is True

    def test_malformed_rejected(self, monkeypatch):
        with pytest.raises(_REAL_VOL.Invalid):
            self._schema(monkeypatch)(
                {"type": wsapi.WS_REFERENCE_DATA, "include_states": "yes"}
            )


# =============================================================================
# search_visibility — _visibility_hidden_set dimensions
# =============================================================================
def _always_expose(_eid):
    return True


class TestVisibilityHiddenSet:
    def test_empty_config_hides_nothing(self):
        view = make_view(entity={"light.a": FakeRegEntry("light.a")})
        hidden = wsapi._visibility_hidden_set(
            view, [FakeState("light.a")], {}, _always_expose
        )
        assert hidden == set()

    def test_deny_list_hides_states_only_entity(self):
        # deny needs no registry entry — a states-only entity is still hidden.
        view = make_view()
        hidden = wsapi._visibility_hidden_set(
            view,
            [FakeState("light.a"), FakeState("light.b")],
            {"deny_entity_ids": ["light.a"]},
            _always_expose,
        )
        assert hidden == {"light.a"}

    def test_exclude_category(self):
        view = make_view(
            entity={
                "sensor.diag": FakeRegEntry(
                    "sensor.diag", entity_category="diagnostic"
                ),
                "light.a": FakeRegEntry("light.a"),
            }
        )
        hidden = wsapi._visibility_hidden_set(
            view,
            [FakeState("sensor.diag"), FakeState("light.a")],
            {"exclude_categories": ["diagnostic", "config"]},
            _always_expose,
        )
        assert hidden == {"sensor.diag"}

    def test_unknown_category_hides_nothing(self):
        view = make_view(
            entity={"sensor.x": FakeRegEntry("sensor.x", entity_category="bogus")}
        )
        hidden = wsapi._visibility_hidden_set(
            view,
            [FakeState("sensor.x")],
            {"exclude_categories": ["bogus"]},
            _always_expose,
        )
        assert hidden == set()

    def test_exclude_hidden(self):
        view = make_view(
            entity={
                "light.h": FakeRegEntry("light.h", hidden_by="user"),
                "light.a": FakeRegEntry("light.a"),
            }
        )
        hidden = wsapi._visibility_hidden_set(
            view,
            [FakeState("light.h"), FakeState("light.a")],
            {"exclude_hidden": True},
            _always_expose,
        )
        assert hidden == {"light.h"}

    def test_exclude_area_direct_and_device_inherited(self):
        view = make_view(
            entity={
                "light.direct": FakeRegEntry("light.direct", area_id="garage"),
                "light.viadev": FakeRegEntry("light.viadev", device_id="d1"),
                "light.keep": FakeRegEntry("light.keep", area_id="office"),
            },
            devices=[FakeDevice("d1", area_id="garage")],
        )
        states = [
            FakeState("light.direct"),
            FakeState("light.viadev"),
            FakeState("light.keep"),
        ]
        hidden = wsapi._visibility_hidden_set(
            view, states, {"exclude_areas": ["garage"]}, _always_expose
        )
        assert hidden == {"light.direct", "light.viadev"}

    def test_exclude_label_direct_and_device_inherited(self):
        view = make_view(
            entity={
                "light.direct": FakeRegEntry("light.direct", labels=["hide"]),
                "light.viadev": FakeRegEntry("light.viadev", device_id="d1"),
                "light.keep": FakeRegEntry("light.keep", labels=["show"]),
            },
            devices=[FakeDevice("d1", labels={"hide"})],
        )
        states = [
            FakeState("light.direct"),
            FakeState("light.viadev"),
            FakeState("light.keep"),
        ]
        hidden = wsapi._visibility_hidden_set(
            view, states, {"exclude_labels": ["hide"]}, _always_expose
        )
        assert hidden == {"light.direct", "light.viadev"}

    def test_allow_entity_ids_restrict_mode(self):
        view = make_view(
            entity={
                "light.keep": FakeRegEntry("light.keep"),
                "light.drop": FakeRegEntry("light.drop"),
            }
        )
        states = [
            FakeState("light.keep"),
            FakeState("light.drop"),
            FakeState("light.statesonly"),
        ]
        hidden = wsapi._visibility_hidden_set(
            view, states, {"allow_entity_ids": ["light.keep"]}, _always_expose
        )
        # Everything not allow-listed is hidden, including states-only entities.
        assert hidden == {"light.drop", "light.statesonly"}

    def test_allow_area_restrict_mode(self):
        view = make_view(
            entity={
                "light.keep": FakeRegEntry("light.keep", area_id="office"),
                "light.drop": FakeRegEntry("light.drop", area_id="garage"),
            }
        )
        states = [FakeState("light.keep"), FakeState("light.drop")]
        hidden = wsapi._visibility_hidden_set(
            view, states, {"allow_areas": ["office"]}, _always_expose
        )
        assert hidden == {"light.drop"}

    def test_allow_empty_registry_guard_drops_area_dim(self):
        # An allowlist area dimension with an empty registry but states-only
        # candidates would blank everything; the guard drops it (allow_entity_ids
        # still applies).
        view = make_view()
        states = [FakeState("light.a"), FakeState("light.b")]
        hidden = wsapi._visibility_hidden_set(
            view, states, {"allow_areas": ["office"]}, _always_expose
        )
        assert hidden == set()

    def test_allow_labels_restrict_mode(self):
        # allow_labels restrict mode: kept only when the entity carries an
        # allowed label directly, OR inherits it from its device; an entity
        # with an unrelated label is hidden like anything else unmatched.
        view = make_view(
            entity={
                "light.keep": FakeRegEntry("light.keep", labels=["important"]),
                "light.viadev": FakeRegEntry("light.viadev", device_id="d1"),
                "light.drop": FakeRegEntry("light.drop", labels=["other"]),
            },
            devices=[FakeDevice("d1", labels={"important"})],
        )
        states = [
            FakeState("light.keep"),
            FakeState("light.viadev"),
            FakeState("light.drop"),
        ]
        hidden = wsapi._visibility_hidden_set(
            view, states, {"allow_labels": ["important"]}, _always_expose
        )
        assert hidden == {"light.drop"}

    def test_deny_wins_over_allow(self):
        # An entity matching an active allow dimension is still hidden if it
        # is also in deny_entity_ids: deny seeds ``hidden`` up front and the
        # allow/assist loop skips anything already hidden, so allow can never
        # rescue a denied entity (mirrors resolver.hidden_entity_ids).
        view = make_view(
            entity={
                "light.both": FakeRegEntry("light.both", area_id="office"),
                "light.keep": FakeRegEntry("light.keep", area_id="office"),
            }
        )
        states = [FakeState("light.both"), FakeState("light.keep")]
        hidden = wsapi._visibility_hidden_set(
            view,
            states,
            {"deny_entity_ids": ["light.both"], "allow_areas": ["office"]},
            _always_expose,
        )
        assert hidden == {"light.both"}

    def test_allow_device_inherited_area_and_label(self):
        # allow_areas/allow_labels restrict mode must also resolve device
        # inheritance: an entity with no direct area_id/labels is kept when
        # its device's area or label matches, and dropped when neither does.
        view = make_view(
            entity={
                "light.viadev_area": FakeRegEntry("light.viadev_area", device_id="d1"),
                "light.viadev_label": FakeRegEntry(
                    "light.viadev_label", device_id="d2"
                ),
                "light.drop": FakeRegEntry("light.drop"),
            },
            devices=[
                FakeDevice("d1", area_id="office"),
                FakeDevice("d2", labels={"important"}),
            ],
        )
        states = [
            FakeState("light.viadev_area"),
            FakeState("light.viadev_label"),
            FakeState("light.drop"),
        ]
        hidden = wsapi._visibility_hidden_set(
            view,
            states,
            {"allow_areas": ["office"], "allow_labels": ["important"]},
            _always_expose,
        )
        assert hidden == {"light.drop"}

    def test_assist_dimension_via_injected_fake(self):
        view = make_view(
            entity={
                "light.a": FakeRegEntry("light.a"),
                "light.b": FakeRegEntry("light.b"),
            }
        )
        states = [FakeState("light.a"), FakeState("light.b")]

        def _expose(eid):
            return eid != "light.a"  # light.a not exposed -> hidden

        hidden = wsapi._visibility_hidden_set(
            view, states, {"respect_assist_exposure": True}, _expose
        )
        assert hidden == {"light.a"}

    def test_assist_not_consulted_when_disabled(self):
        view = make_view(entity={"light.a": FakeRegEntry("light.a")})
        calls = []

        def _spy(eid):
            calls.append(eid)
            return False

        hidden = wsapi._visibility_hidden_set(
            view, [FakeState("light.a")], {"deny_entity_ids": ["x"]}, _spy
        )
        assert calls == []  # respect_assist_exposure not set
        assert hidden == {"x"}

    def test_assist_unavailable_skips_dimension(self):
        # respect_assist_exposure set, but assist_available=False: the Assist check
        # must be skipped (nothing hidden by Assist), mirroring the resolver's
        # "skip the dimension when its data is unavailable" fail-open. should_expose_fn
        # (which would otherwise hide light.a) is never consulted.
        view = make_view(
            entity={
                "light.a": FakeRegEntry("light.a"),
                "light.b": FakeRegEntry("light.b"),
            }
        )
        calls = []

        def _spy(eid):
            calls.append(eid)
            return eid != "light.a"

        hidden = wsapi._visibility_hidden_set(
            view,
            [FakeState("light.a"), FakeState("light.b")],
            {"respect_assist_exposure": True},
            _spy,
            assist_available=False,
        )
        assert hidden == set()
        assert calls == []


class TestVisibilityWarnings:
    """Per-dimension degradation warnings, mirroring the server resolver."""

    def test_empty_config_no_warnings(self):
        view = make_view(entity={"light.a": FakeRegEntry("light.a")})
        assert wsapi._visibility_warnings(view, [FakeState("light.a")], {}) == []

    def test_unknown_category_warns(self):
        view = make_view(entity={"sensor.x": FakeRegEntry("sensor.x")})
        warnings = wsapi._visibility_warnings(
            view,
            [FakeState("sensor.x")],
            {"exclude_categories": ["bogus", "diagnostic"]},
        )
        assert warnings == [wsapi._unknown_categories_warning({"bogus"})]

    def test_known_categories_only_no_warning(self):
        view = make_view(entity={"sensor.x": FakeRegEntry("sensor.x")})
        warnings = wsapi._visibility_warnings(
            view, [FakeState("sensor.x")], {"exclude_categories": ["diagnostic"]}
        )
        assert warnings == []

    def test_empty_registry_allowlist_warns(self):
        # Area/label allowlist with an empty registry but states-only candidates
        # would blank everything; the guard fires and warns.
        view = make_view()
        warnings = wsapi._visibility_warnings(
            view, [FakeState("light.a")], {"allow_areas": ["office"]}
        )
        assert warnings == [wsapi._ALLOWLIST_REGISTRY_EMPTY_WARNING]

    def test_allowlist_with_registry_no_warning(self):
        view = make_view(entity={"light.a": FakeRegEntry("light.a", area_id="office")})
        warnings = wsapi._visibility_warnings(
            view, [FakeState("light.a")], {"allow_areas": ["office"]}
        )
        assert warnings == []

    def test_allow_entity_ids_only_no_empty_registry_warning(self):
        # An allow_entity_ids list needs no registry data, so the empty-registry
        # guard does not fire for it.
        view = make_view()
        warnings = wsapi._visibility_warnings(
            view, [FakeState("light.a")], {"allow_entity_ids": ["light.a"]}
        )
        assert warnings == []

    def test_assist_unavailable_warns(self):
        view = make_view(entity={"light.a": FakeRegEntry("light.a")})
        warnings = wsapi._visibility_warnings(
            view,
            [FakeState("light.a")],
            {"respect_assist_exposure": True},
            assist_available=False,
        )
        assert warnings == [wsapi._ASSIST_UNAVAILABLE_WARNING]

    def test_assist_available_no_warning(self):
        view = make_view(entity={"light.a": FakeRegEntry("light.a")})
        warnings = wsapi._visibility_warnings(
            view,
            [FakeState("light.a")],
            {"respect_assist_exposure": True},
            assist_available=True,
        )
        assert warnings == []

    def test_multiple_degradations_all_surface(self):
        view = make_view()
        warnings = wsapi._visibility_warnings(
            view,
            [FakeState("light.a")],
            {
                "exclude_categories": ["bogus"],
                "allow_areas": ["office"],
                "respect_assist_exposure": True,
            },
            assist_available=False,
        )
        assert set(warnings) == {
            wsapi._unknown_categories_warning({"bogus"}),
            wsapi._ALLOWLIST_REGISTRY_EMPTY_WARNING,
            wsapi._ASSIST_UNAVAILABLE_WARNING,
        }


# =============================================================================
# search_visibility — hard-exclude placement in _do_search
# =============================================================================
class TestSearchVisibilityPlacement:
    def _hass_and_view(self, monkeypatch):
        view = make_view(
            entity={
                "light.a": FakeRegEntry("light.a"),
                "light.b": FakeRegEntry("light.b"),
            }
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
        return FakeHass(
            states=[FakeState("light.a", "on", "A"), FakeState("light.b", "on", "B")]
        )

    def test_no_visibility_returns_all(self, monkeypatch):
        h = self._hass_and_view(monkeypatch)
        res = wsapi._do_search(h, {"query": ""})
        assert {e["entity_id"] for e in res["entities"]} == {"light.a", "light.b"}
        assert res["entity_total_matches"] == 2

    def test_visibility_excludes_before_counts(self, monkeypatch):
        h = self._hass_and_view(monkeypatch)
        res = wsapi._do_search(
            h, {"query": "", "visibility": {"deny_entity_ids": ["light.a"]}}
        )
        assert {e["entity_id"] for e in res["entities"]} == {"light.b"}
        # Excluded BEFORE the count (mirrors the legacy hard-exclude placement).
        assert res["entity_total_matches"] == 1

    def test_empty_visibility_dict_is_noop(self, monkeypatch):
        h = self._hass_and_view(monkeypatch)
        res = wsapi._do_search(h, {"query": "", "visibility": {}})
        assert res["entity_total_matches"] == 2

    def test_assist_seam_wired_in_do_search(self, monkeypatch):
        h = self._hass_and_view(monkeypatch)
        # Assist available (its store is set up) so the dimension runs and consults
        # the injected should_expose seam.
        monkeypatch.setattr(wsapi, "_assist_exposure_available", lambda hass: True)
        monkeypatch.setattr(
            wsapi, "_assist_should_expose", lambda hass, eid: eid != "light.a"
        )
        res = wsapi._do_search(
            h, {"query": "", "visibility": {"respect_assist_exposure": True}}
        )
        assert {e["entity_id"] for e in res["entities"]} == {"light.b"}

    def test_assist_unavailable_skips_dimension_and_warns(self, monkeypatch):
        # Assist requested but its store is unavailable: the dimension is skipped
        # (nothing hidden) and the response carries the degradation warning.
        h = self._hass_and_view(monkeypatch)
        monkeypatch.setattr(wsapi, "_assist_exposure_available", lambda hass: False)
        # Would hide light.a if consulted — but it must NOT be consulted.
        monkeypatch.setattr(
            wsapi, "_assist_should_expose", lambda hass, eid: eid != "light.a"
        )
        res = wsapi._do_search(
            h, {"query": "", "visibility": {"respect_assist_exposure": True}}
        )
        assert {e["entity_id"] for e in res["entities"]} == {"light.a", "light.b"}
        assert res["visibility_warnings"] == [wsapi._ASSIST_UNAVAILABLE_WARNING]


class TestSearchVisibilitySchema:
    def _schema(self, monkeypatch):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        return _REAL_VOL.Schema(wsapi._search_schema())

    def test_accepts_visibility_dict(self, monkeypatch):
        out = self._schema(monkeypatch)(
            {"type": wsapi.WS_SEARCH, "visibility": {"exclude_hidden": True}}
        )
        assert out["visibility"] == {"exclude_hidden": True}

    def test_rejects_non_dict_visibility(self, monkeypatch):
        with pytest.raises(_REAL_VOL.Invalid):
            self._schema(monkeypatch)({"type": wsapi.WS_SEARCH, "visibility": "nope"})


# =============================================================================
# server_entry
# =============================================================================
class TestServerEntry:
    def test_finds_server_entry(self):
        server = FakeConfigEntry(
            domain="ha_mcp_tools",
            data={"entry_type": "server", "webhook_id": "secret-xyz"},
            options={"channel": "dev", "pip_spec": "ha-mcp==1.0"},
            entry_id="srv1",
        )
        tools = FakeConfigEntry(
            domain="ha_mcp_tools", data={"entry_type": "tools"}, entry_id="tools1"
        )
        h = FakeHass(config_entries=[tools, server])
        res = wsapi._do_server_entry(h, {})
        assert res == {
            "entry_id": "srv1",
            "channel": "dev",
            "pip_spec": "ha-mcp==1.0",
        }

    def test_reads_only_marker_key_no_data_leak(self):
        server = FakeConfigEntry(
            domain="ha_mcp_tools",
            data={"entry_type": "server", "access_token": "TOKEN", "secret": "S"},
            options={},
            entry_id="srv1",
        )
        res = wsapi._do_server_entry(FakeHass(config_entries=[server]), {})
        # Only the three declared keys; no entry.data value leaks into the payload.
        assert set(res) == {"entry_id", "channel", "pip_spec"}
        assert "TOKEN" not in res.values()
        assert res["channel"] is None
        assert res["pip_spec"] is None

    def test_no_server_entry(self):
        tools = FakeConfigEntry(
            domain="ha_mcp_tools", data={"entry_type": "tools"}, entry_id="tools1"
        )
        res = wsapi._do_server_entry(FakeHass(config_entries=[tools]), {})
        assert res == {"entry_id": None, "channel": None, "pip_spec": None}

    def test_ignores_other_domain_server_entries(self):
        other = FakeConfigEntry(
            domain="some_other", data={"entry_type": "server"}, entry_id="x"
        )
        res = wsapi._do_server_entry(FakeHass(config_entries=[other]), {})
        assert res["entry_id"] is None


class TestServerEntrySchema:
    def test_schema_shape(self, monkeypatch):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        schema = _REAL_VOL.Schema(wsapi._server_entry_schema())
        out = schema({"type": wsapi.WS_SERVER_ENTRY})
        assert out["type"] == wsapi.WS_SERVER_ENTRY

    def test_rejects_extra_keys(self, monkeypatch):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        schema = _REAL_VOL.Schema(wsapi._server_entry_schema())
        with pytest.raises(_REAL_VOL.Invalid):
            schema({"type": wsapi.WS_SERVER_ENTRY, "entry_id": "x"})


# =============================================================================
# registration + admin gate for the four new commands
# =============================================================================
_NEW_COMMANDS = [
    "ha_mcp_tools/dashboards",
    "ha_mcp_tools/services_list",
    "ha_mcp_tools/reference_data",
    "ha_mcp_tools/server_entry",
]

_NEW_CMD_MSG_EXTRA = {
    "ha_mcp_tools/dashboards": {"mode": "list"},
}


@pytest.fixture
def functional_ws(monkeypatch):
    fake = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", fake)
    monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
    monkeypatch.setattr(
        wsapi, "_resolve_registries", lambda hass: wsapi._RegistryView()
    )
    monkeypatch.setattr(wsapi, "_lovelace_dashboards_map", lambda hass: None)

    async def _empty_desc(hass):
        return {}

    async def _empty_trans(hass, language):
        return {}

    monkeypatch.setattr(wsapi, "_fetch_service_descriptions", _empty_desc)
    monkeypatch.setattr(wsapi, "_fetch_service_translations", _empty_trans)
    wsapi.async_register_commands(FakeHass())
    return fake


class TestNewCommandRegistration:
    def test_new_commands_registered(self, functional_ws):
        for command in _NEW_COMMANDS:
            assert command in functional_ws.registered

    @pytest.mark.parametrize("command", _NEW_COMMANDS)
    def test_non_admin_rejected(self, functional_ws, command):
        handler = functional_ws.registered[command]
        conn = _FakeConnection(is_admin=False)
        with pytest.raises(_Unauthorized):
            handler(FakeHass(), conn, {"id": 1, "type": command})

    @pytest.mark.parametrize("command", _NEW_COMMANDS)
    def test_admin_call_sends_result(self, functional_ws, command):
        handler = functional_ws.registered[command]
        conn = _FakeConnection(is_admin=True)
        msg = {"id": 7, "type": command, **_NEW_CMD_MSG_EXTRA.get(command, {})}
        handler(FakeHass(), conn, msg)
        assert 7 in conn.results
        assert isinstance(conn.results[7], dict)
