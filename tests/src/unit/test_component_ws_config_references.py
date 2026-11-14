"""The component's ``ha_mcp_tools/config_references`` command on a real ``hass``.

The command validates a proposed automation, script or scene with Core,
collects its references with Core's helpers, checks them against Core's
registries and puts each entity target to Core's target lookups (#940, #2989).
Every part of that is Core's own here: its validators, its input_select,
select and switch integrations, their services, triggers and conditions, and
its registries. The command runs through Core's WebSocket API
(``component_ws``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.websocket_api import automation as ws_automation
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.script import Script
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.config_references import WS_CONFIG_REFERENCES
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.reference_validator import validate_config_references

from ._component_core import ws_call, ws_result
from ._core_setup import add_test_entry, set_up_domain

OK = {"status": "ok", "missing": [], "mismatches": []}


@pytest.fixture
async def home(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
) -> str:
    """input_select.mode_of_travel, a template switch.kitchen, binary_sensor.hall, the
    services of their integrations and a ``custom.do_it`` without a target,
    one device, area ``kitchen``, floor ``up`` and label ``x``. Returns the
    device's id."""
    assert await async_setup_component(
        hass,
        "input_select",
        {"input_select": {"mode_of_travel": {"options": ["car", "bike"]}}},
    )
    assert await async_setup_component(hass, "select", {})
    # A real switch entity: Core offers targets only for entities a platform added.
    await set_up_domain(
        hass,
        "template",
        [
            {
                "switch": [
                    {
                        "name": "Kitchen",
                        "state": "{{ true }}",
                        "turn_on": [],
                        "turn_off": [],
                    }
                ]
            }
        ],
    )
    hass.states.async_set("binary_sensor.hall", "off")

    async def do_it(call: ServiceCall) -> None:
        return None

    hass.services.async_register("custom", "do_it", do_it)
    area_registry.async_create("Kitchen")
    floor_registry.async_create("Up")
    label_registry.async_create("X")
    entry = add_test_entry(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "d1")}
    )
    await hass.async_block_till_done()
    return device.id


# Core refuses an automation without triggers.
_PROBE_TRIGGER = {"trigger": "event", "event_type": "probe"}


