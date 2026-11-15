"""The component's read commands on a real ``hass``: states, blueprints,
overview, entity enrichment and Assist exposure.

Every state, registry row, issue, notification, exposure setting and
configuration value is Core's own; the commands run through Core's WebSocket
API (``component_ws``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.components.homeassistant import exposed_entities
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant, State
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.json import json_dumps
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.websocket_api.constants import (
    WS_BLUEPRINT_GET,
    WS_ENTITY_ENRICH,
    WS_EXPOSURE,
    WS_OVERVIEW,
    WS_SEARCH,
    WS_STATES,
)

from ._component_core import ws_call, ws_result
from ._core_setup import write_secrets
from ._executor_spy import executor_jobs, jobs_from


def _as_sent(value: Any) -> Any:
    """``value`` as Core's JSON encoder puts it on the wire."""
    return json.loads(json_dumps(value))


# --- states -----------------------------------------------------------------
async def test_states_splits_found_from_missing(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.a", "on", {"friendly_name": "A"})
    hass.states.async_set("sensor.b", "21", {"friendly_name": "B"})

    result = await ws_result(
        component_ws, WS_STATES, entity_ids=["light.a", "sensor.b", "light.ghost"]
    )

    assert set(result["states"]) == {"light.a", "sensor.b"}
    assert result["missing"] == ["light.ghost"]


async def test_a_state_is_the_one_rest_sends(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The body is Core's ``State.as_dict()`` unmodified, timestamps included,
    so it matches the REST ``/api/states/<id>`` read byte for byte."""
    hass.states.async_set("light.a", "on", {"friendly_name": "A", "brightness": 128})
    state = hass.states.get("light.a")
    assert state is not None

    result = await ws_result(component_ws, WS_STATES, entity_ids=["light.a"])

    assert result["states"]["light.a"] == _as_sent(state.as_dict())


async def test_states_for_no_ids_is_empty(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.a", "on")

    result = await ws_result(component_ws, WS_STATES, entity_ids=[])

    assert result == {"states": {}, "missing": []}


async def test_a_repeated_id_is_returned_once(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.a", "on")

    result = await ws_result(component_ws, WS_STATES, entity_ids=["light.a", "light.a"])

    assert list(result["states"]) == ["light.a"]
    assert result["missing"] == []


async def test_a_state_core_cannot_serialize_is_reported_missing(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A null body would be indistinguishable from a real value (#1813 F5)."""
    hass.states.async_set("sensor.x", "1")
    real = State.as_dict

    def as_dict(state: State) -> Any:
        return None if state.entity_id == "sensor.x" else real(state)

    with patch.object(State, "as_dict", as_dict):
        result = await ws_result(component_ws, WS_STATES, entity_ids=["sensor.x"])

    assert result == {"states": {}, "missing": ["sensor.x"]}


# --- blueprint_get ----------------------------------------------------------
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


def _write_blueprint(hass: HomeAssistant, domain: str, path: str, text: str) -> None:
    target = Path(hass.config.path("blueprints", domain, path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


async def _blueprint(
    ws: MockHAClientWebSocket, path: str, domain: str = "automation"
) -> Any:
    return await ws_result(ws, WS_BLUEPRINT_GET, domain=domain, path=path)


async def test_blueprint_get_returns_the_body_blueprint_list_lacks(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    _write_blueprint(hass, "automation", "user/motion.yaml", _MOTION_LIGHT)

    result = await _blueprint(component_ws, "user/motion.yaml")

    assert result["metadata"]["name"] == "Motion Light"
    assert result["config"]["blueprint"]["domain"] == "automation"
    assert result["config"]["trigger"][0]["platform"] == "state"
    assert result["config"]["action"][0]["service"] == "light.turn_on"


async def test_blueprint_get_returns_the_file_text_byte_for_byte(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The server hands ``yaml`` straight back to ``blueprint/save``, so a
    round trip keeps comments, key order and the ``!input`` tags."""
    _write_blueprint(hass, "automation", "user/motion.yaml", _MOTION_LIGHT)

    result = await _blueprint(component_ws, "user/motion.yaml")

    assert result["yaml"] == _MOTION_LIGHT


async def test_an_unparseable_blueprint_keeps_its_text(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Without the text a caller could not see, or repair, the file Home
    Assistant refuses to load."""
    _write_blueprint(hass, "automation", "broken.yaml", "blueprint: [unclosed\n")

    result = await _blueprint(component_ws, "broken.yaml")

    assert result == {
        "metadata": None,
        "config": None,
        "yaml": "blueprint: [unclosed\n",
    }


async def test_an_input_tag_is_kept_as_a_marker(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    _write_blueprint(hass, "automation", "user/motion.yaml", _MOTION_LIGHT)

    config = (await _blueprint(component_ws, "user/motion.yaml"))["config"]

    assert config["trigger"][0]["entity_id"] == {"__input__": "motion_sensor"}
    assert config["action"][0]["entity_id"] == {"__input__": "target_light"}


async def test_a_secret_tag_is_never_resolved(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    write_secrets(hass, "my_api_token: hunter2\n")
    _write_blueprint(
        hass,
        "automation",
        "sneaky.yaml",
        "blueprint:\n"
        "  name: Has Secret\n"
        "  domain: automation\n"
        "action:\n"
        "  - service: notify.notify\n"
        "    data:\n"
        "      token: !secret my_api_token\n",
    )

    result = await _blueprint(component_ws, "sneaky.yaml")

    assert result["config"]["action"][0]["data"] == {"token": None}
    assert "my_api_token" not in json.dumps(result["config"])
    assert "hunter2" not in json.dumps(result)


@pytest.mark.parametrize(
    "path",
    [
        "../../secrets.yaml",
        "../../../etc/passwd",
        "/etc/passwd",
        "user/../../escape.yaml",
    ],
)
async def test_a_path_out_of_the_blueprint_folder_is_never_read(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, path: str
) -> None:
    """Neither the parsed body nor the raw text of a file outside the jail."""
    write_secrets(hass, "db_pw: hunter2\n")

    result = await _blueprint(component_ws, path)

    assert result == {"metadata": None, "config": None, "yaml": None}


async def test_a_missing_blueprint_returns_nothing(
    component_ws: MockHAClientWebSocket,
) -> None:
    result = await _blueprint(component_ws, "nope.yaml")

    assert result == {"metadata": None, "config": None, "yaml": None}


async def test_the_blueprint_file_is_read_off_the_event_loop(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """One read in the executor serves both the parsed body and the text."""
    source = "blueprint:\n  name: S\n  domain: script\nsequence:\n  - delay: 1\n"
    _write_blueprint(hass, "script", "user/s.yaml", source)

    with executor_jobs(hass) as spy:
        result = await _blueprint(component_ws, "user/s.yaml", domain="script")

    assert jobs_from(spy, "lookups") == 1
    assert result["metadata"]["name"] == "S"
    assert result["config"]["sequence"] == [{"delay": 1}]
    assert result["yaml"] == source


# --- overview ---------------------------------------------------------------
@pytest.fixture
async def home(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
    tmp_path: Path,
) -> dict[str, Any]:
    """A lamp in the office on its device, a service domain, a notification,
    a repair and an allowlisted folder, all Core's own."""
    await async_process_ha_core_config(
        hass, {"allowlist_external_dirs": [str(tmp_path)]}
    )
    assert await async_setup_component(hass, "input_boolean", {})
    assert await async_setup_component(hass, "persistent_notification", {})
    floor = floor_registry.async_create("Ground floor")
    office = area_registry.async_create("Office", floor_id=floor.floor_id)
    label = label_registry.async_create("Lighting")
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "lamp")},
        name="Lamp Device",
    )
    device_registry.async_update_device(device.id, area_id=office.id)
    lamp = entity_registry.async_get_or_create(
        "light",
        "test",
        "lamp",
        suggested_object_id="lamp",
        device_id=device.id,
        original_name="Lamp",
        entity_category=EntityCategory.CONFIG,
        hidden_by=er.RegistryEntryHider.USER,
    )
    entity_registry.async_update_entity(
        lamp.entity_id, area_id=office.id, labels={label.label_id}
    )
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Lamp"})
    persistent_notification.async_create(
        hass, "Something", title="Heads up", notification_id="n1"
    )
    ir.async_create_issue(
        hass,
        "demo",
        "active_issue",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="demo_issue",
    )
    return {"device": device.id, "office": office.id, "floor": floor.floor_id}


async def test_overview_returns_each_raw_slice_the_server_reads(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, home: dict[str, Any]
) -> None:
    result = await ws_result(component_ws, WS_OVERVIEW)

    state = next(s for s in result["states"] if s["entity_id"] == "light.lamp")
    assert (state["state"], state["attributes"]["friendly_name"]) == ("on", "Lamp")
    names = hass.services.async_services()["input_boolean"]
    assert {
        "domain": "input_boolean",
        "services": {name: {} for name in names},
    } in result["services"]
    (entity,) = result["entity_registry"]
    assert (entity["entity_id"], entity["area_id"]) == ("light.lamp", home["office"])
    assert entity["device_id"] == home["device"]
    assert len(entity["labels"]) == 1
    # Core's enums are sent as their wire strings.
    assert (entity["entity_category"], entity["hidden_by"]) == ("config", "user")
    assert result["device_registry"] == [
        {
            "id": home["device"],
            "area_id": home["office"],
            "labels": [],
            "name": "Lamp Device",
            "name_by_user": None,
            "manufacturer": None,
            "model": None,
        }
    ]
    assert result["area_registry"] == [
        {"area_id": home["office"], "name": "Office", "floor_id": home["floor"]}
    ]
    # Home Assistant's own config fields; the server supplies base_url.
    assert result["config"]["version"] == __version__
    assert result["config"]["location_name"] == hass.config.location_name
    assert "base_url" not in result["config"]
    allowlist = result["config"]["allowlist_external_dirs"]
    assert sorted(allowlist) == sorted(hass.config.allowlist_external_dirs)
    core_notes = await ws_result(component_ws, "persistent_notification/get")
    assert result["notifications"] == [
        {
            key: note[key]
            for key in ("notification_id", "title", "message", "created_at")
        }
        for note in core_notes
    ]
    repair = next(r for r in result["repairs"] if r["issue_id"] == "active_issue")
    assert (repair["severity"], repair["ignored"]) == ("warning", False)
    assert result["slice_errors"] == []


async def test_overview_leaves_out_the_sections_turned_off(
    component_ws: MockHAClientWebSocket, home: dict[str, Any]
) -> None:
    result = await ws_result(
        component_ws,
        WS_OVERVIEW,
        include_notifications=False,
        include_repairs=False,
    )

    assert (result["notifications"], result["repairs"]) == ([], [])
    assert result["states"] and result["entity_registry"]


async def test_an_unavailable_registry_is_an_empty_slice_not_an_error(
    component_ws: MockHAClientWebSocket, home: dict[str, Any]
) -> None:
    """A registry Core does not hand out is "nothing here"; ``slice_errors``
    is only for a read that failed."""
    with (
        patch.object(er, "async_get", side_effect=RuntimeError("gone")),
        patch.object(dr, "async_get", side_effect=RuntimeError("gone")),
        patch.object(ar, "async_get", side_effect=RuntimeError("gone")),
    ):
        result = await ws_result(component_ws, WS_OVERVIEW)

    assert result["entity_registry"] == []
    assert result["device_registry"] == []
    assert result["area_registry"] == []
    assert result["slice_errors"] == []


async def test_a_failed_slice_is_named_and_the_others_still_served(
    component_ws: MockHAClientWebSocket, home: dict[str, Any]
) -> None:
    """The server can tell "empty" from "failed" and read just that slice
    the legacy way."""
    with patch.object(
        er, "async_get_full_entity_name", side_effect=RuntimeError("blew up")
    ):
        result = await ws_result(component_ws, WS_OVERVIEW)

    assert result["slice_errors"] == ["entity_registry"]
    assert result["entity_registry"] == []
    assert result["states"], "an unrelated slice must still be populated"


# --- entity_enrich ----------------------------------------------------------
@pytest.fixture
def enrich_home(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """``light.lamp`` placed and labeled itself; ``switch.plug`` only through
    its device."""
    upstairs = floor_registry.async_create("Upstairs")
    downstairs = floor_registry.async_create("Downstairs")
    office = area_registry.async_create("Office", floor_id=upstairs.floor_id)
    garage = area_registry.async_create("Garage", floor_id=downstairs.floor_id)
    favorites = label_registry.async_create("Favorites")
    auto = label_registry.async_create("Auto")
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("test", "plug")}
    )
    device_registry.async_update_device(
        device.id, area_id=garage.id, labels={auto.label_id}
    )
    lamp = entity_registry.async_get_or_create(
        "light", "test", "lamp", suggested_object_id="lamp", original_name="Lamp"
    )
    entity_registry.async_update_entity(
        lamp.entity_id,
        aliases=["reading light"],
        area_id=office.id,
        labels={favorites.label_id},
    )
    entity_registry.async_get_or_create(
        "switch", "test", "plug", suggested_object_id="plug", device_id=device.id
    )
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Lamp"})


def _full_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """The name Core composes for the entity, as the component serves it."""
    entry = er.async_get(hass).async_get(entity_id)
    return (er.async_get_full_entity_name(hass, entry) or None) if entry else None


async def _enrich(ws: MockHAClientWebSocket, *entity_ids: str) -> Any:
    return (await ws_result(ws, WS_ENTITY_ENRICH, entity_ids=list(entity_ids)))[
        "entities"
    ]


async def test_enrich_resolves_the_names_of_an_entitys_own_placement(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, enrich_home: None
) -> None:
    assert (await _enrich(component_ws, "light.lamp"))["light.lamp"] == {
        "area": "Office",
        "floor": "Upstairs",
        "labels": ["Favorites"],
        "aliases": ["reading light"],
        "full_name": _full_name(hass, "light.lamp"),
    }


async def test_enrich_inherits_area_and_labels_from_the_device(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, enrich_home: None
) -> None:
    assert (await _enrich(component_ws, "switch.plug"))["switch.plug"] == {
        "area": "Garage",
        "floor": "Downstairs",
        "labels": ["Auto"],
        "aliases": [],
        "full_name": _full_name(hass, "switch.plug"),
    }


async def test_enrich_keeps_an_unknown_id_with_empty_fields(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, enrich_home: None
) -> None:
    """The caller keys the result back to its request."""
    assert (await _enrich(component_ws, "light.ghost"))["light.ghost"] == {
        "area": None,
        "floor": None,
        "labels": [],
        "aliases": [],
        "full_name": _full_name(hass, "light.ghost"),
    }


async def test_enrich_of_no_ids_is_empty(
    component_ws: MockHAClientWebSocket, enrich_home: None
) -> None:
    assert await _enrich(component_ws) == {}


async def test_enrich_and_search_name_an_entity_alike(
    component_ws: MockHAClientWebSocket, enrich_home: None
) -> None:
    """Both read the same registry join, so they cannot drift apart."""
    enriched = (await _enrich(component_ws, "light.lamp"))["light.lamp"]
    search = await ws_result(
        component_ws, WS_SEARCH, query="light.lamp", search_types=["entity"]
    )

    (hit,) = search["entities"]
    shared = enriched.keys() - {"full_name"}
    assert {key: hit[key] for key in shared} == {key: enriched[key] for key in shared}
    # Both carry the name Core composes: search as name, enrich as full_name.
    assert hit["name"] == enriched["full_name"]


# --- exposure ---------------------------------------------------------------
@pytest.fixture
async def kitchen(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    """``light.kitchen`` and ``light.attic`` in the Kitchen on the Main floor,
    with Core's exposed-entities store set up."""
    assert await async_setup_component(hass, "homeassistant", {})
    main = floor_registry.async_create("Main")
    area = area_registry.async_create("Kitchen", floor_id=main.floor_id)
    for object_id in ("kitchen", "attic"):
        entry = entity_registry.async_get_or_create(
            "light", "test", object_id, suggested_object_id=object_id
        )
        entity_registry.async_update_entity(entry.entity_id, area_id=area.id)


async def test_exposure_reports_only_the_assistants_it_is_exposed_to(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, kitchen: None
) -> None:
    """Core's settings list every assistant with any stored option."""
    exposed_entities.async_expose_entity(hass, "conversation", "light.kitchen", True)
    exposed_entities.async_expose_entity(hass, "cloud.alexa", "light.kitchen", False)
    exposed_entities.async_set_assistant_option(
        hass, "cloud.google_assistant", "light.kitchen", "some_other_option", "x"
    )
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen Light"})

    result = await ws_result(component_ws, WS_EXPOSURE, entity_id="light.kitchen")

    assert result["exposed_entities"] == {"light.kitchen": {"conversation": True}}
    info = result["entity_info"]["light.kitchen"]
    assert (info["friendly_name"], info["domain"]) == ("Kitchen Light", "light")
    assert (info["area"], info["floor"], info["state"]) == ("Kitchen", "Main", "on")


async def test_an_unknown_entity_is_not_exposed_rather_than_an_error(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, kitchen: None
) -> None:
    """Core raises "Unknown entity" for it; the legacy list never errors on a
    junk id."""
    hass.states.async_set("light.ghost", "on", {"friendly_name": "Ghost"})

    result = await ws_result(component_ws, WS_EXPOSURE, entity_id="light.ghost")

    assert result["exposed_entities"] == {}
    assert result["entity_info"]["light.ghost"]["domain"] == "light"


async def test_an_entity_without_a_state_has_no_live_fields(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, kitchen: None
) -> None:
    exposed_entities.async_expose_entity(hass, "conversation", "light.attic", True)

    result = await ws_result(component_ws, WS_EXPOSURE, entity_id="light.attic")

    info = result["entity_info"]["light.attic"]
    assert "friendly_name" not in info
    assert "state" not in info
    assert (info["domain"], info["area"]) == ("light", "Kitchen")


@pytest.mark.parametrize(
    "error",
    [
        # Same type as Core's "Unknown entity", another fault (#1813 F4).
        HomeAssistantError("settings store read failed"),
        ValueError("Unknown entity"),
    ],
)
async def test_a_failed_settings_read_is_an_error_not_unexposed(
    component_ws: MockHAClientWebSocket, kitchen: None, error: Exception
) -> None:
    """Only Core's own "Unknown entity" means not exposed."""
    with patch.object(exposed_entities, "async_get_entity_settings", side_effect=error):
        reply = await ws_call(component_ws, WS_EXPOSURE, entity_id="light.kitchen")

    assert reply["success"] is False


async def test_exposure_list_keeps_only_exposed_entities(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, kitchen: None
) -> None:
    """Like ``homeassistant/expose_entity/list``, with each id enriched."""
    exposed_entities.async_expose_entity(hass, "conversation", "light.kitchen", True)
    exposed_entities.async_expose_entity(hass, "cloud.alexa", "light.attic", False)
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen Light"})

    result = await ws_result(component_ws, WS_EXPOSURE)

    assert result["exposed_entities"] == {"light.kitchen": {"conversation": True}}
    assert set(result["entity_info"]) == {"light.kitchen"}
