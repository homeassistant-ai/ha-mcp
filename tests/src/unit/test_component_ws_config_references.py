"""Unit tests for the component-side ``ha_mcp_tools/config_references`` command.

The command validates a proposed automation, script or scene with Core,
collects its references with Core's helpers, checks them against Core's
registries and puts each entity target to Core's target lookups (#940, #2989).
Core's parts are faked here; these tests pin what the component owns: what it
asks Core, and what it reports from Core's answers.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.modules.setdefault("homeassistant", SimpleNamespace())
for _mod in (
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

from ha_mcp.tools.reference_validator import _warnings  # noqa: E402

from ._component_ws_api import wsapi  # noqa: E402  (after the stubs above)
from .test_component_ws_search import (  # noqa: E402
    FakeHass,
    _FakeConnection,
    _Unauthorized,
    functional_ws,  # noqa: F401 - pytest fixture
)

config_references = wsapi.config_references

_STATES = {"input_select.mode_of_travel", "light.kitchen", "binary_sensor.hall"}
_SERVICES = {"input_select.select_option", "select.select_option", "light.turn_on"}
_REGISTRY = {"device": {"d1"}, "area": {"kitchen"}, "floor": {"up"}, "label": {"x"}}
# Core's lookup answers, by kind and entity.
_OFFERED = {
    "services": {
        "input_select.mode_of_travel": {
            "input_select.select_option",
            "input_select.select_next",
        },
        "light.kitchen": {"light.turn_on"},
    },
    "triggers": {"light.kitchen": {"light.turned_on"}},
    "conditions": {"light.kitchen": {"light.is_on"}},
}


class _Template:
    """Stands in for Core's Template: it holds ``hass``, which cannot be copied."""

    def __deepcopy__(self, memo: dict) -> Any:
        raise TypeError("HomeAssistant.__new__() missing 1 required argument")


def _templated(value: Any) -> Any:
    """Template strings become Template objects, as Core's validator makes them."""
    if isinstance(value, dict):
        return {k: _templated(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_templated(v) for v in value]
    if isinstance(value, str) and "{{" in value:
        return _Template()
    return value


class _Checked:
    """Core's validated config: its values and its validation outcome."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = _templated(config)
        self.get = self._config.get
        refused = config.get("refuse")
        self.validation_status = "failed_actions" if refused else "ok"
        self.validation_error = refused


# Core's ACTIONS_SET keys and the kind each one names.
_ACTION_KEYS = {
    "action": "call_service",
    "condition": "condition",
    "choose": "choose",
    "if": "if",
    "repeat": "repeat",
    "parallel": "parallel",
    "sequence": "sequence",
    "wait_for_trigger": "wait_for_trigger",
    "event": "event",
    "variables": "variables",
    "delay": "delay",
}


def _determine(node: dict[str, Any]) -> str:
    for key, kind in _ACTION_KEYS.items():
        if key in node:
            return kind
    raise ValueError("Unable to determine action")


def _service_name(name: str) -> str:
    if "." not in name:
        raise ValueError("invalid service")
    return name


def _steps(sequence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every step, nested choose sequences included, as Core's Script visits."""
    found = []
    for step in sequence:
        found.append(step)
        for option in step.get("choose", []):
            found.extend(_steps(option["sequence"]))
    return found


def _script_entities(found: set[str], sequence: list[dict[str, Any]]) -> None:
    for step in _steps(sequence):
        for block in (step, step.get("target"), step.get("data")):
            if isinstance(block, dict) and isinstance(block.get("entity_id"), str):
                found.add(block["entity_id"])


def _script_devices(found: set[str], sequence: list[dict[str, Any]]) -> None:
    found.update(s["device_id"] for s in _steps(sequence) if "device_id" in s)


def _script_targets(attr: str, found: set[str], sequence: list[dict]) -> None:
    for step in _steps(sequence):
        if attr in (step.get("target") or {}):
            found.add(step["target"][attr])


def _extract(attr: str) -> Any:
    def extract(conf: dict[str, Any]) -> set[str]:
        value = conf.get(attr)
        return {value} if isinstance(value, str) else set()

    return extract


def _lookup(kind: str) -> Any:
    async def lookup(hass: Any, target: dict[str, Any], expand_group: bool) -> set:
        (entity_id,) = target["entity_id"]
        return _OFFERED[kind].get(entity_id, set())

    return lookup


def _registry(kind: str, getter: str) -> Any:
    held = _REGISTRY.get(kind, set())
    return lambda hass: SimpleNamespace(
        **{getter: lambda value: object() if value in held else None}
    )


def _scene_states(entities: Any) -> dict[str, Any]:
    """Core's STATES_SCHEMA takes a dict; a list is refused."""
    if not isinstance(entities, dict):
        raise ValueError("expected a dictionary")
    return dict(entities)


def _fake_core() -> SimpleNamespace:
    helpers = SimpleNamespace(
        entities=_extract("entity_id"),
        devices=_extract("device_id"),
        targets=lambda conf, attr: set(),
    )
    return SimpleNamespace(
        const=SimpleNamespace(
            CONF_ACTION="action",
            CONF_ACTIONS="actions",
            CONF_CHOOSE="choose",
            CONF_CONDITION="condition",
            CONF_CONDITIONS="conditions",
            CONF_DEFAULT="default",
            CONF_ELSE="else",
            CONF_ENTITIES="entities",
            CONF_IF="if",
            CONF_PARALLEL="parallel",
            CONF_PLATFORM="platform",
            CONF_REPEAT="repeat",
            CONF_SEQUENCE="sequence",
            CONF_TARGET="target",
            CONF_THEN="then",
            CONF_TRIGGER="trigger",
            CONF_TRIGGERS="triggers",
            CONF_UNTIL="until",
            CONF_WAIT_FOR_TRIGGER="wait_for_trigger",
            CONF_WHILE="while",
            ATTR_AREA_ID="area_id",
            ATTR_FLOOR_ID="floor_id",
            ATTR_LABEL_ID="label_id",
            ENTITY_MATCH_ALL="all",
            ENTITY_MATCH_NONE="none",
        ),
        validate_automation=AsyncMock(
            side_effect=lambda hass, config, **kw: _Checked(config)
        ),
        validate_script=AsyncMock(
            side_effect=lambda hass, object_id, config, **kw: _Checked(config)
        ),
        scene_states=_scene_states,
        script_entities=_script_entities,
        script_devices=_script_devices,
        script_targets=_script_targets,
        trigger=helpers,
        condition=helpers,
        registries=SimpleNamespace(
            entity=_registry("entity", "async_get"),
            device=_registry("device", "async_get"),
            area=_registry("area", "async_get_area"),
            floor=_registry("floor", "async_get_floor"),
            label=_registry("label", "async_get_label"),
        ),
        determine_action=_determine,
        actions=SimpleNamespace(
            call_service="call_service",
            condition="condition",
            wait_for_trigger="wait_for_trigger",
            choose="choose",
            if_="if",
            repeat="repeat",
            parallel="parallel",
            sequence="sequence",
        ),
        service_name=_service_name,
        target_selection=lambda target: target,
        referenced=lambda hass, selection, expand_group: SimpleNamespace(
            referenced={selection["entity_id"]}
        ),
        is_template=lambda value: "{{" in value,
        descriptions={
            "services": AsyncMock(
                return_value={
                    "select": {"select_option": {"target": {}}},
                    "input_select": {"select_option": {"target": {}}},
                    "light": {"turn_on": {"target": {}}},
                    "custom": {"do_it": {"fields": {}}},
                }
            ),
            "triggers": AsyncMock(return_value={"light.turned_on": {"target": {}}}),
            "conditions": AsyncMock(return_value={"light.is_on": {"target": {}}}),
        },
        for_target={kind: _lookup(kind) for kind in _OFFERED},
    )


_HASS = SimpleNamespace(
    states=SimpleNamespace(get=lambda eid: object() if eid in _STATES else None),
    services=SimpleNamespace(
        has_service=lambda domain, service: (
            f"{domain}.{service}" in _SERVICES or domain == "custom"
        )
    ),
)


@pytest.fixture(autouse=True)
def _core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_references, "_core", _fake_core)


