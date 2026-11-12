"""List fields that arrive as {"item": ...} or "" get an explanatory error (issue #2548).

The automation, script and dashboard tools run on the server's own client
against a real ``hass``; a config they let through is written to Core. The
flow form's schema is the one Core's template flow shows.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_entry_flow_form import form_submission
from ha_mcp.tools.dashboard_list_checks import (
    patch_writes_inside_card,
    reject_malformed_dashboard_config,
    reject_malformed_dashboard_lists,
    reject_malformed_dashboard_patch,
)
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._config_writes import automation_tools, error_body, error_of, script_tools
from ._dashboard_search_helpers import SEARCH_URL_PATH, store_dashboard

_ACTION = {"action": "light.turn_on", "target": {"entity_id": "light.x"}}
_TRIGGER = {"trigger": "state", "entity_id": "input_boolean.x"}


def _error(exc: ToolError) -> dict[str, Any]:
    error: dict[str, Any] = json.loads(str(exc))["error"]
    return error


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    return hass_tmp_config_dir


@pytest.fixture
async def automations(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AutomationConfigTools:
    return await automation_tools(hass, core_client, monkeypatch)


@pytest.fixture
async def scripts(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> ConfigScriptTools:
    return await script_tools(hass, core_client, monkeypatch)


async def _automation_rejected(
    automations: AutomationConfigTools, config: dict[str, Any]
) -> pytest.ExceptionInfo[ToolError]:
    with pytest.raises(ToolError) as exc_info:
        await automations.ha_config_set_automation(config=config, MandatoryBPS=False)
    return exc_info


async def _script_rejected(
    scripts: ConfigScriptTools, config: dict[str, Any]
) -> pytest.ExceptionInfo[ToolError]:
    with pytest.raises(ToolError) as exc_info:
        await scripts.ha_config_set_script(
            script_id="s", config=config, MandatoryBPS=False
        )
    return exc_info


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
async def test_automation_item_wrapped_list_is_named(
    automations: AutomationConfigTools, key: str, wrapped: dict[str, Any]
) -> None:
    config = {"alias": "x", "triggers": [_TRIGGER], "actions": [_ACTION], key: wrapped}
    error = error_of(await _automation_rejected(automations, config))
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert f"'{key}'" in error["message"]
    assert '{"item": ...}' in error["message"]
    assert "JSON-encoded string" in json.dumps(error["suggestions"])


async def test_script_item_wrapped_sequence_is_named(
    scripts: ConfigScriptTools,
) -> None:
    exc_info = await _script_rejected(
        scripts, {"alias": "x", "sequence": {"item": [_ACTION]}}
    )
    error = error_of(exc_info)
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
async def test_valid_automation_configs_are_written_as_given(
    automations: AutomationConfigTools,
    core_client: HomeAssistantClient,
    config: dict[str, Any],
) -> None:
    expected = json.loads(json.dumps(config))
    result = await automations.ha_config_set_automation(
        config=config, MandatoryBPS=False
    )
    stored = await core_client.get_automation_config(result["entity_id"])
    assert {k: v for k, v in stored.items() if k != "id"} == expected


async def test_valid_script_with_todo_item_data_is_written(
    scripts: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
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
    await scripts.ha_config_set_script(
        script_id="s", config=dict(config), MandatoryBPS=False
    )
    stored = await core_client.get_script_config("s")
    assert stored["config"] == config


@pytest.fixture
async def button_form_schema(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[list[dict[str, Any]]]:
    """The form Core's template flow shows for a button, whose 'press' is an action selector."""
    assert await async_setup_component(hass, "config", {})
    menu = await core_client.start_config_flow("template")
    step = await core_client.submit_config_flow_step(
        menu["flow_id"], {"next_step_id": "button"}
    )
    try:
        schema: list[dict[str, Any]] = step["data_schema"]
        assert {"action": {}} in [field.get("selector") for field in schema]
        yield schema
    finally:
        await core_client.abort_config_flow(menu["flow_id"])


@pytest.mark.parametrize("press", [{"item": [_ACTION, _ACTION]}, ""])
def test_flow_action_selector_malformed_is_named(
    button_form_schema: list[dict[str, Any]], press: object
) -> None:
    config = {"name": "b", "press": press}
    with pytest.raises(ToolError) as exc_info:
        form_submission(button_form_schema, config, keep_shown=False)
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
def test_flow_valid_values_pass(
    button_form_schema: list[dict[str, Any]], config: dict[str, Any]
) -> None:
    expected = dict(config)
    assert form_submission(button_form_schema, config, keep_shown=False) == expected


