"""List fields that arrive as {"item": ...} or "" get an explanatory error (issue #2548)."""

from __future__ import annotations

import json

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.config_entry_flow_form import _consume_form_schema
from ha_mcp.tools.dashboard_list_checks import (
    patch_writes_inside_card,
    reject_malformed_dashboard_config,
    reject_malformed_dashboard_lists,
    reject_malformed_dashboard_patch,
)
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
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
    expected = json.loads(json.dumps(config))
    assert AutomationConfigTools._parse_and_validate_config(config) == expected


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
        reject_malformed_dashboard_lists(config, "test-dash")
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
    reject_malformed_dashboard_lists(config, "test-dash")


_STACK = {"type": "vertical-stack", "cards": {"item": [{"type": "tile"}]}}


def test_dashboard_nested_stack_card_list_is_named() -> None:
    config = {"views": [{"cards": [{"type": "grid", "cards": [_STACK]}]}]}
    with pytest.raises(ToolError) as exc_info:
        reject_malformed_dashboard_lists(config, "test-dash")
    assert "'views[0].cards[0].cards[0].cards'" in _error(exc_info.value)["message"]


def test_dashboard_custom_card_cards_option_passes() -> None:
    config = {"views": [{"cards": [{"type": "custom:x", "cards": {"item": "a"}}]}]}
    reject_malformed_dashboard_lists(config, "test-dash")


@pytest.mark.parametrize(
    "op",
    [
        {"op": "replace", "path": "/views/0/cards", "value": {"item": [{}]}},
        {"op": "add", "path": "/views/-", "value": {"sections": ""}},
        {"op": "add", "path": "/views/0/sections/1/cards/-", "value": _STACK},
    ],
)
def test_dashboard_patch_malformed_list_is_named(op: dict) -> None:
    with pytest.raises(ToolError) as exc_info:
        reject_malformed_dashboard_patch([op], "test-dash")
    assert f"({op['path']})" in _error(exc_info.value)["message"]


@pytest.mark.parametrize(
    "op",
    [
        {"op": "add", "path": "/views/0/cards/-", "value": {"type": "tile"}},
        {"op": "test", "path": "/views/0/cards", "value": {"item": [{}]}},
        {"op": "replace", "path": "/views/0/title", "value": ""},
        {"op": "remove", "path": "/views/0/cards/0"},
    ],
)
def test_dashboard_patch_valid_ops_pass(op: dict) -> None:
    reject_malformed_dashboard_patch([op], "test-dash")


@pytest.mark.parametrize("wrapper", ["conditional", "entity-filter"])
def test_dashboard_stack_inside_wrapper_card_is_named(wrapper: str) -> None:
    config = {"views": [{"cards": [{"type": wrapper, "card": _STACK}]}]}
    with pytest.raises(ToolError) as exc_info:
        reject_malformed_dashboard_lists(config, "test-dash")
    assert "'views[0].cards[0].card.cards'" in _error(exc_info.value)["message"]


def test_dashboard_custom_card_nested_card_passes() -> None:
    config = {"views": [{"cards": [{"type": "custom:x", "card": _STACK}]}]}
    reject_malformed_dashboard_lists(config, "test-dash")


def _dashboard_tools(monkeypatch, current: dict) -> DashboardConfigTools:
    async def fetch(_client, _url_path):
        return current, "hash"

    monkeypatch.setattr(
        "ha_mcp.tools.tools_config_dashboards._get_dashboard_config_internal", fetch
    )
    return DashboardConfigTools(object())


_DEEP_OP = {"op": "replace", "path": "/views/0/cards/0/cards", "value": {"item": [{}]}}


@pytest.mark.anyio
async def test_dashboard_patch_inside_stack_card_is_named(monkeypatch) -> None:
    tools = _dashboard_tools(
        monkeypatch, {"views": [{"cards": [{"type": "grid", "cards": []}]}]}
    )
    with pytest.raises(ToolError) as exc_info:
        await tools._reject_malformed_patched_dashboard("test-dash", [_DEEP_OP])
    assert "'views[0].cards[0].cards'" in _error(exc_info.value)["message"]


@pytest.mark.anyio
async def test_dashboard_patch_inside_custom_card_passes(monkeypatch) -> None:
    tools = _dashboard_tools(
        monkeypatch, {"views": [{"cards": [{"type": "custom:x", "cards": []}]}]}
    )
    await tools._reject_malformed_patched_dashboard("test-dash", [_DEEP_OP])