async def _check(config: dict[str, Any], kind: str = "automation") -> dict[str, Any]:
    return await config_references.async_config_references(
        _HASS, {"kind": kind, "config": config}
    )


def _action(action: str, entity_id: str) -> dict[str, Any]:
    return {"action": action, "target": {"entity_id": entity_id}}


@pytest.mark.anyio
async def test_select_option_on_an_input_select_is_a_mismatch_with_cores_offer():
    result = await _check(
        {"actions": [_action("select.select_option", "input_select.mode_of_travel")]}
    )
    assert result == {
        "status": "ok",
        "missing": [],
        "mismatches": [
            {
                "path": "actions[0]",
                "kind": "services",
                "name": "select.select_option",
                "entity_id": "input_select.mode_of_travel",
                "offered": ["input_select.select_next", "input_select.select_option"],
            }
        ],
    }


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
@pytest.mark.anyio
async def test_targets_core_would_offer_are_not_mismatches(step):
    result = await _check({"actions": [step]})
    assert result == {"status": "ok", "missing": [], "mismatches": []}


@pytest.mark.anyio
async def test_references_core_does_not_have_are_missing_and_sentinels_are_not():
    config = {
        "triggers": [{"trigger": "state", "entity_id": "sensor.ghost"}],
        "actions": [
            {"action": "notify.mobile_app_andrew_phone"},
            {"device_id": "d2", "domain": "light"},
            {
                "action": "light.turn_on",
                "target": {"area_id": "attic", "floor_id": "up", "label_id": "y"},
            },
            {"action": "light.turn_on", "target": {"entity_id": "all"}},
        ],
    }
    result = await _check(config)
    assert sorted((m["kind"], m["value"]) for m in result["missing"]) == [
        ("area", "attic"),
        ("device", "d2"),
        ("entity", "sensor.ghost"),
        ("label", "y"),
        ("service", "notify.mobile_app_andrew_phone"),
    ]


