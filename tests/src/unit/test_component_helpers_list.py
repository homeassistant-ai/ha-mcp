"""``ha_mcp_tools/helpers_list``: stored collection helpers + flow helpers.

Collection helpers come from Core's storage collections (what ``<type>/list``
returns); flow helpers from ``ConfigEntry.options``, never ``entry.data``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from .test_component_ws_search import (
    FakeCollectionEntity,
    FakeComponent,
    FakeConfig,
    FakeConfigEntry,
    FakeHass,
    FakeRegEntry,
    FakeState,
    empty_view,  # noqa: F401 - pytest fixture, applied to the class
    make_view,
    stored_helpers,
    wsapi,
)


@pytest.mark.usefixtures("empty_view")
class TestHelpersList:
    @staticmethod
    def _stored_world(
        collections: dict[str, list[dict[str, Any]]],
        states: Iterable[FakeState] = (),
        entries: Iterable[tuple[str, str, str, str | None]] = (),
    ) -> tuple[FakeHass, Any]:
        """A hass whose helpers live in Core's storage collections, plus a view
        whose registry binds each ``(entity_id, platform, unique_id)``."""
        view = make_view(
            entity={
                entity_id: FakeRegEntry(
                    entity_id, platform=platform, unique_id=unique_id, name=name
                )
                for entity_id, platform, unique_id, name in entries
            }
        )
        return FakeHass(states=states, data=stored_helpers(collections)), view

    def test_listing_reports_stored_config_not_runtime_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #2901: input_select.set_options changes only the runtime options; the
        # listing must report the stored helper, as input_select/list does, not
        # the state attributes (runtime options, editable, friendly_name).
        stored = {
            "id": "mode",
            "name": "Mode",
            "options": ["stored_a", "stored_b"],
            "initial": "stored_b",
        }
        state = FakeState(
            "input_select.mode",
            "runtime_x",
            friendly_name="Mode",
            options=["runtime_x", "runtime_y"],
            editable=True,
        )
        hass, view = self._stored_world(
            {"input_select": [stored]},
            states=[state],
            entries=[("input_select.mode", "input_select", "mode", None)],
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: view)
        res = wsapi._do_helpers_list(hass, {"helper_types": ["input_select"]})
        (rec,) = res["helpers"]
        assert rec["config"] == stored
        assert rec["storage_id"] == "mode"
        assert rec["entity_id"] == "input_select.mode"

    def test_rename_shows_current_values_issue_1794(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The stored name is the creation-time one; a rename only changes the
        # registry, so the record's name is the current one.
        hass, view = self._stored_world(
            {"input_boolean": [{"id": "guest_mode", "name": "Old Name"}]},
            states=[
                FakeState(
                    "input_boolean.guest_mode", "off", friendly_name="Current Guest"
                )
            ],
            entries=[
                (
                    "input_boolean.guest_mode",
                    "input_boolean",
                    "guest_mode",
                    "Current Guest",
                )
            ],
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: view)
        (rec,) = wsapi._do_helpers_list(hass, {})["helpers"]
        assert rec["name"] == "Current Guest"
        assert rec["storage_id"] == "guest_mode"

    def test_helper_without_state_is_still_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A disabled helper has no state but is still stored, and <type>/list
        # lists it; the listing must not drop it.
        hass, view = self._stored_world(
            {"counter": [{"id": "visits", "name": "Visits", "step": 2}]},
            entries=[("counter.visits", "counter", "visits", None)],
        )
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: view)
        (rec,) = wsapi._do_helpers_list(hass, {})["helpers"]
        assert rec["entity_id"] == "counter.visits"
        assert rec["name"] == "Visits"
        assert rec["config"]["step"] == 2

    def test_helper_outside_storage_has_no_storage_id(self) -> None:
        # A YAML-defined helper is not in <type>/list; its record says so with a
        # null storage id, so the server can leave it out of a storage listing.
        hass = FakeHass(
            states=[FakeState("input_boolean.legacy", "off", friendly_name="Legacy")],
            data=stored_helpers({"input_boolean": []}),
        )
        (rec,) = wsapi._do_helpers_list(hass, {})["helpers"]
        assert rec["entity_id"] == "input_boolean.legacy"
        assert rec["storage_id"] is None

    def test_type_with_a_helper_the_registry_cannot_place_is_left_to_its_list(
        self,
    ) -> None:
        # Without its registry entry a stored helper cannot be told apart from
        # its state, so listing both would show it twice; <type>/list lists it.
        hass = FakeHass(
            states=[FakeState("counter.visits", "1", friendly_name="Visits")],
            data=stored_helpers({"counter": [{"id": "visits", "name": "Visits"}]}),
        )
        res = wsapi._do_helpers_list(hass, {"helper_types": ["counter"]})
        assert res["helpers"] == []
        assert "counter" not in res["covered_types"]

    def test_flow_helper_options_and_entity_data_never_leaks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_flow_helper_options_secret_scrubbed(self) -> None:
        # Flow-helper options share config_entries' exposure class, so they pass
        # through the SAME secret scrub — a resolved !secret is redacted here too.
        secret = "s3cr3thelperopt"
        entry = FakeConfigEntry(
            "template",
            title="Tmpl",
            options={"token": secret, "keep": "kept"},
            entry_id="e1",
        )
        res = wsapi._do_helpers_list(
            FakeHass(config_entries=[entry]), {}, secret_values=frozenset({secret})
        )
        rec = next(h for h in res["helpers"] if h["kind"] == "flow")
        assert rec["options"] == {"token": "**redacted**", "keep": "kept"}
        assert secret not in json.dumps(res)

    def test_flow_helper_scrub_off_by_default(self) -> None:
        # No secret_values (default) is a no-op — options pass through verbatim.
        entry = FakeConfigEntry(
            "template", title="Tmpl", options={"token": "plain"}, entry_id="e1"
        )
        res = wsapi._do_helpers_list(FakeHass(config_entries=[entry]), {})
        rec = next(h for h in res["helpers"] if h["kind"] == "flow")
        assert rec["options"] == {"token": "plain"}
        assert "secret_scrub_degraded" not in res

    def test_helpers_list_degraded_signalled_when_flow_included(self) -> None:
        entry = FakeConfigEntry(
            "template", title="Tmpl", options={"token": "x"}, entry_id="e1"
        )
        res = wsapi._do_helpers_list(
            FakeHass(config_entries=[entry]), {}, secret_scrub_degraded=True
        )
        assert res["secret_scrub_degraded"] is True

    def test_helpers_list_no_degraded_signal_when_flow_excluded(self) -> None:
        # The signal only rides the flow path; it is suppressed when flow helpers
        # are excluded (the scrubbed surface isn't emitted).
        res = wsapi._do_helpers_list(
            FakeHass(),
            {"include_flow_helpers": False},
            secret_scrub_degraded=True,
        )
        assert "secret_scrub_degraded" not in res

    @pytest.mark.asyncio
    async def test_helpers_list_prep_skips_read_without_flow(
        self, monkeypatch, tmp_path
    ):
        # Perf gate: no secrets.yaml read when flow helpers are excluded.
        calls = {"n": 0}

        async def _spy(func, *args):
            calls["n"] += 1
            return func(*args)

        h = FakeHass()
        h.config = FakeConfig(tmp_path)
        monkeypatch.setattr(h, "async_add_executor_job", _spy)
        extra = await wsapi._helpers_list_prep(h, {"include_flow_helpers": False})
        assert extra == {"secret_values": frozenset(), "secret_scrub_degraded": False}
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_helpers_list_prep_loads_secrets_with_flow(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "secrets.yaml").write_text(
            "api_password: sekret\n", encoding="utf-8"
        )
        h = FakeHass()
        h.config = FakeConfig(tmp_path)
        extra = await wsapi._helpers_list_prep(h, {"type": wsapi.WS_HELPERS_LIST})
        assert extra["secret_values"] == frozenset({"sekret"})
        assert extra["secret_scrub_degraded"] is False

    def test_flow_helper_without_registered_entity(self) -> None:
        entry = FakeConfigEntry(
            "group", title="Living Room Group", options={"entities": []}, entry_id="e9"
        )
        res = wsapi._do_helpers_list(FakeHass(config_entries=[entry]), {})
        rec = next(h for h in res["helpers"] if h["kind"] == "flow")
        assert rec["entity_id"] is None
        assert rec["name"] == "Living Room Group"

    def test_helper_types_filter(self) -> None:
        states = [FakeState("input_boolean.guest", "off", "Guest")]
        entry = FakeConfigEntry(
            "template", title="Tmpl", options={"state": "x"}, entry_id="e1"
        )
        h = FakeHass(
            states=states,
            config_entries=[entry],
            data=stored_helpers({"input_boolean": []}),
        )
        only_tmpl = wsapi._do_helpers_list(h, {"helper_types": ["template"]})
        assert {r["helper_type"] for r in only_tmpl["helpers"]} == {"template"}
        only_bool = wsapi._do_helpers_list(h, {"helper_types": ["input_boolean"]})
        assert {r["helper_type"] for r in only_bool["helpers"]} == {"input_boolean"}

    def test_include_flow_helpers_false(self) -> None:
        entry = FakeConfigEntry(
            "template", title="Tmpl", options={"state": "x"}, entry_id="e1"
        )
        res = wsapi._do_helpers_list(
            FakeHass(config_entries=[entry]), {"include_flow_helpers": False}
        )
        assert [r for r in res["helpers"] if r["kind"] == "flow"] == []

    def test_home_zone_is_listed_but_search_unaffected(self) -> None:
        # ha_get_zone lists the home zone, which is outside the zone storage;
        # search's helper surface leaves zones out.
        states = [FakeState("zone.home", "zoning", friendly_name="Home", radius=1)]
        home = {"name": "Home", "latitude": 1.0, "longitude": 2.0, "radius": 100}
        hass = FakeHass(
            states=states,
            data={
                **stored_helpers({"zone": []}),
                "entity_components": {
                    "zone": FakeComponent([FakeCollectionEntity("zone.home", home)])
                },
            },
        )
        (rec,) = wsapi._do_helpers_list(hass, {})["helpers"]
        assert rec["entity_id"] == "zone.home"
        assert rec["storage_id"] is None
        assert rec["config"] == home
        searched = wsapi._do_search(hass, {"query": "home", "search_types": ["helper"]})
        assert searched["helpers"] == []

    def test_covered_types_names_only_types_read_like_their_list_command(self) -> None:
        # covered_types tells the server which types it may trust an empty list
        # for. person/list adds YAML persons and tag/list adds registry names, so
        # those two, and a type whose storage did not load, are left to the
        # server's own <type>/list.
        stock = {
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
        }
        loaded = {helper_type: [] for helper_type in stock | {"person", "tag"}}
        del loaded["timer"]
        covered = set(
            wsapi._do_helpers_list(FakeHass(data=stored_helpers(loaded)), {})[
                "covered_types"
            ]
        )
        assert stock - {"timer"} <= covered
        assert covered.isdisjoint({"timer", "person", "tag"})
        assert {"template", "group", "utility_meter"} <= covered

    def test_covered_types_excludes_flow_when_disabled(self) -> None:
        res = wsapi._do_helpers_list(
            FakeHass(data=stored_helpers({"input_boolean": []})),
            {"include_flow_helpers": False},
        )
        covered = set(res["covered_types"])
        assert "input_boolean" in covered
        # No flow types are covered when flow enumeration is turned off.
        assert covered.isdisjoint(wsapi.FLOW_HELPER_DOMAINS)