def test_dashboard_python_transform_result_is_named() -> None:
    with pytest.raises(ToolError) as exc_info:
        DashboardConfigTools._apply_dashboard_python_transform(
            "test-dash", "config['views'] = ''", {"views": []}
        )
    error = _error(exc_info.value)
    assert "python_transform produced 'views'" in error["message"]


@pytest.mark.anyio
async def test_automation_python_transform_result_is_named(monkeypatch) -> None:
    tools = AutomationConfigTools(object())

    async def fetch(_identifier, _hash, _action):
        return {"alias": "x", "triggers": [_TRIGGER], "actions": [_ACTION]}, "id"

    monkeypatch.setattr(tools, "_fetch_and_verify_hash", fetch)
    with pytest.raises(ToolError) as exc_info:
        await tools._run_python_transform(
            "id", "h", "config['actions'] = ''", None, False, None, False
        )
    assert "python_transform produced 'actions'" in _error(exc_info.value)["message"]


@pytest.mark.anyio
async def test_script_python_transform_result_is_named(monkeypatch) -> None:
    tools = ConfigScriptTools(object())

    async def fetch(_script_id, _hash, _action):
        return {"alias": "x", "sequence": [_ACTION]}, "s"

    monkeypatch.setattr(tools, "_fetch_and_verify_hash", fetch)
    with pytest.raises(ToolError) as exc_info:
        await tools._prepare_script_transform(
            "s", "h", "config['sequence'] = {'item': []}"
        )
    assert "python_transform produced 'sequence'" in _error(exc_info.value)["message"]


def test_automation_multiple_malformed_fields_are_all_named() -> None:
    config = {"alias": "x", "triggers": {"item": _TRIGGER}, "actions": ""}
    with pytest.raises(ToolError) as exc_info:
        AutomationConfigTools._parse_and_validate_config(config)
    body = json.loads(str(exc_info.value))
    assert (
        "'triggers' as {\"item\": ...}; 'actions' as \"\"" in body["error"]["message"]
    )
    assert body["malformed_list_fields"] == {
        "triggers": '{"item": ...}',
        "actions": '""',
    }


@pytest.mark.parametrize("key", ["trigger", "condition"])
def test_automation_singular_keys_are_checked(key: str) -> None:
    config = {"alias": "x", "triggers": [_TRIGGER], "actions": [_ACTION], key: ""}
    with pytest.raises(ToolError):
        AutomationConfigTools._parse_and_validate_config(config)


@pytest.mark.parametrize(
    "op,label",
    [
        (
            {"op": "replace", "path": "/views", "value": [{"cards": {"item": []}}]},
            "(/views)[0].cards",
        ),
        (
            {"op": "add", "path": "/views/0/sections/-", "value": {"cards": ""}},
            "(/views/0/sections/-).cards",
        ),
        (
            {"op": "replace", "path": "/views/0/badges", "value": {"item": "sun.sun"}},
            "(/views/0/badges)'",
        ),
        (
            {"op": "replace", "path": "", "value": {"views": [{"sections": ""}]}},
            "().views[0].sections",
        ),
    ],
)
def test_dashboard_patch_branches_name_the_field(op: dict, label: str) -> None:
    with pytest.raises(ToolError) as exc_info:
        reject_malformed_dashboard_patch([op], "test-dash")
    assert label in _error(exc_info.value)["message"]


def test_dashboard_patch_error_points_at_patch_not_config() -> None:
    op = {"op": "replace", "path": "/views/0/cards", "value": {"item": []}}
    with pytest.raises(ToolError) as exc_info:
        reject_malformed_dashboard_patch([op], "test-dash")
    error = _error(exc_info.value)
    assert error["message"].startswith("Received patch value")
    assert "whole patch as one JSON-encoded string" in json.dumps(error)


def test_dashboard_json_string_config_is_checked() -> None:
    with pytest.raises(ToolError):
        reject_malformed_dashboard_config('{"views": [{"cards": ""}]}', "test-dash")
    reject_malformed_dashboard_config("{not json", "test-dash")


@pytest.mark.parametrize(
    "op,expected",
    [
        ({"op": "replace", "path": "/views/0/cards/0/icon", "value": "mdi:x"}, False),
        ({"op": "replace", "path": "/views/0/cards/0/cards", "value": ""}, True),
        ({"op": "add", "path": "/views/0/cards/0/cards/-", "value": _STACK}, True),
        ({"op": "remove", "path": "/views/0/cards/0/cards/0"}, False),
    ],
)
def test_patch_writes_inside_card_only_for_structural_edits(
    op: dict, expected: bool
) -> None:
    assert patch_writes_inside_card([op]) is expected