@pytest.mark.anyio
async def test_action_lookalikes_in_event_and_service_data_are_not_services():
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
                "action": "light.turn_on",
                "target": {"entity_id": "light.kitchen"},
                "data": {"actions": [{"action": "DISMISS"}]},
            }
        ],
    }
    assert await _check(config) == {"status": "ok", "missing": [], "mismatches": []}


@pytest.mark.anyio
async def test_action_shaped_payloads_are_not_steps():
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
    assert await _check(config) == {"status": "ok", "missing": [], "mismatches": []}


@pytest.mark.anyio
async def test_a_scene_entities_list_reaches_cores_schema_unchanged():
    """``entities: []`` was replaced by an empty dict Core accepts."""
    result = await _check({"entities": []}, "scene")
    assert result["status"] == "invalid"
    assert await _check({}, "scene") == {
        "status": "ok",
        "missing": [],
        "mismatches": [],
    }


_MISMATCHED_STEP = _action("select.select_option", "input_select.mode_of_travel")
_MISMATCHED_CONDITION = {
    "condition": "light.is_on",
    "target": {"entity_id": "binary_sensor.hall"},
}


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
                        "trigger": "light.turned_on",
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
@pytest.mark.anyio
async def test_items_nested_the_way_core_nests_them_are_checked(step, path):
    result = await _check({"actions": [step]})
    assert [m["path"] for m in result["mismatches"]] == [path]


@pytest.mark.anyio
async def test_trigger_and_condition_targets_are_put_to_their_own_lookups():
    config = {
        "triggers": [
            {"trigger": "light.turned_on", "target": {"entity_id": "light.kitchen"}},
            {
                "trigger": "light.turned_on",
                "target": {"entity_id": "binary_sensor.hall"},
            },
        ],
        "conditions": [
            {"condition": "light.is_on", "target": {"entity_id": "binary_sensor.hall"}}
        ],
    }
    result = await _check(config)
    assert [(m["path"], m["kind"]) for m in result["mismatches"]] == [
        ("triggers[1]", "triggers"),
        ("conditions[0]", "conditions"),
    ]


@pytest.mark.parametrize("kind", ["automation", "script"])
@pytest.mark.anyio
async def test_a_config_core_refuses_is_answered_with_cores_error(kind):
    assert await _check({"refuse": "Invalid action"}, kind) == {
        "status": "invalid",
        "error": "Invalid action",
    }


@pytest.mark.anyio
async def test_a_script_sequence_is_checked():
    result = await _check(
        {"sequence": [_action("select.select_option", "input_select.mode_of_travel")]},
        "script",
    )
    assert [m["path"] for m in result["mismatches"]] == ["sequence[0]"]


@pytest.mark.anyio
async def test_a_scene_entity_core_does_not_have_is_missing():
    result = await _check(
        {"entities": {"light.kitchen": {"state": "on"}, "light.ghost": {}}}, "scene"
    )
    assert result == {
        "status": "ok",
        "missing": [{"kind": "entity", "value": "light.ghost"}],
        "mismatches": [],
    }


@pytest.mark.anyio
async def test_a_step_holding_a_template_is_still_checked():
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
    result = await _check(config)
    assert result["missing"] == [{"kind": "service", "value": "notify.ghost"}]
    assert [m["path"] for m in result["mismatches"]] == ["actions[1]"]


@pytest.mark.anyio
async def test_a_core_without_a_part_names_it(monkeypatch):
    """After a Core rename the check stopped with no hint of which part went."""
    monkeypatch.setattr(config_references, "_core", lambda: "script_entities")
    result = await _check({"actions": []})
    assert result["status"] == "unavailable"
    assert "script_entities" in result["error"]


@pytest.mark.anyio
async def test_a_failure_inside_the_check_is_answered_not_raised(monkeypatch):
    core = _fake_core()
    core.for_target["services"] = AsyncMock(side_effect=TypeError("boom"))
    monkeypatch.setattr(config_references, "_core", lambda: core)
    result = await _check(
        {"actions": [_action("select.select_option", "input_select.mode_of_travel")]}
    )
    assert result["status"] == "unavailable"
    assert "TypeError" in result["error"]


@pytest.mark.anyio
async def test_the_server_reads_the_components_answer():
    """The server's reading of the answer and the component's answer must agree."""
    answer = await _check(
        {
            "actions": [
                {"action": "notify.ghost"},
                _action("select.select_option", "input_select.mode_of_travel"),
            ]
        }
    )
    warnings = _warnings(answer)
    assert [(w["kind"], w["value"]) for w in warnings] == [
        ("service", "notify.ghost"),
        ("entity", "input_select.mode_of_travel"),
    ]
    assert "input_select.select_option" in warnings[1]["offered"]


def test_the_command_is_admin_gated(functional_ws):  # noqa: F811
    handler = functional_ws.registered[config_references.WS_CONFIG_REFERENCES]
    msg = {"id": 1, "type": config_references.WS_CONFIG_REFERENCES}
    msg |= {"kind": "automation", "config": {}}
    for connection in (
        _FakeConnection(is_admin=False),
        _FakeConnection(has_user=False),
    ):
        with pytest.raises(_Unauthorized):
            handler(FakeHass(), connection, msg)
