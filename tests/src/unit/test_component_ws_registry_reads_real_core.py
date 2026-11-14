"""The component's config-entry and registry reads on a real ``hass``.

Config entries are ``MockConfigEntry`` rows, registry entries are Core's, and
``secrets.yaml`` sits in Core's own config directory. The commands run through
Core's WebSocket API (``component_ws``); the registry rows are compared with
the rows Core's own ``config/*_registry/list`` commands send.
"""

from __future__ import annotations

import json
import logging
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.json import json_bytes, json_dumps
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.websocket_api.constants import (
    WS_CONFIG_ENTRIES,
    WS_ENTITY_LOOKUP,
    WS_REGISTRIES,
    WS_REGISTRY_LOOKUP,
    WS_SYSTEM_SNAPSHOT,
)

from ._component_core import ws_call, ws_result
from ._core_setup import write_secrets
from ._executor_spy import executor_jobs, jobs_from

_SECRET = "s3cr3toptionvalue"
_COMPONENT_LOGGER = "custom_components.ha_mcp_tools.websocket_api"


def _as_sent(value: Any) -> Any:
    """``value`` as Core's JSON encoder puts it on the wire."""
    return json.loads(json_dumps(value))


def _mosquitto(
    hass: HomeAssistant,
    *,
    domain: str = "mqtt",
    entry_id: str = "cfg1",
    options: Any = None,
) -> MockConfigEntry:
    """An entry with credentials in ``data`` and in its subentry's data."""
    entry = MockConfigEntry(
        domain=domain,
        entry_id=entry_id,
        title="Mosquitto",
        unique_id="mqtt-unique-1",
        options={"discovery": True} if options is None else options,
        data={"password": "DATA_SECRET_XYZ"},
        subentries_data=[
            {
                "data": {"k": "SUBSECRET"},
                "subentry_id": "sub1",
                "subentry_type": "device",
                "title": "Sub One",
                "unique_id": "u1",
            }
        ],
    )
    entry.add_to_hass(hass)
    return entry


async def _entries(ws: MockHAClientWebSocket, **params: Any) -> Any:
    return (await ws_result(ws, WS_CONFIG_ENTRIES, **params))["entries"]


