"""``ha_mcp_tools/helpers_list``: stored collection helpers + flow helpers.

Collection helpers come from Core's storage collections (what ``<type>/list``
returns), created here the way the frontend does; flow helpers from
``ConfigEntry.options``, never ``entry.data``. The command runs through Core's
WebSocket API (``component_ws``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.websocket_api.constants import (
    FLOW_HELPER_DOMAINS,
    WS_HELPERS_LIST,
    WS_SEARCH,
)

from ._component_core import ws_result
from ._core_setup import set_up_domain, write_secrets
from ._executor_spy import executor_jobs, jobs_from

_SECRET = "s3cr3thelperopt"


async def _create(
    hass: HomeAssistant, ws: MockHAClientWebSocket, domain: str, **fields: Any
) -> Any:
    """Create a stored ``domain`` helper the way the frontend does."""
    assert await async_setup_component(hass, domain, {})
    return await ws_result(ws, f"{domain}/create", **fields)


async def _helpers(ws: MockHAClientWebSocket, **params: Any) -> Any:
    return await ws_result(ws, WS_HELPERS_LIST, **params)


def _template_helper(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain="template",
        title="Sun Sensor",
        options=options,
        data={"api_key": "DATA_SECRET_XYZ"},
        entry_id="e1",
    )
    entry.add_to_hass(hass)
    return entry


def _flow(result: dict[str, Any]) -> dict[str, Any]:
    return next(h for h in result["helpers"] if h["kind"] == "flow")


async def test_the_listing_reports_stored_config_not_runtime_state(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """#2901: ``set_options`` changes only the runtime options; the listing
    reports the stored helper, as ``input_select/list`` does."""
    stored = await _create(
        hass,
        component_ws,
        "input_select",
        name="Mode",
        options=["stored_a", "stored_b"],
        initial="stored_b",
    )
    await hass.services.async_call(
        "input_select",
        "set_options",
        {"entity_id": "input_select.mode", "options": ["runtime_x", "runtime_y"]},
        blocking=True,
    )

    (rec,) = (await _helpers(component_ws, helper_types=["input_select"]))["helpers"]

    assert rec["config"] == stored
    assert (rec["storage_id"], rec["entity_id"]) == ("mode", "input_select.mode")


async def test_a_renamed_helper_shows_its_current_name_issue_1794(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """The stored name is the creation-time one; a rename only changes the
    registry."""
    await _create(hass, component_ws, "input_boolean", name="Old Name")
    entity_registry.async_update_entity("input_boolean.old_name", name="Current Guest")

    (rec,) = (await _helpers(component_ws))["helpers"]

    assert (rec["name"], rec["storage_id"]) == ("Current Guest", "old_name")


async def test_a_helper_without_a_state_is_still_listed(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """A disabled helper has no state but is still stored, and ``<type>/list``
    lists it."""
    await _create(hass, component_ws, "counter", name="Visits", step=2)
    entity_registry.async_update_entity(
        "counter.visits", disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert hass.states.get("counter.visits") is None

    (rec,) = (await _helpers(component_ws))["helpers"]

    assert (rec["entity_id"], rec["name"]) == ("counter.visits", "Visits")
    assert rec["config"]["step"] == 2


async def test_a_helper_outside_the_storage_has_no_storage_id(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A YAML helper is not in ``<type>/list``; the server leaves it out of a
    storage listing by its null storage id."""
    await set_up_domain(hass, "input_boolean", {"legacy": {"name": "Legacy"}})

    (rec,) = (await _helpers(component_ws))["helpers"]

    assert (rec["entity_id"], rec["storage_id"]) == ("input_boolean.legacy", None)


