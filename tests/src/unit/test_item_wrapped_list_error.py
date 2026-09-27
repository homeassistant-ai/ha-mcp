"""List fields that arrive as {"item": ...} or "" get an explanatory error (issue #2548)."""

from __future__ import annotations

import json

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.config_entry_flow_form import _consume_form_schema
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_dashboards import _reject_malformed_dashboard_lists
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

_ACTION = {"action": "light.turn_on", "target": {"entity_id": "light.x"}}
_TRIGGER = {"trigger": "state", "entity_id": "input_boolean.x"}


def _error(exc: ToolError) -> dict:
    return json.loads(str(exc))["error"]


@pytest.mark.parametrize(
    "key,wrapped",
    [
        ("actions", {"item": _ACTION}),
        ("actions", {"item": [_ACTION, _ACTION]}),
        ("triggers", {"item": _TRIGGER}),
        ("conditions", {"item": {"condition": "state", "entity_id": "sun.sun"}}),
        ("action", {"item": _ACTION}),
    ],
)
def test_automation_item_wrapped_list_is_named(key: str, wrapped: dict) -> None:
    config = {"alias": "x", "triggers": [_TRIGGER], "actions": [_ACTION], key: wrapped}
    with pytest.raises(ToolError) as exc_info:
        AutomationConfigTools._parse_and_validate_config(config)
    error = _error(exc_info.value)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert f"'{key}'" in error["message"]
    assert '{"item": ...}' in error["message"]
    assert "JSON-encoded string" in json.dumps(error["suggestions"])


def test_script_item_wrapped_sequence_is_named() -> None:
    with pytest.raises(ToolError) as exc_info:
        ConfigScriptTools._validate_script_config(
            {"alias": "x", "sequence": {"item": [_ACTION]}}, "s", None
        )
    error = _error(exc_info.value)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "'sequence'" in error["message"]


@pytest.mark.parametrize(
    "config",
    [
        # todo.add_item legitimately sends data: {"item": ...}; only list fields are checked.
        {
            "alias": "x",
            "triggers": [_TRIGGER],
            "actions": [
                {
                    "action": "todo.add_item",
                    "target": {"entity_id": "todo.x"},
                    "data": {"item": "Milk"},
                }
            ],
        },
        # A single action object is valid HA shorthand for a one-element list.
        {"alias": "x", "triggers": _TRIGGER, "actions": _ACTION},
        {
            "alias": "x",
            "triggers": [_TRIGGER],
            "conditions": "{{ true }}",
            "actions": [_ACTION],
        },
    ],
)
def test_valid_automation_configs_pass(config: dict) -> None:
    assert AutomationConfigTools._parse_and_validate_config(config) == config


def test_valid_script_with_todo_item_data_passes() -> None:
    config = {
        "alias": "x",
        "sequence": [
            {
                "action": "todo.add_item",
                "target": {"entity_id": "todo.x"},
                "data": {"item": "Milk"},
            }
        ],
    }
    result, _ = ConfigScriptTools._validate_script_config(dict(config), "s", None)
    assert result == config


# Template helpers serialize their action fields as {"selector": {"action": {}}}.
_BUTTON_SCHEMA = [
    {"name": "name", "required": True, "selector": {"text": {}}},
    {"name": "press", "required": False, "selector": {"action": {}}},
]


@pytest.mark.parametrize("press", [{"item": [_ACTION, _ACTION]}, ""])
def test_flow_action_selector_malformed_is_named(press: object) -> None:
    config = {"name": "b", "press": press}
    with pytest.raises(ToolError) as exc_info:
        _consume_form_schema(_BUTTON_SCHEMA, config)
    error = _error(exc_info.value)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "'press'" in error["message"]


@pytest.mark.parametrize(
    "config",
    [
        {"name": "b", "press": [_ACTION]},
        {"name": "b", "press": _ACTION},
        # Only action-selector fields are checked; other fields pass through as-is.
        {"name": {"item": "x"}, "press": [_ACTION]},
    ],
)
def test_flow_valid_values_pass(config: dict) -> None:
    expected = dict(config)
    assert _consume_form_schema(_BUTTON_SCHEMA, config) == expected


@pytest.mark.parametrize("key", ["triggers", "conditions", "actions"])
def test_automation_empty_string_list_is_named(key: str) -> None:
    config = {"alias": "x", "triggers": [_TRIGGER], "actions": [_ACTION], key: ""}
    with pytest.raises(ToolError) as exc_info:
        AutomationConfigTools._parse_and_validate_config(config)
    assert f"'{key}' as \"\"" in _error(exc_info.value)["message"]


def test_script_empty_string_sequence_is_named() -> None:
    with pytest.raises(ToolError) as exc_info:
        ConfigScriptTools._validate_script_config(
            {"alias": "x", "sequence": ""}, "s", None
        )
    assert "'sequence' as \"\"" in _error(exc_info.value)["message"]


@pytest.mark.parametrize(
    "config,path",
    [
        ({"views": {"item": [{"cards": []}]}}, "'views'"),
        ({"views": ""}, "'views'"),
        ({"views": [{"cards": {"item": {"type": "tile"}}}]}, "'views[0].cards'"),
        ({"views": [{"badges": {"item": "sun.sun"}}]}, "'views[0].badges'"),
        (
            {"views": [{"sections": [{"cards": {"item": [{"type": "tile"}]}}]}]},
            "'views[0].sections[0].cards'",
        ),
    ],
)
def test_dashboard_malformed_list_is_named(config: dict, path: str) -> None:
    with pytest.raises(ToolError) as exc_info:
        _reject_malformed_dashboard_lists(config, "test-dash")
    assert path in _error(exc_info.value)["message"]


@pytest.mark.parametrize(
    "config",
    [
        {"views": [{"sections": [{"cards": [{"type": "tile"}]}], "badges": []}]},
        {"strategy": {"type": "original-states"}},
        # Card-level options belong to each card type and are not checked.
        {"views": [{"cards": [{"type": "custom:x", "options": {"item": "a"}}]}]},
    ],
)
def test_dashboard_valid_configs_pass(config: dict) -> None:
    _reject_malformed_dashboard_lists(config, "test-dash")