# --- config_entries ---------------------------------------------------------
async def test_a_config_entry_row_is_cores_row_plus_three_fields(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Every field Core's ``config_entries/get`` sends, unchanged, plus the
    ``unique_id`` Core withholds, ``options`` and the subentry identities."""
    entry = _mosquitto(hass)

    (row,) = await _entries(component_ws, domain="mqtt")

    assert row == {
        **json.loads(json_bytes(entry.as_json_fragment)),
        "unique_id": "mqtt-unique-1",
        "options": {"discovery": True},
        "subentries": [
            {
                "subentry_id": "sub1",
                "subentry_type": "device",
                "title": "Sub One",
                "unique_id": "u1",
            }
        ],
    }


async def test_entry_and_subentry_data_are_never_sent(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    _mosquitto(hass)

    sent = json.dumps(await ws_result(component_ws, WS_CONFIG_ENTRIES))

    assert "DATA_SECRET_XYZ" not in sent
    assert "SUBSECRET" not in sent


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"domain": "mqtt"}, ["c1"]),
        ({"entry_id": "c2"}, ["c2"]),
        ({"entry_id": "ghost"}, []),
        # An empty entry_id is a lookup that misses, not a request to list.
        ({"entry_id": ""}, []),
        ({}, ["c1", "c2"]),
    ],
)
async def test_config_entries_are_selected_by_domain_or_id(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    params: dict[str, Any],
    expected: list[str],
) -> None:
    _mosquitto(hass, entry_id="c1")
    _mosquitto(hass, domain="zwave_js", entry_id="c2")

    rows = await _entries(component_ws, **params)

    assert sorted(row["entry_id"] for row in rows) == expected


async def test_an_entry_without_subentries_lists_none(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    MockConfigEntry(domain="mqtt").add_to_hass(hass)

    (row,) = await _entries(component_ws)

    assert row["subentries"] == []


async def test_options_holding_a_secret_are_redacted_at_any_depth(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    write_secrets(hass, f"api_password: {_SECRET}\n")
    _mosquitto(
        hass,
        options={
            "top": _SECRET,
            "nested": {"inner": _SECRET, "keep": "kept"},
            "list": [_SECRET, "also_kept"],
        },
    )

    result = await ws_result(component_ws, WS_CONFIG_ENTRIES)

    assert result["entries"][0]["options"] == {
        "top": "**redacted**",
        "nested": {"inner": "**redacted**", "keep": "kept"},
        "list": ["**redacted**", "also_kept"],
    }
    assert _SECRET not in json.dumps(result)


async def test_options_inside_a_nested_mapping_proxy_are_redacted(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Stringified past the scrub, the secret would leave inside the repr."""
    write_secrets(hass, f"api_password: {_SECRET}\n")
    _mosquitto(
        hass,
        options={"outer": MappingProxyType({"token": _SECRET, "keep": "kept"})},
    )

    result = await ws_result(component_ws, WS_CONFIG_ENTRIES)

    assert result["entries"][0]["options"] == {
        "outer": {"token": "**redacted**", "keep": "kept"}
    }
    assert _SECRET not in json.dumps(result)


async def test_an_option_holding_a_numeric_secret_is_redacted(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """``alarm_code: 1234`` is a YAML int; an option can carry it back as one."""
    write_secrets(hass, "alarm_code: 1234\n")
    _mosquitto(hass, options={"alarm_code": 1234, "port": 8123})

    (row,) = await _entries(component_ws)

    assert row["options"] == {"alarm_code": "**redacted**", "port": 8123}


async def test_every_string_and_number_in_secrets_yaml_is_scrubbed_but_booleans(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """``True`` is never a credential, and redacting it would over-redact."""
    write_secrets(
        hass, f"port: 8123\napi_password: {_SECRET}\nother: value2\nflag: true\n"
    )
    _mosquitto(
        hass,
        options={"a": "8123", "b": _SECRET, "c": "value2", "d": "True", "e": "kept"},
    )

    (row,) = await _entries(component_ws)

    assert row["options"] == {
        "a": "**redacted**",
        "b": "**redacted**",
        "c": "**redacted**",
        "d": "True",
        "e": "kept",
    }


async def test_without_secrets_yaml_options_are_sent_silently_as_they_are(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Most installs have no secrets.yaml: nothing to scrub, nothing degraded."""
    _mosquitto(hass, options={"token": _SECRET})

    with caplog.at_level(logging.WARNING, logger=_COMPONENT_LOGGER):
        result = await ws_result(component_ws, WS_CONFIG_ENTRIES)

    assert result["entries"][0]["options"] == {"token": _SECRET}
    assert "secret_scrub_degraded" not in result
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_an_unreadable_secrets_yaml_is_signalled_as_degraded(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Unredacted options must not pass for redacted ones."""
    write_secrets(hass, "{bad: yaml: [")
    _mosquitto(hass, options={"token": "unredacted"})

    result = await ws_result(component_ws, WS_CONFIG_ENTRIES)

    assert result["secret_scrub_degraded"] is True
    assert result["entries"][0]["options"] == {"token": "unredacted"}


async def test_an_unreadable_secrets_yaml_warns_once(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    caplog: pytest.LogCaptureFixture,
) -> None:
    write_secrets(hass, "{not: valid: yaml: [")
    _mosquitto(hass)

    with caplog.at_level(logging.WARNING, logger=_COMPONENT_LOGGER):
        await ws_result(component_ws, WS_CONFIG_ENTRIES)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


async def test_config_entries_read_secrets_yaml_off_the_event_loop(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    write_secrets(hass, "api_password: sekret\n")
    _mosquitto(hass, options={"token": "sekret"})

    with executor_jobs(hass) as spy:
        (row,) = await _entries(component_ws)

    assert jobs_from(spy, "secrets") == 1
    assert row["options"] == {"token": "**redacted**"}


# --- registry_lookup --------------------------------------------------------
@pytest.fixture
def meters(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> None:
    """Three entities of config entry ``cfg1`` (a utility meter and its
    tariffs, one disabled) and one of ``cfg2``."""
    meter = MockConfigEntry(domain="utility_meter", entry_id="cfg1")
    other = MockConfigEntry(domain="test", entry_id="cfg2")
    for entry in (meter, other):
        entry.add_to_hass(hass)
    for object_id, disabled in (
        ("meter", None),
        ("meter_peak", None),
        ("meter_offpeak", er.RegistryEntryDisabler.USER),
    ):
        entity_registry.async_get_or_create(
            "sensor",
            "utility_meter",
            object_id,
            suggested_object_id=object_id,
            config_entry=meter,
            disabled_by=disabled,
        )
    entity_registry.async_get_or_create(
        "light", "test", "other", suggested_object_id="other", config_entry=other
    )


async def test_lookup_by_config_entry_returns_all_of_its_entities(
    entity_registry: er.EntityRegistry,
    component_ws: MockHAClientWebSocket,
    meters: None,
) -> None:
    """Every entity of the entry, the disabled one too, as Core's
    ``config/entity_registry/list`` row."""
    result = await ws_result(component_ws, WS_REGISTRY_LOOKUP, config_entry_id="cfg1")

    rows = {row["entity_id"]: row for row in result["entities"]}
    assert set(rows) == {"sensor.meter", "sensor.meter_peak", "sensor.meter_offpeak"}
    for entity_id, row in rows.items():
        entry = entity_registry.async_get(entity_id)
        assert entry is not None
        assert row == _as_sent(entry.as_partial_dict)
    assert rows["sensor.meter_offpeak"]["disabled_by"] == "user"
    assert "missing" not in result


async def test_lookup_by_ids_splits_found_from_missing(
    component_ws: MockHAClientWebSocket, meters: None
) -> None:
    result = await ws_result(
        component_ws, WS_REGISTRY_LOOKUP, entity_ids=["sensor.meter", "sensor.ghost"]
    )

    assert [row["entity_id"] for row in result["entities"]] == ["sensor.meter"]
    assert result["missing"] == ["sensor.ghost"]


@pytest.mark.parametrize("params", [{}, {"entity_ids": []}])
async def test_a_lookup_without_a_target_is_an_error(
    component_ws: MockHAClientWebSocket, meters: None, params: dict[str, Any]
) -> None:
    """An empty result would read as "no matches" (#1813 M2)."""
    reply = await ws_call(component_ws, WS_REGISTRY_LOOKUP, **params)

    assert reply["success"] is False


# --- system_snapshot --------------------------------------------------------
@pytest.fixture
def health(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> er.RegistryEntry:
    MockConfigEntry(
        domain="mqtt",
        title="Mosquitto",
        entry_id="cfg1",
        state=ConfigEntryState.LOADED,
        options={"discovery": True},
        data={"password": "DATA_SECRET_XYZ"},
    ).add_to_hass(hass)
    ir.async_create_issue(
        hass,
        "mqtt",
        "iss1",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="iss1",
    )
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Lamp"})
    return entity_registry.async_get_or_create(
        "light", "test", "lamp", suggested_object_id="lamp"
    )


async def test_the_snapshot_reads_every_health_slice_in_one_pass(
    component_ws: MockHAClientWebSocket, health: er.RegistryEntry
) -> None:
    result = await ws_result(component_ws, WS_SYSTEM_SNAPSHOT)

    # Identity only: the health view has no use for options or subentries.
    assert result["config_entries"] == [
        {
            "entry_id": "cfg1",
            "domain": "mqtt",
            "title": "Mosquitto",
            "state": "loaded",
            "source": "user",
            "disabled_by": None,
        }
    ]
    assert "DATA_SECRET_XYZ" not in json.dumps(result)
    issue = next(i for i in result["issues"] if i["issue_id"] == "iss1")
    assert issue["severity"] == "warning"
    assert result["entities"] == [_as_sent(health.as_partial_dict)]
    (state,) = result["states"]
    assert (state["entity_id"], state["attributes"]["friendly_name"]) == (
        "light.lamp",
        "Lamp",
    )


async def test_the_snapshot_leaves_out_the_slices_turned_off(
    component_ws: MockHAClientWebSocket, health: er.RegistryEntry
) -> None:
    result = await ws_result(
        component_ws,
        WS_SYSTEM_SNAPSHOT,
        include_states=False,
        include_config_entries=False,
    )

    assert (result["states"], result["config_entries"]) == ([], [])
    assert result["entities"] and result["issues"]


# --- entity_lookup ----------------------------------------------------------
@pytest.fixture
def shared_unique_id(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> None:
    """``shared`` is the unique_id of a zha sensor and an mqtt binary sensor."""
    zha = MockConfigEntry(domain="zha", entry_id="cfg1")
    mqtt = MockConfigEntry(domain="mqtt", entry_id="cfg2")
    for entry in (zha, mqtt):
        entry.add_to_hass(hass)
    sensor = entity_registry.async_get_or_create(
        "sensor",
        "zha",
        "shared",
        suggested_object_id="a",
        config_entry=zha,
        disabled_by=er.RegistryEntryDisabler.USER,
        hidden_by=er.RegistryEntryHider.INTEGRATION,
    )
    entity_registry.async_update_entity(
        sensor.entity_id, categories={"automation": "cat1"}
    )
    entity_registry.async_get_or_create(
        "binary_sensor", "mqtt", "shared", suggested_object_id="b", config_entry=mqtt
    )
    entity_registry.async_get_or_create(
        "light", "zha", "other", suggested_object_id="c"
    )


async def _matches(ws: MockHAClientWebSocket, **params: Any) -> Any:
    return (await ws_result(ws, WS_ENTITY_LOOKUP, **params))["matches"]


async def test_every_entity_with_the_unique_id_is_returned(
    component_ws: MockHAClientWebSocket, shared_unique_id: None
) -> None:
    matches = await _matches(component_ws, unique_id="shared")

    assert {m["entity_id"] for m in matches} == {"sensor.a", "binary_sensor.b"}
    assert next(m for m in matches if m["entity_id"] == "sensor.a") == {
        "entity_id": "sensor.a",
        "unique_id": "shared",
        "platform": "zha",
        "domain": "sensor",
        "config_entry_id": "cfg1",
        "categories": {"automation": "cat1"},
        "disabled_by": "user",
        "hidden_by": "integration",
    }


@pytest.mark.parametrize(
    ("narrowing", "expected"),
    [
        ({"platform": "zha"}, ["sensor.a"]),
        ({"domain": "binary_sensor"}, ["binary_sensor.b"]),
    ],
)
async def test_platform_or_domain_narrows_the_matches(
    component_ws: MockHAClientWebSocket,
    shared_unique_id: None,
    narrowing: dict[str, str],
    expected: list[str],
) -> None:
    matches = await _matches(component_ws, unique_id="shared", **narrowing)

    assert [m["entity_id"] for m in matches] == expected


async def test_an_unmatched_unique_id_has_no_matches(
    component_ws: MockHAClientWebSocket, shared_unique_id: None
) -> None:
    assert await _matches(component_ws, unique_id="nope") == []


async def test_an_unavailable_entity_registry_is_an_error_not_no_match(
    component_ws: MockHAClientWebSocket, shared_unique_id: None
) -> None:
    """The server falls back to its own scan instead of trusting an empty
    answer it cannot tell from "no such entry"."""
    with patch.object(er, "async_get", side_effect=RuntimeError("core drift")):
        reply = await ws_call(component_ws, WS_ENTITY_LOOKUP, unique_id="x")

    assert reply["success"] is False


# --- registries -------------------------------------------------------------
async def _core_rows(ws: MockHAClientWebSocket, command: str, **params: Any) -> Any:
    return await ws_result(ws, f"config/{command}_registry/list", **params)


async def test_area_rows_are_cores_area_list_rows(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    assert await async_setup_component(hass, "config", {})
    floor = floor_registry.async_create("Upstairs")
    label = label_registry.async_create("Favorites")
    # Core takes only a humidity and a temperature sensor it has a state for.
    hass.states.async_set("sensor.h", "40", {"device_class": "humidity"})
    hass.states.async_set("sensor.t", "21", {"device_class": "temperature"})
    area_registry.async_create(
        "Office",
        floor_id=floor.floor_id,
        aliases={"studio"},
        icon="mdi:home",
        picture="/pic.png",
        labels={label.label_id},
        humidity_entity_id="sensor.h",
        temperature_entity_id="sensor.t",
    )

    result = await ws_result(component_ws, WS_REGISTRIES, registries=["area"])

    assert list(result) == ["areas"]
    assert result["areas"] == await _core_rows(component_ws, "area")
    (row,) = result["areas"]
    assert isinstance(row["created_at"], float)
    assert isinstance(row["modified_at"], float)


async def test_floor_rows_are_cores_floor_list_rows(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    floor_registry: fr.FloorRegistry,
) -> None:
    """Core's floors have no labels, so the row must not invent them."""
    assert await async_setup_component(hass, "config", {})
    floor_registry.async_create("Upstairs", level=2, icon="mdi:stairs", aliases={"top"})

    result = await ws_result(component_ws, WS_REGISTRIES, registries=["floor"])

    assert result["floors"] == await _core_rows(component_ws, "floor")
    assert "labels" not in result["floors"][0]


async def test_label_rows_are_cores_label_list_rows(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    label_registry: lr.LabelRegistry,
) -> None:
    assert await async_setup_component(hass, "config", {})
    label_registry.async_create(
        "Favorites", color="red", description="fav", icon="mdi:star"
    )

    result = await ws_result(component_ws, WS_REGISTRIES, registries=["label"])

    assert result["labels"] == await _core_rows(component_ws, "label")


async def test_category_rows_are_cores_rows_for_each_scope(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    category_registry: cr.CategoryRegistry,
) -> None:
    """A scope without categories is an empty list, not a missing key."""
    assert await async_setup_component(hass, "config", {})
    category_registry.async_create(name="Lights", scope="automation", icon="mdi:bulb")

    result = await ws_result(
        component_ws,
        WS_REGISTRIES,
        registries=["category"],
        category_scopes=["automation", "script"],
    )

    core = await _core_rows(component_ws, "category", scope="automation")
    assert result["categories"] == {"automation": core, "script": []}


async def test_only_the_requested_registries_are_sent(
    component_ws: MockHAClientWebSocket,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    area_registry.async_create("Office")
    floor_registry.async_create("Up")
    label_registry.async_create("Fav")

    result = await ws_result(component_ws, WS_REGISTRIES, registries=["area", "label"])

    assert set(result) == {"areas", "labels"}


@pytest.mark.parametrize("scopes", [None, []])
async def test_categories_without_a_scope_are_an_error(
    component_ws: MockHAClientWebSocket, scopes: list[str] | None
) -> None:
    """``{}`` would read as "no categories in any scope" (#1813 M1)."""
    params = {} if scopes is None else {"category_scopes": scopes}

    reply = await ws_call(
        component_ws, WS_REGISTRIES, registries=["category"], **params
    )

    assert reply["success"] is False


@pytest.mark.parametrize(
    ("registry", "kind", "params"),
    [
        (ar, "area", {}),
        (cr, "category", {"category_scopes": ["automation"]}),
    ],
)
async def test_an_unavailable_registry_is_an_error_not_empty(
    component_ws: MockHAClientWebSocket,
    registry: Any,
    kind: str,
    params: dict[str, Any],
) -> None:
    """An empty list would let the backup capture read every row as missing
    and skip it (review-5 M-6); the server falls back to Core's list."""
    with patch.object(registry, "async_get", side_effect=RuntimeError("core drift")):
        reply = await ws_call(component_ws, WS_REGISTRIES, registries=[kind], **params)

    assert reply["success"] is False