# The singular root keys are checked too.
@pytest.mark.parametrize(
    "key", ["triggers", "conditions", "actions", "trigger", "condition"]
)
async def test_automation_empty_string_list_is_named(
    automations: AutomationConfigTools, key: str
) -> None:
    config = {"alias": "x", "triggers": [_TRIGGER], "actions": [_ACTION], key: ""}
    exc_info = await _automation_rejected(automations, config)
    assert f"'{key}' as \"\"" in error_of(exc_info)["message"]


async def test_script_empty_string_sequence_is_named(
    scripts: ConfigScriptTools,
) -> None:
    exc_info = await _script_rejected(scripts, {"alias": "x", "sequence": ""})
    assert "'sequence' as \"\"" in error_of(exc_info)["message"]


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


async def _stored_dashboard(
    hass: HomeAssistant, core_client: HomeAssistantClient, config: dict[str, Any]
) -> tuple[DashboardConfigTools, str]:
    """The dashboard tools and the config_hash of ``config`` saved in Core."""
    await store_dashboard(hass, core_client, config)
    tools = DashboardConfigTools(core_client)
    current = await tools.ha_config_get_dashboard(url_path=SEARCH_URL_PATH)
    return tools, current["config_hash"]


async def _dashboard_rejected(
    tools: DashboardConfigTools, **kwargs: Any
) -> pytest.ExceptionInfo[ToolError]:
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_dashboard(
            url_path=SEARCH_URL_PATH, MandatoryBPS=False, **kwargs
        )
    return exc_info


_DEEP_OP = {"op": "replace", "path": "/views/0/cards/0/cards", "value": {"item": [{}]}}


async def test_dashboard_patch_inside_stack_card_is_named(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tools, config_hash = await _stored_dashboard(
        hass, core_client, {"views": [{"cards": [{"type": "grid", "cards": []}]}]}
    )
    exc_info = await _dashboard_rejected(
        tools, patch=[_DEEP_OP], config_hash=config_hash
    )
    assert "'views[0].cards[0].cards'" in error_of(exc_info)["message"]


async def test_dashboard_patch_inside_custom_card_is_written(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tools, config_hash = await _stored_dashboard(
        hass, core_client, {"views": [{"cards": [{"type": "custom:x", "cards": []}]}]}
    )
    await tools.ha_config_set_dashboard(
        url_path=SEARCH_URL_PATH,
        patch=[_DEEP_OP],
        config_hash=config_hash,
        MandatoryBPS=False,
    )
    stored = await core_client.send_websocket_message(
        {"type": "lovelace/config", "url_path": SEARCH_URL_PATH}
    )
    assert stored["result"]["views"][0]["cards"][0]["cards"] == {"item": [{}]}


async def test_dashboard_python_transform_result_is_named(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tools, config_hash = await _stored_dashboard(
        hass, core_client, {"views": [{"cards": []}]}
    )
    exc_info = await _dashboard_rejected(
        tools, python_transform="config['views'] = ''", config_hash=config_hash
    )
    assert "python_transform produced 'views'" in error_of(exc_info)["message"]


async def test_automation_python_transform_result_is_named(
    automations: AutomationConfigTools,
) -> None:
    created = await automations.ha_config_set_automation(
        config={"alias": "x", "triggers": [_TRIGGER], "actions": [_ACTION]},
        MandatoryBPS=False,
    )
    current = await automations.ha_config_get_automation(entity_id=created["entity_id"])
    with pytest.raises(ToolError) as exc_info:
        await automations.ha_config_set_automation(
            entity_id=created["entity_id"],
            config_hash=current["config_hash"],
            python_transform="config['actions'] = ''",
            MandatoryBPS=False,
        )
    assert "python_transform produced 'actions'" in error_of(exc_info)["message"]


async def test_script_python_transform_result_is_named(
    scripts: ConfigScriptTools,
) -> None:
    await scripts.ha_config_set_script(
        script_id="s",
        config={"alias": "x", "sequence": [_ACTION]},
        MandatoryBPS=False,
    )
    current = await scripts.ha_config_get_script(script_id="s")
    with pytest.raises(ToolError) as exc_info:
        await scripts.ha_config_set_script(
            script_id="s",
            config_hash=current["config_hash"],
            python_transform="config['sequence'] = {'item': []}",
            MandatoryBPS=False,
        )
    assert "python_transform produced 'sequence'" in error_of(exc_info)["message"]


async def test_automation_multiple_malformed_fields_are_all_named(
    automations: AutomationConfigTools,
) -> None:
    config = {"alias": "x", "triggers": {"item": _TRIGGER}, "actions": ""}
    body = error_body(await _automation_rejected(automations, config))
    assert (
        "'triggers' as {\"item\": ...}; 'actions' as \"\"" in body["error"]["message"]
    )
    assert body["malformed_list_fields"] == {
        "triggers": '{"item": ...}',
        "actions": '""',
    }


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