async def test_a_type_with_a_helper_the_registry_cannot_place_is_left_to_its_list(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """A stored helper whose registry entry was removed cannot be paired with
    an entity; ``<type>/list`` lists it, so the type is not covered."""
    await _create(hass, component_ws, "counter", name="Visits")
    entity_registry.async_remove("counter.visits")
    await hass.async_block_till_done()

    result = await _helpers(component_ws, helper_types=["counter"])

    assert result["helpers"] == []
    assert "counter" not in result["covered_types"]


async def test_flow_helper_options_are_listed_and_entry_data_never_sent(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """The entity bound to the entry supplies its current entity_id and name:
    a rename updates the registry, not the entry title."""
    options = {"state": "{{ is_state('sun.sun', 'above_horizon') }}"}
    entry = _template_helper(hass, options)
    sun = entity_registry.async_get_or_create(
        "binary_sensor",
        "template",
        "sun_up",
        suggested_object_id="sun_up",
        config_entry=entry,
    )
    entity_registry.async_update_entity(sun.entity_id, name="Sun Is Up")

    result = await _helpers(component_ws)

    rec = _flow(result)
    assert (rec["helper_type"], rec["entry_id"], rec["storage_id"]) == (
        "template",
        "e1",
        "e1",
    )
    assert (rec["entity_id"], rec["name"]) == ("binary_sensor.sun_up", "Sun Is Up")
    assert rec["options"] == options
    assert "DATA_SECRET_XYZ" not in json.dumps(result)
    assert "api_key" not in json.dumps(result)


async def test_flow_helper_options_holding_a_secret_are_redacted(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Flow-helper options share ``config_entries``' exposure, and its scrub;
    secrets.yaml is read off the event loop."""
    write_secrets(hass, f"api_password: {_SECRET}\n")
    _template_helper(hass, {"token": _SECRET, "keep": "kept"})

    with executor_jobs(hass) as spy:
        result = await _helpers(component_ws)

    assert _flow(result)["options"] == {"token": "**redacted**", "keep": "kept"}
    assert _SECRET not in json.dumps(result)
    assert jobs_from(spy, "secrets") == 1


async def test_without_secrets_yaml_flow_options_are_listed_as_they_are(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    _template_helper(hass, {"token": "plain"})

    result = await _helpers(component_ws)

    assert _flow(result)["options"] == {"token": "plain"}
    assert "secret_scrub_degraded" not in result


@pytest.mark.parametrize("include_flow", [True, False])
async def test_an_unreadable_secrets_yaml_is_signalled_only_with_flow_helpers(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, include_flow: bool
) -> None:
    """Without flow helpers the scrubbed options are not sent, and
    secrets.yaml is not read at all (perf gate)."""
    write_secrets(hass, "{bad: yaml: [")
    _template_helper(hass, {"token": "x"})

    with executor_jobs(hass) as spy:
        result = await _helpers(component_ws, include_flow_helpers=include_flow)

    assert ("secret_scrub_degraded" in result) is include_flow
    assert jobs_from(spy, "secrets") == (1 if include_flow else 0)


async def test_a_flow_helper_without_an_entity_is_named_by_its_title(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    MockConfigEntry(
        domain="group",
        title="Living Room Group",
        options={"entities": []},
        entry_id="e9",
    ).add_to_hass(hass)

    rec = _flow(await _helpers(component_ws))

    assert (rec["entity_id"], rec["name"]) == (None, "Living Room Group")


async def test_helper_types_selects_the_types_listed(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await set_up_domain(hass, "input_boolean", {"guest": {"name": "Guest"}})
    _template_helper(hass, {"state": "x"})

    templates = await _helpers(component_ws, helper_types=["template"])
    booleans = await _helpers(component_ws, helper_types=["input_boolean"])
    no_flow = await _helpers(component_ws, include_flow_helpers=False)

    assert {r["helper_type"] for r in templates["helpers"]} == {"template"}
    assert {r["helper_type"] for r in booleans["helpers"]} == {"input_boolean"}
    assert [r for r in no_flow["helpers"] if r["kind"] == "flow"] == []


async def test_the_home_zone_is_listed_but_not_searched(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """``ha_get_zone`` lists the home zone, which is outside the zone storage;
    search's helper surface leaves zones out."""
    assert await async_setup_component(hass, "zone", {})

    result = await _helpers(component_ws, helper_types=["zone"])
    searched = await ws_result(
        component_ws, WS_SEARCH, query="home", search_types=["helper"]
    )

    (rec,) = result["helpers"]
    assert (rec["entity_id"], rec["storage_id"]) == ("zone.home", None)
    assert rec["config"]["latitude"] == hass.config.latitude
    assert searched["helpers"] == []


_STOCK = {
    "input_boolean",
    "input_number",
    "input_text",
    "input_select",
    "input_datetime",
    "input_button",
    "counter",
    "schedule",
    "zone",
}


async def test_covered_types_name_only_types_read_like_their_list_command(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """``person/list`` adds YAML persons and ``tag/list`` registry names, so
    those two, and a type that is not set up (timer here), are left to the
    server's own ``<type>/list``."""
    for domain in (*_STOCK, "person", "tag"):
        assert await async_setup_component(hass, domain, {})

    covered = set((await _helpers(component_ws))["covered_types"])

    assert covered >= _STOCK
    assert covered.isdisjoint({"timer", "person", "tag"})
    assert covered >= {"template", "group", "utility_meter"}


async def test_covered_types_leave_out_flow_types_when_flow_is_off(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    assert await async_setup_component(hass, "input_boolean", {})

    result = await _helpers(component_ws, include_flow_helpers=False)

    covered = set(result["covered_types"])
    assert "input_boolean" in covered
    assert covered.isdisjoint(FLOW_HELPER_DOMAINS)
