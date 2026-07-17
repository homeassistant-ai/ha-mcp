"""Unit tests for the Phase 2 async-prep component WS commands (Task 3).

Kept in a SEPARATE file from ``test_component_ws_search.py`` (which Task 2 also
edits in parallel) so the two task branches merge cleanly; the Fake* fixtures and
``make_view`` are imported from that module. Covers the five Task 3 additions:

* ``dashboards`` — list / get / search modes over ``hass.data[LOVELACE_DATA]``,
  loaded off the loop in ``_dashboards_prep``: legacy-parity list rows + additive
  ``mode``; the default dashboard (url_path=None) resolved in ``get``; a YAML-mode
  dashboard never emits a body (``yaml_excluded``); search-match truncation.
* ``services_list`` — REST ``/api/services`` reshape with the ``domain`` exact
  filter and domain-scoped translation filtering (no ``query`` coarse-filter).
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
import sys
from types import SimpleNamespace

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
        assert wsapi.COMPONENT_VERSION == "1.3.0"


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

    def test_search_matches_view_badges(self, patch_dashboards):
        # View-level badges (bare-string AND dict form) are entity refs the card
        # walk never visits — MODE 4 must cover them (mirrors MODE 2).
        body = {
            "title": "Home",
            "views": [
                {
                    "title": "Living",
                    "badges": [
                        "sensor.badge_temp",  # bare-string badge
                        {"type": "entity", "entity": "sensor.badge_hum"},  # dict badge
                    ],
                    "cards": [],
                }
            ],
        }
        patch_dashboards({"home": _storage_dash("home", "Home", body=body)})
        res = _run_dashboards(
            FakeHass(), {"mode": "search", "query": "sensor.badge_temp"}
        )
        m = next(m for m in res["matches"] if m["matched_value"] == "sensor.badge_temp")
        assert m["card_type"] == "badge"
        assert m["matched_field"] == "badges"
        assert m["card_path"] == "views[0].badges[0]"
        assert m["view_title"] == "Living"
        # The dict badge's entity leaf is matched and attributed to badges[1].
        res2 = _run_dashboards(
            FakeHass(), {"mode": "search", "query": "sensor.badge_hum"}
        )
        m2 = next(
            m for m in res2["matches"] if m["matched_value"] == "sensor.badge_hum"
        )
        assert m2["card_path"] == "views[0].badges[1]"
        assert m2["matched_field"] == "entity"

    def test_search_matches_header_card(self, patch_dashboards):
        # A sections-view header card (views[n].header.card) is walked as a single
        # card — the card walk never visits it otherwise (mirrors MODE 2).
        body = {
            "title": "Home",
            "views": [
                {
                    "type": "sections",
                    "title": "Overview",
                    "header": {
                        "card": {"type": "markdown", "content": "welcome home banner"}
                    },
                    "sections": [],
                }
            ],
        }
        patch_dashboards({"home": _storage_dash("home", "Home", body=body)})
        res = _run_dashboards(FakeHass(), {"mode": "search", "query": "welcome home"})
        m = next(m for m in res["matches"] if m["matched_field"] == "content")
        assert m["card_type"] == "markdown"
        assert m["card_path"] == "views[0].header.card"
        assert m["view_index"] == 0

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
# services_list — reshape + domain filter (no query coarse-filter)
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

    def test_translations_filtered_to_kept_domains(self):
        res = self._do({"domain": "light"})  # keeps light only
        assert all(key.split(".")[1] == "light" for key in res["translations"])
        assert (
            "component.climate.services.set_temperature.name" not in res["translations"]
        )

    def test_query_not_accepted_by_do(self):
        # No coarse ``query`` filter: an (unschema'd) query param is inert — the full
        # domain-scoped catalog is returned unchanged for the server to filter.
        res = self._do({"query": "climate"})
        assert {e["domain"] for e in res["services"]} == {"light", "climate"}

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
        res = wsapi._do_services_list(FakeHass(), {"domain": "climate"}, **extra)
        assert {e["domain"] for e in res["services"]} == {"climate"}

    def test_service_descriptions_drift_raises(self, monkeypatch):
        # Core drift: async_get_all_descriptions returns a non-Mapping. This must
        # RAISE (→ command-error fallback to the legacy REST /api/services), NOT
        # serve a well-formed empty catalog the server would trust (review-4 E).
        async def _bad_desc(hass):
            return ["not", "a", "mapping"]

        monkeypatch.setitem(
            sys.modules,
            "homeassistant.helpers.service",
            SimpleNamespace(async_get_all_descriptions=_bad_desc),
        )
        monkeypatch.setitem(
            sys.modules, "homeassistant.exceptions", _base._exceptions_stub
        )
        with pytest.raises(_base._StubHomeAssistantError):
            asyncio.run(wsapi._fetch_service_descriptions(FakeHass()))


class TestServicesListSchema:
    def _schema(self, monkeypatch):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        return _REAL_VOL.Schema(wsapi._services_list_schema())

    def test_language_default(self, monkeypatch):
        out = self._schema(monkeypatch)({"type": wsapi.WS_SERVICES_LIST})
        assert out["language"] == "en"

    def test_query_param_rejected(self, monkeypatch):
        # ``query`` is no longer a wire param — a coarse component filter cannot be a
        # superset of the server's concatenation-based filter, so it is not accepted.
        with pytest.raises(_REAL_VOL.Invalid):
            self._schema(monkeypatch)(
                {"type": wsapi.WS_SERVICES_LIST, "query": "light"}
            )

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

    def test_no_services_substrate_raises(self, monkeypatch):
        # Core drift / broken hass: hass.services (the ServiceRegistry, always present
        # in a running HA) is absent, so async_services() yields no Mapping. This must
        # RAISE (→ server command-error fallback to the legacy REST get_services),
        # NOT serve an empty catalog that makes every reference emit a false
        # "not found" warning where legacy skips validation (review-3 M-1).
        monkeypatch.setitem(
            sys.modules, "homeassistant.exceptions", _base._exceptions_stub
        )
        with pytest.raises(_base._StubHomeAssistantError):
            wsapi._do_reference_data(FakeHass(), {})

    def test_no_state_machine_substrate_raises(self, monkeypatch):
        # Core drift: hass.states.async_all is absent/renamed. With include_states
        # (the default), this must RAISE rather than serve an empty entity universe.
        monkeypatch.setitem(
            sys.modules, "homeassistant.exceptions", _base._exceptions_stub
        )
        hass = FakeHass(services=FakeServices({"light": {"turn_on": object()}}))
        hass.states = None  # no async_all → drifted state machine
        with pytest.raises(_base._StubHomeAssistantError):
            wsapi._do_reference_data(hass, {})

    def test_genuinely_empty_services_still_returns_empty(self, monkeypatch):
        # A PRESENT-but-empty service registry (a real empty Mapping) is NOT drift:
        # it returns an empty catalog, not a raise. This keeps the drift guard keyed
        # off unavailability, not off emptiness.
        hass = FakeHass(states=[FakeState("light.a")], services=FakeServices({}))
        res = wsapi._do_reference_data(hass, {})
        assert res["services"] == []
        assert res["entity_ids"] == ["light.a"]


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

    def test_accepts_all_nine_known_keys(self, monkeypatch):
        visibility = {
            "exclude_categories": ["config"],
            "deny_entity_ids": ["light.a"],
            "exclude_areas": ["office"],
            "exclude_labels": ["lb"],
            "allow_entity_ids": ["light.b"],
            "allow_areas": ["kitchen"],
            "allow_labels": ["lb2"],
            "exclude_hidden": True,
            "respect_assist_exposure": True,
        }
        assert set(visibility) == set(wsapi._VISIBILITY_LIST_KEYS) | set(
            wsapi._VISIBILITY_BOOL_KEYS
        )
        out = self._schema(monkeypatch)(
            {"type": wsapi.WS_SEARCH, "visibility": visibility}
        )
        assert out["visibility"] == visibility

    def test_rejects_unknown_visibility_key(self, monkeypatch):
        # A typo'd / newer-server dimension is rejected loudly (the server then falls
        # back to legacy with the filter applied) rather than silently ignored.
        with pytest.raises(_REAL_VOL.Invalid):
            self._schema(monkeypatch)(
                {"type": wsapi.WS_SEARCH, "visibility": {"exclude_area": ["x"]}}
            )

    def test_rejects_non_dict_visibility(self, monkeypatch):
        with pytest.raises(_REAL_VOL.Invalid):
            self._schema(monkeypatch)({"type": wsapi.WS_SEARCH, "visibility": "nope"})


# =============================================================================
# _assist_should_expose — READ-ONLY reconstruction of core's async_should_expose
# =============================================================================
class _NamedHomeAssistantError(Exception):
    """Exception whose __name__ is ``HomeAssistantError`` (what _is_unknown_entity_error
    keys off, since the fake suite stubs the real class)."""


# _is_unknown_entity_error matches on the type NAME, so alias to the exact name.
_NamedHomeAssistantError.__name__ = "HomeAssistantError"


def _boom(*_a, **_k):
    raise AssertionError("must not be consulted")


class TestAssistShouldExposeReadOnly:
    """``_assist_should_expose`` reconstructs core's ``async_should_expose`` WITHOUT
    the write side effect: an explicit per-entity override wins; else ``expose_new``
    gates the default-exposure check. It must never call core's ``async_should_expose``
    (which persists computed defaults back onto unstamped entities)."""

    def test_explicit_override_true_wins(self, monkeypatch):
        monkeypatch.setattr(
            wsapi,
            "_async_get_entity_settings",
            lambda hass, eid: {"conversation": {"should_expose": True}},
        )
        # expose_new / default must NOT be consulted when an override exists.
        monkeypatch.setattr(wsapi, "_assist_expose_new_entities", _boom)
        assert wsapi._assist_should_expose(FakeHass(), "light.a") is True

    def test_explicit_override_false_wins(self, monkeypatch):
        monkeypatch.setattr(
            wsapi,
            "_async_get_entity_settings",
            lambda hass, eid: {"conversation": {"should_expose": False}},
        )
        monkeypatch.setattr(wsapi, "_assist_expose_new_entities", _boom)
        assert wsapi._assist_should_expose(FakeHass(), "light.a") is False

    def test_no_override_expose_new_off_is_false(self, monkeypatch):
        monkeypatch.setattr(wsapi, "_async_get_entity_settings", lambda hass, eid: {})
        monkeypatch.setattr(wsapi, "_assist_expose_new_entities", lambda hass: False)
        monkeypatch.setattr(wsapi, "_assist_default_exposed", _boom)
        assert wsapi._assist_should_expose(FakeHass(), "light.a") is False

    @pytest.mark.parametrize("default_exposed", [True, False])
    def test_no_override_expose_new_on_uses_default(self, monkeypatch, default_exposed):
        monkeypatch.setattr(wsapi, "_async_get_entity_settings", lambda hass, eid: {})
        monkeypatch.setattr(wsapi, "_assist_expose_new_entities", lambda hass: True)
        monkeypatch.setattr(
            wsapi, "_assist_default_exposed", lambda hass, eid: default_exposed
        )
        assert wsapi._assist_should_expose(FakeHass(), "light.a") is default_exposed

    def test_unknown_entity_falls_to_default(self, monkeypatch):
        # An id in neither the registry nor the store raises "Unknown entity" from the
        # settings read; that is NOT an explicit override — fall to the default.
        def _raise(hass, eid):
            raise _NamedHomeAssistantError("Unknown entity")

        monkeypatch.setattr(wsapi, "_async_get_entity_settings", _raise)
        monkeypatch.setattr(wsapi, "_assist_expose_new_entities", lambda hass: True)
        monkeypatch.setattr(wsapi, "_assist_default_exposed", lambda hass, eid: True)
        assert wsapi._assist_should_expose(FakeHass(), "sensor.states_only") is True

    def test_fails_open_on_error(self, monkeypatch):
        def _raise(hass):
            raise RuntimeError("boom")

        monkeypatch.setattr(wsapi, "_async_get_entity_settings", lambda hass, eid: {})
        monkeypatch.setattr(wsapi, "_assist_expose_new_entities", _raise)
        # Any error means "do not hide" (fail open) — mirrors the resolver.
        assert wsapi._assist_should_expose(FakeHass(), "light.a") is True

    def test_never_calls_core_writing_should_expose(self):
        # The read-only guarantee is structural: the recomposition never CALLS core's
        # writing async_should_expose (the call form ``async_should_expose(`` — the
        # docstrings reference it in prose, without parens, to explain why). A
        # regression that reintroduces the call fails here.
        import inspect

        for fn in (
            wsapi._assist_should_expose,
            wsapi._explicit_assist_exposure,
            wsapi._assist_expose_new_entities,
            wsapi._assist_default_exposed,
        ):
            assert "async_should_expose(" not in inspect.getsource(fn)


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
    def test_admin_call_sends_result(self, functional_ws, command, monkeypatch):
        handler = functional_ws.registered[command]
        conn = _FakeConnection(is_admin=True)
        msg = {"id": 7, "type": command, **_NEW_CMD_MSG_EXTRA.get(command, {})}
        if command == wsapi.WS_REFERENCE_DATA:
            # A bare FakeHass serves no services mapping, and reference_data's
            # whole answer IS that substrate — the drift guard correctly raises
            # (→ command error → the server's legacy REST fallback). The admin
            # gate provably admitted the call because the raise comes from the
            # pure handler; result-shape coverage lives in TestReferenceData.
            monkeypatch.setitem(
                sys.modules, "homeassistant.exceptions", _base._exceptions_stub
            )
            with pytest.raises(_base._StubHomeAssistantError):
                handler(FakeHass(), conn, msg)
            return
        handler(FakeHass(), conn, msg)
        assert 7 in conn.results
        assert isinstance(conn.results[7], dict)


# =============================================================================
# call_service (Phase 3 — the FIRST write capability, issue #1813)
# =============================================================================
class _StubServiceNotFound(_base._StubHomeAssistantError):
    """Stand-in for core's ``ServiceNotFound`` (a ``HomeAssistantError`` subclass).

    ``_call_service_prep`` does a function-local ``from homeassistant.exceptions
    import ServiceNotFound`` and ``raise ServiceNotFound(domain, service)``; the
    module is MagicMock-stubbed, so a REAL exception class must back the attribute
    for the raise path to be exercisable (mirrors ``_StubHomeAssistantError``).
    Subclassing the HomeAssistantError stub mirrors core's real class hierarchy.
    """

    def __init__(self, domain=None, service=None):
        super().__init__(f"Service {domain}.{service} not found")
        self.domain = domain
        self.service = service


# Pin ServiceNotFound onto the shared exceptions stub so the raise-path tests'
# ``monkeypatch.setitem(sys.modules, "homeassistant.exceptions", _exceptions_stub)``
# binds a real class (the base module already pins ``HomeAssistantError``).
_base._exceptions_stub.ServiceNotFound = _StubServiceNotFound

# ``_call_service_prep`` imports ``EVENT_STATE_CHANGED`` from ``homeassistant.const``
# (function-locally, only when it registers the confirmation listener). Under the
# MagicMock stub that submodule is absent, so ensure it resolves; the fake bus
# ignores the event-type value, so a literal is sufficient.
# ``homeassistant.const.EVENT_STATE_CHANGED`` is guaranteed for every unit test by
# an autouse fixture in conftest.py (the component's call_service waiter imports it
# function-locally). The module-level default here keeps this file importable on
# its own; conftest handles the full-suite collection-order case.
sys.modules.setdefault(
    "homeassistant.const", SimpleNamespace(EVENT_STATE_CHANGED="state_changed")
)


class _FakeEvent:
    """A ``state_changed`` event — only ``.data`` is read by the listener."""

    def __init__(self, entity_id, new_state):
        self.data = {"entity_id": entity_id, "new_state": new_state}


class _FakeBus:
    """Records ``async_listen`` callbacks; lets a test fire a state_changed event.

    ``async_listen(event_type, cb)`` stores ``cb`` and returns an unsub that counts
    its calls; ``fire(entity_id, new_state)`` drives every stored callback with a
    fake event (simulating the in-process bus delivering the confirming transition).
    """

    def __init__(self):
        self.listeners = []
        self.unsub_count = 0

    def async_listen(self, event_type, cb):
        self.listeners.append((event_type, cb))

        def _unsub():
            self.unsub_count += 1

        return _unsub

    def fire(self, entity_id, new_state):
        event = _FakeEvent(entity_id, new_state)
        for _event_type, cb in list(self.listeners):
            cb(event)


class _FakeCallServices:
    """``hass.services`` stand-in with the write surface ``call_service`` drives.

    ``has_service`` answers from a known ``{(domain, service)}`` set; ``async_call``
    records its args, optionally runs an ``on_call`` hook (used to fire the
    confirming state_changed event mid-dispatch), optionally raises, else returns a
    canned ``response``.
    """

    def __init__(self, *, known=(), response=None, on_call=None, raises=None):
        self._known = set(known)
        self._response = response
        self._on_call = on_call
        self._raises = raises
        self.calls = []

    def has_service(self, domain, service):
        return (domain, service) in self._known

    async def async_call(
        self, domain, service, service_data, blocking=True, return_response=False
    ):
        self.calls.append(
            {
                "domain": domain,
                "service": service,
                "service_data": service_data,
                "blocking": blocking,
                "return_response": return_response,
            }
        )
        if self._raises is not None:
            raise self._raises
        if self._on_call is not None:
            self._on_call()
        return self._response

    @property
    def call_count(self):
        return len(self.calls)


def _call_hass(states, services, bus):
    hass = FakeHass(states=list(states), services=services)
    hass.bus = bus
    return hass


def _run_call_service(hass, msg):
    """Drive the async prep then the pure formatter, as the WS wrapper does."""
    extra = asyncio.run(wsapi._call_service_prep(hass, msg))
    return wsapi._do_call_service(hass, msg, **extra)


class TestCallServiceDomainBlock:
    """D1 — the authoritative, component-side ``ha_mcp_tools`` domain block.

    THE load-bearing security test: a component ``call_service`` that skipped this
    would let a caller invoke the admin-gated ``ha_mcp_tools.get_caller_token``
    in-process (the server IS admin) and then every file/YAML service. The block
    must fire BEFORE ``has_service`` / any dispatch, so ``async_call`` is NEVER
    reached — even when the service would otherwise exist.
    """

    @pytest.mark.parametrize("domain", ["ha_mcp_tools", " HA_MCP_TOOLS "])
    def test_domain_block_never_dispatches(self, monkeypatch, domain):
        monkeypatch.setitem(
            sys.modules, "homeassistant.exceptions", _base._exceptions_stub
        )
        bus = _FakeBus()
        # The gateway service EXISTS in the registry — only the D1 block stops it.
        services = _FakeCallServices(known={("ha_mcp_tools", "get_caller_token")})
        hass = _call_hass([], services, bus)
        msg = {
            "type": wsapi.WS_CALL_SERVICE,
            "domain": domain,
            "service": "get_caller_token",
        }
        with pytest.raises(_base._StubHomeAssistantError) as exc:
            asyncio.run(wsapi._call_service_prep(hass, msg))
        # It is the D1 refusal, not a coincidental ServiceNotFound.
        assert "not callable" in str(exc.value)
        assert not isinstance(exc.value, _StubServiceNotFound)
        # THE assertion: async_call was NEVER reached; no listener was registered.
        assert services.call_count == 0
        assert bus.listeners == []
        assert bus.unsub_count == 0


class TestCallServiceNotFound:
    def test_unknown_service_raises_before_dispatch(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "homeassistant.exceptions", _base._exceptions_stub
        )
        bus = _FakeBus()
        services = _FakeCallServices(known=set())  # nothing registered
        hass = _call_hass([], services, bus)
        msg = {
            "type": wsapi.WS_CALL_SERVICE,
            "domain": "light",
            "service": "does_not_exist",
        }
        with pytest.raises(_StubServiceNotFound):
            asyncio.run(wsapi._call_service_prep(hass, msg))
        assert services.call_count == 0  # refused before any dispatch
        assert bus.listeners == []  # raised before register-before-fire


class TestCallServiceHappyPath:
    def test_confirmed_real_transition(self):
        old = FakeState("light.a", state="off", brightness=100)
        new = FakeState("light.a", state="on", brightness=255)
        bus = _FakeBus()
        services = _FakeCallServices(
            known={("light", "turn_on")},
            on_call=lambda: bus.fire("light.a", new),
        )
        hass = _call_hass([old], services, bus)
        msg = {
            "type": wsapi.WS_CALL_SERVICE,
            "domain": "light",
            "service": "turn_on",
            "entity_ids": ["light.a"],
            "service_data": {"brightness": 255},
        }
        res = _run_call_service(hass, msg)

        assert res["dispatched"] is True
        assert res["confirmed"] is True
        assert res["partial"] is False
        assert "service_response" not in res  # not requested
        (transition,) = res["transitions"]
        assert transition["entity_id"] == "light.a"
        assert transition["old_state"]["state"] == "off"
        assert transition["new_state"]["state"] == "on"
        assert transition["changed"] is True
        assert transition["attributes_changed"] == ["brightness"]
        # Exactly one blocking dispatch, with the given service_data.
        assert services.call_count == 1
        assert services.calls[0]["blocking"] is True
        assert services.calls[0]["service_data"] == {"brightness": 255}
        # Register-before-fire listener torn down.
        assert bus.unsub_count == 1

    def test_attributes_changed_lists_only_differing_keys(self):
        old = FakeState("climate.a", state="heat", temperature=19, hvac_mode="heat")
        new = FakeState("climate.a", state="heat", temperature=21, hvac_mode="heat")
        bus = _FakeBus()
        services = _FakeCallServices(
            known={("climate", "set_temperature")},
            on_call=lambda: bus.fire("climate.a", new),
        )
        hass = _call_hass([old], services, bus)
        res = _run_call_service(
            hass,
            {
                "type": wsapi.WS_CALL_SERVICE,
                "domain": "climate",
                "service": "set_temperature",
                "entity_ids": ["climate.a"],
            },
        )
        transition = res["transitions"][0]
        # state itself unchanged, only the temperature attribute moved.
        assert transition["changed"] is False
        assert transition["attributes_changed"] == ["temperature"]
        assert res["confirmed"] is True


class TestCallServiceWait:
    def test_timeout_is_partial_not_failure(self):
        old = FakeState("light.a", state="off")
        bus = _FakeBus()
        services = _FakeCallServices(known={("light", "turn_on")})  # no event fired
        hass = _call_hass([old], services, bus)
        res = _run_call_service(
            hass,
            {
                "type": wsapi.WS_CALL_SERVICE,
                "domain": "light",
                "service": "turn_on",
                "entity_ids": ["light.a"],
                "timeout": 0.01,
            },
        )
        # The wait lapsed but the call landed: partial success, no raise.
        assert res["dispatched"] is True
        assert res["confirmed"] is False
        assert res["partial"] is True
        transition = res["transitions"][0]
        # Backstop re-read from states.get returns the (unchanged) current state.
        assert transition["new_state"]["state"] == "off"
        assert transition["changed"] is False
        assert bus.unsub_count == 1  # unsub ran on the timeout path too

    def test_post_dispatch_timeout_does_not_raise(self):
        # async_call SUCCEEDS (dispatched=True) but the confirmation never arrives —
        # this must NOT surface as a call failure: the write already landed.
        bus = _FakeBus()
        services = _FakeCallServices(known={("lock", "lock")})
        hass = _call_hass([FakeState("lock.front", state="unlocked")], services, bus)
        res = _run_call_service(
            hass,
            {
                "type": wsapi.WS_CALL_SERVICE,
                "domain": "lock",
                "service": "lock",
                "entity_ids": ["lock.front"],
                "timeout": 0.01,
            },
        )
        assert services.call_count == 1  # dispatch happened
        assert res["dispatched"] is True
        assert res["partial"] is True


class TestCallServiceReturnResponse:
    def test_present_when_requested_and_nonnull(self):
        old = FakeState("light.a", state="off")
        new = FakeState("light.a", state="on")
        bus = _FakeBus()
        services = _FakeCallServices(
            known={("light", "turn_on")},
            response={"changed_states": [{"entity_id": "light.a"}]},
            on_call=lambda: bus.fire("light.a", new),
        )
        hass = _call_hass([old], services, bus)
        res = _run_call_service(
            hass,
            {
                "type": wsapi.WS_CALL_SERVICE,
                "domain": "light",
                "service": "turn_on",
                "entity_ids": ["light.a"],
                "return_response": True,
            },
        )
        assert res["service_response"] == {"changed_states": [{"entity_id": "light.a"}]}
        assert services.calls[0]["return_response"] is True

    def test_absent_when_not_requested(self):
        # A non-None response is still omitted when return_response is not set.
        services = _FakeCallServices(known={("light", "turn_on")}, response={"x": 1})
        hass = _call_hass([], services, _FakeBus())
        res = _run_call_service(
            hass,
            {
                "type": wsapi.WS_CALL_SERVICE,
                "domain": "light",
                "service": "turn_on",
                "entity_ids": [],
            },
        )
        assert "service_response" not in res

    def test_absent_when_response_is_none(self):
        services = _FakeCallServices(known={("light", "turn_on")}, response=None)
        hass = _call_hass([], services, _FakeBus())
        res = _run_call_service(
            hass,
            {
                "type": wsapi.WS_CALL_SERVICE,
                "domain": "light",
                "service": "turn_on",
                "entity_ids": [],
                "return_response": True,
            },
        )
        assert "service_response" not in res


class TestCallServiceNonEntity:
    def test_no_wait_no_listener(self):
        bus = _FakeBus()
        services = _FakeCallServices(known={("automation", "trigger")})
        hass = _call_hass([], services, bus)
        res = _run_call_service(
            hass,
            {
                "type": wsapi.WS_CALL_SERVICE,
                "domain": "automation",
                "service": "trigger",
                "entity_ids": [],
            },
        )
        assert res["dispatched"] is True
        assert res["confirmed"] is False
        assert res["partial"] is False
        assert res["transitions"] == []
        assert services.call_count == 1
        # Nothing to confirm → no listener registered (register-before-fire is moot).
        assert bus.listeners == []


class TestCallServiceUnsub:
    def test_unsub_runs_when_dispatch_raises(self, monkeypatch):
        # A post-registration dispatch failure (async_call raises) must still tear
        # down the listener via the finally, and the exception must propagate.
        monkeypatch.setitem(
            sys.modules, "homeassistant.exceptions", _base._exceptions_stub
        )
        boom = _base._StubHomeAssistantError("dispatch failed")
        bus = _FakeBus()
        services = _FakeCallServices(known={("light", "turn_on")}, raises=boom)
        hass = _call_hass([FakeState("light.a", state="off")], services, bus)
        msg = {
            "type": wsapi.WS_CALL_SERVICE,
            "domain": "light",
            "service": "turn_on",
            "entity_ids": ["light.a"],
        }
        with pytest.raises(_base._StubHomeAssistantError):
            asyncio.run(wsapi._call_service_prep(hass, msg))
        # Register-before-fire happened, and the finally unsubbed exactly once.
        assert len(bus.listeners) == 1
        assert bus.unsub_count == 1
        assert services.call_count == 1


class TestCallServiceSchema:
    def _schema(self, monkeypatch):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        return _REAL_VOL.Schema(wsapi._call_service_schema())

    def test_defaults(self, monkeypatch):
        out = self._schema(monkeypatch)(
            {"type": wsapi.WS_CALL_SERVICE, "domain": "light", "service": "turn_on"}
        )
        assert out["service_data"] == {}
        assert out["entity_ids"] == []
        assert out["wait"] is True
        assert out["timeout"] == 10.0
        assert out["return_response"] is False

    def test_mutable_defaults_are_fresh_instances(self, monkeypatch):
        schema = self._schema(monkeypatch)
        a = schema({"type": wsapi.WS_CALL_SERVICE, "domain": "l", "service": "s"})
        b = schema({"type": wsapi.WS_CALL_SERVICE, "domain": "l", "service": "s"})
        # The callable defaults must yield distinct objects, never a shared mutable.
        assert a["service_data"] is not b["service_data"]
        assert a["entity_ids"] is not b["entity_ids"]

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "ha_mcp_tools/call_service", "service": "turn_on"},  # no domain
            {"type": "ha_mcp_tools/call_service", "domain": "light"},  # no service
            {  # timeout over the 60s cap
                "type": "ha_mcp_tools/call_service",
                "domain": "light",
                "service": "turn_on",
                "timeout": 120,
            },
            {  # entity_ids must be a list of strings
                "type": "ha_mcp_tools/call_service",
                "domain": "light",
                "service": "turn_on",
                "entity_ids": "light.a",
            },
        ],
    )
    def test_malformed_rejected(self, monkeypatch, bad):
        with pytest.raises(_REAL_VOL.Invalid):
            self._schema(monkeypatch)(bad)

    def test_timeout_at_cap_accepted(self, monkeypatch):
        out = self._schema(monkeypatch)(
            {
                "type": wsapi.WS_CALL_SERVICE,
                "domain": "light",
                "service": "turn_on",
                "timeout": 60,
            }
        )
        assert out["timeout"] == 60


# =============================================================================
# bulk_call_service (Phase 3, D5a — the BATCH write capability, issue #1813)
# =============================================================================
class _FakeBulkServices:
    """``hass.services`` stand-in with PER-``(domain, service)`` dispatch behavior.

    ``behaviors`` maps ``(domain, service)`` to an optional dict carrying ``on_call``
    (a hook run mid-dispatch, e.g. fire THIS op's confirming state_changed event),
    ``raises`` (an exception to raise for THIS op only), and ``response``. Unlike the
    single-call ``_FakeCallServices`` (one shared hook / raise), the per-op routing
    lets one batch op raise while another confirms — the parallel-isolation case.
    ``has_service`` answers from the behavior keys plus any extra ``known``.
    """

    def __init__(self, behaviors=None, known=()):
        self._behaviors = {k: dict(v) for k, v in dict(behaviors or {}).items()}
        self._known = set(known) | set(self._behaviors)
        self.calls = []

    def has_service(self, domain, service):
        return (domain, service) in self._known

    async def async_call(
        self, domain, service, service_data, blocking=True, return_response=False
    ):
        self.calls.append(
            {
                "domain": domain,
                "service": service,
                "service_data": service_data,
                "blocking": blocking,
                "return_response": return_response,
            }
        )
        behavior = self._behaviors.get((domain, service), {})
        if behavior.get("raises") is not None:
            raise behavior["raises"]
        if behavior.get("on_call") is not None:
            behavior["on_call"]()
        return behavior.get("response")

    @property
    def call_count(self):
        return len(self.calls)


def _run_bulk_call_service(hass, msg):
    """Drive the async prep then the pure formatter, as the WS wrapper does."""
    extra = asyncio.run(wsapi._bulk_call_service_prep(hass, msg))
    return wsapi._do_bulk_call_service(hass, msg, **extra)


class TestBulkCallServiceDomainBlock:
    """D1 batch fail-closed — THE load-bearing batch security test.

    A batch where even ONE op targets ``ha_mcp_tools`` must raise BEFORE any
    dispatch or listener registration, so NOTHING in the batch lands. The
    ``ha_mcp_tools`` op is placed SECOND, behind a perfectly valid ``light`` op, to
    prove the whole batch is guarded up front (all-guards-first): the valid op ahead
    of it is never dispatched either.
    """

    @pytest.mark.parametrize("bad_domain", ["ha_mcp_tools", " HA_MCP_TOOLS "])
    def test_one_ha_mcp_tools_op_dispatches_nothing(self, monkeypatch, bad_domain):
        monkeypatch.setitem(
            sys.modules, "homeassistant.exceptions", _base._exceptions_stub
        )
        bus = _FakeBus()
        # Both services EXIST in the registry — only the D1 block stops the batch.
        services = _FakeBulkServices(
            known={("light", "turn_on"), ("ha_mcp_tools", "get_caller_token")}
        )
        hass = _call_hass([FakeState("light.a", state="off")], services, bus)
        msg = {
            "type": wsapi.WS_BULK_CALL_SERVICE,
            "operations": [
                {"domain": "light", "service": "turn_on", "entity_ids": ["light.a"]},
                {"domain": bad_domain, "service": "get_caller_token"},
            ],
        }
        with pytest.raises(_base._StubHomeAssistantError) as exc:
            asyncio.run(wsapi._bulk_call_service_prep(hass, msg))
        # The D1 refusal, not a coincidental ServiceNotFound.
        assert "not callable" in str(exc.value)
        assert not isinstance(exc.value, _StubServiceNotFound)
        # THE assertion: ZERO dispatches for the whole batch — not even the valid
        # ``light`` op ahead of the refused one — and no listener was registered.
        assert services.call_count == 0
        assert bus.listeners == []
        assert bus.unsub_count == 0


class TestBulkCallServiceNotFound:
    def test_one_unknown_service_aborts_whole_batch(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "homeassistant.exceptions", _base._exceptions_stub
        )
        bus = _FakeBus()
        # Only ``light.turn_on`` exists; the second op's service is unknown.
        services = _FakeBulkServices(known={("light", "turn_on")})
        hass = _call_hass([FakeState("light.a", state="off")], services, bus)
        msg = {
            "type": wsapi.WS_BULK_CALL_SERVICE,
            "operations": [
                {"domain": "light", "service": "turn_on", "entity_ids": ["light.a"]},
                {"domain": "light", "service": "does_not_exist"},
            ],
        }
        with pytest.raises(_StubServiceNotFound):
            asyncio.run(wsapi._bulk_call_service_prep(hass, msg))
        # Pre-dispatch abort: nothing fired, no register-before-fire listener.
        assert services.call_count == 0
        assert bus.listeners == []


class TestBulkCallServiceHappyPath:
    def test_parallel_both_confirmed_real_transitions(self):
        old_a = FakeState("light.a", state="off", brightness=100)
        new_a = FakeState("light.a", state="on", brightness=255)
        old_b = FakeState("switch.b", state="off")
        new_b = FakeState("switch.b", state="on")
        bus = _FakeBus()
        services = _FakeBulkServices(
            behaviors={
                ("light", "turn_on"): {"on_call": lambda: bus.fire("light.a", new_a)},
                ("switch", "turn_on"): {"on_call": lambda: bus.fire("switch.b", new_b)},
            }
        )
        hass = _call_hass([old_a, old_b], services, bus)
        res = _run_bulk_call_service(
            hass,
            {
                "type": wsapi.WS_BULK_CALL_SERVICE,
                "operations": [
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "entity_ids": ["light.a"],
                        "service_data": {"brightness": 255},
                    },
                    {
                        "domain": "switch",
                        "service": "turn_on",
                        "entity_ids": ["switch.b"],
                    },
                ],
                "parallel": True,
            },
        )
        assert res["total"] == 2
        assert res["dispatched"] == 2
        assert res["failed"] == 0
        op1, op2 = res["operations"]
        # Op order is preserved regardless of dispatch fan-out.
        assert op1["domain"] == "light"
        assert op2["domain"] == "switch"
        for op in (op1, op2):
            assert op["dispatched"] is True
            assert op["confirmed"] is True
            assert op["partial"] is False
            assert "error" not in op
        (t1,) = op1["transitions"]
        assert t1["old_state"]["state"] == "off"
        assert t1["new_state"]["state"] == "on"
        assert t1["changed"] is True
        assert t1["attributes_changed"] == ["brightness"]
        # Both ops fired exactly once, with the per-op service_data preserved.
        assert services.call_count == 2
        assert {(c["domain"], c["service"]) for c in services.calls} == {
            ("light", "turn_on"),
            ("switch", "turn_on"),
        }
        assert services.calls[0]["service_data"] == {"brightness": 255}
        # Every register-before-fire listener torn down.
        assert bus.unsub_count == 2


class TestBulkCallServiceSequential:
    def test_parallel_false_dispatches_in_order(self):
        new_a = FakeState("light.a", state="on")
        new_b = FakeState("light.b", state="on")
        bus = _FakeBus()
        services = _FakeBulkServices(
            behaviors={
                ("light", "turn_on"): {"on_call": lambda: bus.fire("light.a", new_a)},
                ("light", "turn_off"): {"on_call": lambda: bus.fire("light.b", new_b)},
            }
        )
        hass = _call_hass(
            [FakeState("light.a", state="off"), FakeState("light.b", state="on")],
            services,
            bus,
        )
        res = _run_bulk_call_service(
            hass,
            {
                "type": wsapi.WS_BULK_CALL_SERVICE,
                "operations": [
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "entity_ids": ["light.a"],
                    },
                    {
                        "domain": "light",
                        "service": "turn_off",
                        "entity_ids": ["light.b"],
                    },
                ],
                "parallel": False,
            },
        )
        assert res["dispatched"] == 2
        # Sequential awaits: the calls land in operation order.
        assert [(c["domain"], c["service"]) for c in services.calls] == [
            ("light", "turn_on"),
            ("light", "turn_off"),
        ]
        assert all(op["confirmed"] is True for op in res["operations"])


class TestBulkCallServiceParallelIsolation:
    def test_one_op_raises_others_unaffected(self):
        # Under ``parallel`` (gather return_exceptions), one op's async_call raising
        # is captured on THAT op — the OTHER op still dispatches and confirms.
        boom = _base._StubHomeAssistantError("boom")
        new_b = FakeState("switch.b", state="on")
        bus = _FakeBus()
        services = _FakeBulkServices(
            behaviors={
                ("light", "turn_on"): {"raises": boom},
                ("switch", "turn_on"): {"on_call": lambda: bus.fire("switch.b", new_b)},
            }
        )
        hass = _call_hass(
            [FakeState("light.a", state="off"), FakeState("switch.b", state="off")],
            services,
            bus,
        )
        res = _run_bulk_call_service(
            hass,
            {
                "type": wsapi.WS_BULK_CALL_SERVICE,
                "operations": [
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "entity_ids": ["light.a"],
                    },
                    {
                        "domain": "switch",
                        "service": "turn_on",
                        "entity_ids": ["switch.b"],
                    },
                ],
                "parallel": True,
            },
        )
        assert res["total"] == 2
        assert res["dispatched"] == 1
        assert res["failed"] == 1
        failed, ok = res["operations"]
        # The failed op: error recorded, NOT dispatched, neither confirmed nor partial.
        assert failed["domain"] == "light"
        assert failed["dispatched"] is False
        assert failed["confirmed"] is False
        assert failed["partial"] is False
        assert "boom" in failed["error"]
        # The other op is entirely unaffected — it dispatched and confirmed.
        assert ok["domain"] == "switch"
        assert ok["dispatched"] is True
        assert ok["confirmed"] is True
        assert "error" not in ok
        # Both listeners (including the failed op's) were torn down.
        assert bus.unsub_count == 2


class TestBulkCallServiceWait:
    def test_no_events_all_partial_no_raise(self):
        # No op fires its confirming event: the one shared deadline lapses and every
        # confirmable op is ``partial`` (dispatched, unconfirmed) — never a failure.
        bus = _FakeBus()
        services = _FakeBulkServices(
            known={("light", "turn_on"), ("lock", "lock")}  # no on_call anywhere
        )
        hass = _call_hass(
            [
                FakeState("light.a", state="off"),
                FakeState("lock.front", state="unlocked"),
            ],
            services,
            bus,
        )
        res = _run_bulk_call_service(
            hass,
            {
                "type": wsapi.WS_BULK_CALL_SERVICE,
                "operations": [
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "entity_ids": ["light.a"],
                    },
                    {
                        "domain": "lock",
                        "service": "lock",
                        "entity_ids": ["lock.front"],
                    },
                ],
                "timeout": 0.01,
            },
        )
        assert res["dispatched"] == 2
        assert res["failed"] == 0
        for op in res["operations"]:
            assert op["dispatched"] is True
            assert op["confirmed"] is False
            assert op["partial"] is True
        # unsub ran on the timeout path too, for every registered listener.
        assert bus.unsub_count == 2


class TestBulkCallServiceUnsub:
    def test_every_registered_listener_torn_down(self):
        # A mix of a confirmable op (registers a listener) and a non-entity op
        # (registers none): every listener that WAS registered is torn down, and the
        # non-entity op registers nothing (register-before-fire is moot for it).
        new_a = FakeState("light.a", state="on")
        bus = _FakeBus()
        services = _FakeBulkServices(
            behaviors={
                ("light", "turn_on"): {"on_call": lambda: bus.fire("light.a", new_a)},
                ("automation", "trigger"): {},
            }
        )
        hass = _call_hass([FakeState("light.a", state="off")], services, bus)
        res = _run_bulk_call_service(
            hass,
            {
                "type": wsapi.WS_BULK_CALL_SERVICE,
                "operations": [
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "entity_ids": ["light.a"],
                    },
                    {"domain": "automation", "service": "trigger", "entity_ids": []},
                ],
            },
        )
        # Only the confirmable op registered a listener; it was torn down.
        assert len(bus.listeners) == 1
        assert bus.unsub_count == len(bus.listeners) == 1
        light_op, automation_op = res["operations"]
        assert light_op["confirmed"] is True
        # The non-entity op has nothing to confirm: no transitions, not partial.
        assert automation_op["dispatched"] is True
        assert automation_op["confirmed"] is False
        assert automation_op["partial"] is False
        assert automation_op["transitions"] == []


class TestBulkCallServiceSchema:
    def _schema(self, monkeypatch):
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        return _REAL_VOL.Schema(wsapi._bulk_call_service_schema())

    def test_defaults(self, monkeypatch):
        out = self._schema(monkeypatch)(
            {
                "type": wsapi.WS_BULK_CALL_SERVICE,
                "operations": [{"domain": "light", "service": "turn_on"}],
            }
        )
        assert out["parallel"] is True
        assert out["wait"] is True
        assert out["timeout"] == 10.0
        op = out["operations"][0]
        assert op["service_data"] == {}
        assert op["entity_ids"] == []

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "ha_mcp_tools/bulk_call_service"},  # operations missing
            {  # operations must be non-empty
                "type": "ha_mcp_tools/bulk_call_service",
                "operations": [],
            },
            {  # timeout over the 60s cap
                "type": "ha_mcp_tools/bulk_call_service",
                "operations": [{"domain": "light", "service": "turn_on"}],
                "timeout": 120,
            },
            {  # an operation missing its required service
                "type": "ha_mcp_tools/bulk_call_service",
                "operations": [{"domain": "light"}],
            },
        ],
    )
    def test_malformed_rejected(self, monkeypatch, bad):
        with pytest.raises(_REAL_VOL.Invalid):
            self._schema(monkeypatch)(bad)