def _proposed(config: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind != "automation" or "triggers" in config:
        return config
    return {"triggers": [_PROBE_TRIGGER], **config}


async def _check(
    ws: MockHAClientWebSocket, config: dict[str, Any], kind: str = "automation"
) -> Any:
    return await ws_result(
        ws, WS_CONFIG_REFERENCES, kind=kind, config=_proposed(config, kind)
    )


def _action(action: str, entity_id: str) -> dict[str, Any]:
    return {"action": action, "target": {"entity_id": entity_id}}


_MISMATCHED_STEP = _action("select.select_option", "input_select.mode_of_travel")
_MISMATCHED_CONDITION = {
    "condition": "switch.is_on",
    "target": {"entity_id": "binary_sensor.hall"},
}


async def test_select_option_on_an_input_select_is_a_mismatch_with_cores_offer(
    component_ws: MockHAClientWebSocket, home: str
) -> None:
    result = await _check(component_ws, {"actions": [_MISMATCHED_STEP]})

    assert (result["status"], result["missing"]) == ("ok", [])
    (mismatch,) = result["mismatches"]
    assert {key: mismatch[key] for key in ("path", "kind", "name", "entity_id")} == {
        "path": "actions[0]",
        "kind": "services",
        "name": "select.select_option",
        "entity_id": "input_select.mode_of_travel",
    }
    assert "input_select.select_option" in mismatch["offered"]
    assert "select.select_option" not in mismatch["offered"]


@pytest.mark.parametrize(
    "step",
    [
        pytest.param(
            _action("input_select.select_option", "input_select.mode_of_travel"),
            id="offered-action",
        ),
        pytest.param(
            _action("custom.do_it", "input_select.mode_of_travel"),
            id="action-core-describes-no-target-for",
        ),
    ],
)
async def test_targets_core_would_offer_are_not_mismatches(
    component_ws: MockHAClientWebSocket, home: str, step: dict[str, Any]
) -> None:
    assert await _check(component_ws, {"actions": [step]}) == OK


async def test_references_core_does_not_have_are_missing_and_sentinels_are_not(
    component_ws: MockHAClientWebSocket, home: str
) -> None:
    config = {
        "triggers": [{"trigger": "state", "entity_id": "sensor.ghost"}],
        "actions": [
            {"action": "notify.mobile_app_andrew_phone"},
            {"action": "switch.turn_on", "target": {"device_id": ["d2", home]}},
            {
                "action": "switch.turn_on",
                "target": {"area_id": "attic", "floor_id": "up", "label_id": "y"},
            },
            {"action": "switch.turn_on", "target": {"entity_id": "all"}},
        ],
    }

    result = await _check(component_ws, config)

    assert sorted((m["kind"], m["value"]) for m in result["missing"]) == [
        ("area", "attic"),
        ("device", "d2"),
        ("entity", "sensor.ghost"),
        ("label", "y"),
        ("service", "notify.mobile_app_andrew_phone"),
    ]


async def test_action_lookalikes_in_event_and_service_data_are_not_services(
    component_ws: MockHAClientWebSocket, home: str
) -> None:
    """An event trigger's ``event_data.action`` and a notification's buttons
    were reported as services Home Assistant does not have."""
    config = {
        "triggers": [
            {
                "trigger": "event",
                "event_type": "mobile_app_notification_action",
                "event_data": {"action": "OPEN_GARAGE"},
            }
        ],
        "actions": [
            {
                "action": "switch.turn_on",
                "target": {"entity_id": "switch.kitchen"},
                "data": {"actions": [{"action": "DISMISS"}]},
            }
        ],
    }

    assert await _check(component_ws, config) == OK


async def test_action_shaped_payloads_are_not_steps(
    component_ws: MockHAClientWebSocket, home: str
) -> None:
    """An event's data, a fired event's data, step variables and a repeat's
    ``for_each`` shaped like a service call, alone or in a list, were put to
    Core's target lookup and reported as mismatches or missing services."""
    payload = {
        "action": "notify.ghost",
        "target": {"entity_id": "input_select.mode_of_travel"},
    }
    config = {
        "triggers": [
            {"trigger": "event", "event_type": "x", "event_data": payload},
            {"trigger": "event", "event_type": "y", "event_data": {"q": [payload]}},
        ],
        "actions": [
            {"event": "fired", "event_data": {"queue": [payload]}},
            {"variables": {"step": payload, "steps": [payload]}},
            {"repeat": {"for_each": [payload], "sequence": [{"delay": 1}]}},
        ],
    }

    assert await _check(component_ws, config) == OK


async def test_a_scene_entities_list_reaches_cores_schema_unchanged(
    component_ws: MockHAClientWebSocket, home: str
) -> None:
    """``entities: []`` was replaced by an empty dict Core accepts."""
    refused = await _check(component_ws, {"entities": []}, "scene")

    assert refused["status"] == "invalid"
    assert await _check(component_ws, {}, "scene") == OK


@pytest.mark.parametrize(
    ("step", "path"),
    [
        pytest.param(
            {"choose": [{"conditions": [], "sequence": [_MISMATCHED_STEP]}]},
            "actions[0].choose[0].sequence[0]",
            id="choose-sequence",
        ),
        pytest.param(
            {"choose": [], "default": [_MISMATCHED_STEP]},
            "actions[0].default[0]",
            id="choose-default",
        ),
        pytest.param(
            {"choose": [{"conditions": [_MISMATCHED_CONDITION], "sequence": []}]},
            "actions[0].choose[0].conditions[0]",
            id="choose-conditions",
        ),
        pytest.param(
            {"if": [_MISMATCHED_CONDITION], "then": []},
            "actions[0].if[0]",
            id="if-conditions",
        ),
        pytest.param(
            {"if": [], "then": [_MISMATCHED_STEP]},
            "actions[0].then[0]",
            id="if-then",
        ),
        pytest.param(
            {"if": [], "then": [], "else": [_MISMATCHED_STEP]},
            "actions[0].else[0]",
            id="if-else",
        ),
        pytest.param(
            {"repeat": {"while": [_MISMATCHED_CONDITION], "sequence": []}},
            "actions[0].repeat.while[0]",
            id="repeat-while",
        ),
        pytest.param(
            {"repeat": {"until": [_MISMATCHED_CONDITION], "sequence": []}},
            "actions[0].repeat.until[0]",
            id="repeat-until",
        ),
        pytest.param(
            {"repeat": {"count": 2, "sequence": [_MISMATCHED_STEP]}},
            "actions[0].repeat.sequence[0]",
            id="repeat-sequence",
        ),
        pytest.param(
            {"parallel": [{"sequence": [_MISMATCHED_STEP]}]},
            "actions[0].parallel[0].sequence[0]",
            id="parallel",
        ),
        pytest.param(
            {"sequence": [_MISMATCHED_STEP]},
            "actions[0].sequence[0]",
            id="sequence",
        ),
        pytest.param(
            {
                "wait_for_trigger": [
                    {
                        "trigger": "switch.turned_on",
                        "target": {"entity_id": "binary_sensor.hall"},
                    }
                ]
            },
            "actions[0].wait_for_trigger[0]",
            id="wait-for-trigger",
        ),
        pytest.param(
            {"condition": "and", "conditions": [_MISMATCHED_CONDITION]},
            "actions[0].conditions[0]",
            id="condition-step-nested",
        ),
    ],
)
async def test_items_nested_the_way_core_nests_them_are_checked(
    component_ws: MockHAClientWebSocket,
    home: str,
    step: dict[str, Any],
    path: str,
) -> None:
    result = await _check(component_ws, {"actions": [step]})

    assert [m["path"] for m in result["mismatches"]] == [path]


async def test_trigger_and_condition_targets_are_put_to_their_own_lookups(
    component_ws: MockHAClientWebSocket, home: str
) -> None:
    config = {
        "triggers": [
            {"trigger": "switch.turned_on", "target": {"entity_id": "switch.kitchen"}},
            {
                "trigger": "switch.turned_on",
                "target": {"entity_id": "binary_sensor.hall"},
            },
        ],
        "conditions": [
            {"condition": "switch.is_on", "target": {"entity_id": "binary_sensor.hall"}}
        ],
        "actions": [],
    }

    result = await _check(component_ws, config)

    assert [(m["path"], m["kind"]) for m in result["mismatches"]] == [
        ("triggers[1]", "triggers"),
        ("conditions[0]", "conditions"),
    ]


@pytest.mark.parametrize(
    ("kind", "config"),
    [
        ("automation", {"triggers": [], "actions": [{"bogus": 1}]}),
        ("script", {"sequence": [{"bogus": 1}]}),
    ],
)
async def test_a_config_core_refuses_is_answered_with_cores_error(
    component_ws: MockHAClientWebSocket,
    home: str,
    kind: str,
    config: dict[str, Any],
) -> None:
    result = await _check(component_ws, config, kind)

    assert result["status"] == "invalid"
    assert result["error"]


async def test_a_script_sequence_is_checked(
    component_ws: MockHAClientWebSocket, home: str
) -> None:
    result = await _check(component_ws, {"sequence": [_MISMATCHED_STEP]}, "script")

    assert [m["path"] for m in result["mismatches"]] == ["sequence[0]"]


async def test_a_scene_entity_core_does_not_have_is_missing(
    component_ws: MockHAClientWebSocket, home: str
) -> None:
    result = await _check(
        component_ws,
        {"entities": {"switch.kitchen": "on", "switch.ghost": "off"}},
        "scene",
    )

    assert result == {
        "status": "ok",
        "missing": [{"kind": "entity", "value": "switch.ghost"}],
        "mismatches": [],
    }


async def test_a_step_holding_a_template_is_still_checked(
    component_ws: MockHAClientWebSocket, home: str
) -> None:
    """Every config with a template in a service step answered "unavailable":
    Core's Template holds hass, and copying the step failed."""
    config = {
        "actions": [
            {"action": "notify.ghost", "data": {"message": "{{ trigger.id }}"}},
            {
                "action": "select.select_option",
                "target": {"entity_id": "input_select.mode_of_travel"},
                "data": {"option": "{{ trigger.id }}"},
            },
        ]
    }

    result = await _check(component_ws, config)

    assert result["missing"] == [{"kind": "service", "value": "notify.ghost"}]
    assert [m["path"] for m in result["mismatches"]] == ["actions[1]"]


async def test_a_core_without_a_part_names_it(
    component_ws: MockHAClientWebSocket,
    home: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a Core rename the check stopped with no hint of which part went."""
    monkeypatch.delattr(Script, "_find_referenced_entities")

    result = await _check(component_ws, {"actions": []})

    assert result["status"] == "unavailable"
    assert "script_entities" in result["error"]


async def test_a_failure_inside_the_check_is_answered_not_raised(
    component_ws: MockHAClientWebSocket, home: str
) -> None:
    with patch.object(
        ws_automation,
        "async_get_services_for_target",
        side_effect=TypeError("boom"),
    ):
        result = await _check(component_ws, {"actions": [_MISMATCHED_STEP]})

    assert result["status"] == "unavailable"
    assert "TypeError" in result["error"]


async def test_the_server_reads_the_components_answer(
    component_core_client: HomeAssistantClient, home: str
) -> None:
    """The server's reading of the answer and the component's answer agree."""
    meta = await validate_config_references(
        component_core_client,
        _proposed(
            {"actions": [{"action": "notify.ghost"}, _MISMATCHED_STEP]}, "automation"
        ),
        "automation",
    )

    warnings = meta["warnings"]
    assert [(w["kind"], w["value"]) for w in warnings] == [
        ("service", "notify.ghost"),
        ("entity", "input_select.mode_of_travel"),
    ]
    assert "input_select.select_option" in warnings[1]["offered"]


async def test_the_command_is_admin_only(
    component_ws_read_only: MockHAClientWebSocket,
) -> None:
    reply = await ws_call(
        component_ws_read_only, WS_CONFIG_REFERENCES, kind="automation", config={}
    )

    assert reply["error"]["code"] == "unauthorized"
