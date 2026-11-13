"""The input guards ha_config_set_automation applies itself.

Core validates the automation (required keys, root forms, trigger and
condition keys); these are HA-MCP's own checks, which reject before anything is
written: the script misroute hint, the runtime-only ``enabled`` key, the
scene.create misroute, the duplicate-id guard and the JSON parse error. A config
they let through is written to Core.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

from ._config_writes import automation_tools
from ._tool_errors import error_body, error_of

_TRIGGER = {"trigger": "state", "entity_id": "input_boolean.guest_mode", "to": "on"}


@pytest.fixture
async def tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AutomationConfigTools:
    return await automation_tools(hass, core_client, monkeypatch)


async def _rejected(
    tools: AutomationConfigTools, config: Any, **kwargs: Any
) -> pytest.ExceptionInfo[ToolError]:
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_automation(
            config=config, MandatoryBPS=False, **kwargs
        )
    return exc_info


async def _written(
    tools: AutomationConfigTools, core_client: HomeAssistantClient, config: dict
) -> dict[str, Any]:
    """Create the automation and return what Core stored for it."""
    result = await tools.ha_config_set_automation(config=config, MandatoryBPS=False)
    assert result["success"] is True, result
    stored: dict[str, Any] = await core_client.get_automation_config(
        result["entity_id"]
    )
    return stored


async def test_invalid_json_string_suggests_dict(tools: AutomationConfigTools) -> None:
    """A config sent as broken JSON text suggests passing it as a dict."""
    exc_info = await _rejected(tools, '{alias: "x", "triggers": []}')

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_JSON"
    assert "dict" in json.dumps(error)


async def test_config_core_accepts_is_written_without_an_alias(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """Core names an automation without an alias itself; refusing it here rejected a config Home Assistant accepts."""
    stored = await _written(tools, core_client, {"triggers": [_TRIGGER], "actions": []})

    assert stored["triggers"] == [_TRIGGER]


async def test_sequence_in_config_hints_at_set_script(
    tools: AutomationConfigTools,
) -> None:
    """A config with 'sequence' and no actions hints at ha_config_set_script."""
    exc_info = await _rejected(
        tools, {"alias": "Goodnight", "sequence": [{"action": "light.turn_off"}]}
    )

    error = error_of(exc_info)
    assert error["code"] == "CONFIG_MISSING_REQUIRED_FIELDS"
    # Primary suggestion must name the correct tool.
    assert "ha_config_set_script" in error.get("suggestion", "")
    all_text = json.dumps(error)
    assert "sequence" in all_text
    assert "add 'triggers'" in all_text


async def test_sequence_with_trigger_but_no_action_still_hints(
    tools: AutomationConfigTools,
) -> None:
    """Sequence + triggers but no actions still gives the script hint, without asking for triggers."""
    exc_info = await _rejected(
        tools,
        {"alias": "x", "triggers": [], "sequence": [{"action": "light.turn_off"}]},
    )

    error = error_of(exc_info)
    assert "ha_config_set_script" in error.get("suggestion", "")
    assert "add 'triggers'" not in json.dumps(error)


@pytest.mark.parametrize("actions_key", ["actions", "action"])
async def test_sequence_beside_actions_is_left_to_core(
    tools: AutomationConfigTools, core_client: HomeAssistantClient, actions_key: str
) -> None:
    """With actions present the config is not script-shaped; Core judges the stray 'sequence' key."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_automation(
            config={
                "triggers": [_TRIGGER],
                "sequence": [],
                actions_key: [{"action": "light.turn_off"}],
            },
            MandatoryBPS=False,
        )

    error = error_of(exc_info)
    assert "ha_config_set_script" not in json.dumps(error)
    assert "sequence" in error["message"]


async def test_sequence_and_enabled_report_together(
    tools: AutomationConfigTools,
) -> None:
    """A script-shaped config that also misplaces 'enabled' reports both violations in one rejection."""
    exc_info = await _rejected(
        tools, {"sequence": [{"action": "light.turn_on"}], "enabled": True}
    )

    body = error_body(exc_info)
    assert body["invalid_key"] == "enabled"
    error = body["error"]
    all_text = json.dumps(error)
    assert "ha_config_set_script" in all_text
    assert "runtime-only" in all_text
    # The two guidance sentences are separated, not run together.
    assert ". 'enabled'" in error["message"]
    suggestions = error.get("suggestions", [])
    assert any("Remove 'enabled' from config" in s for s in suggestions)
    assert "Use enabled=None to leave the current runtime state unchanged" in (
        suggestions
    )


async def test_sequence_misroute_on_transform_points_at_transform(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """A transform producing 'sequence' gets transform-expression guidance, not config={...} (which write modes would reject)."""
    created = await tools.ha_config_set_automation(
        config={"alias": "Night", "triggers": [_TRIGGER], "actions": []},
        MandatoryBPS=False,
    )
    current = await tools.ha_config_get_automation(entity_id=created["entity_id"])

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_automation(
            entity_id=created["entity_id"],
            config_hash=current["config_hash"],
            python_transform=(
                "del config['actions']; "
                "config['sequence'] = [{'action': 'light.turn_off'}]"
            ),
            MandatoryBPS=False,
        )

    error = error_of(exc_info)
    assert error["code"] == "CONFIG_MISSING_REQUIRED_FIELDS"
    all_text = json.dumps(error)
    assert "python_transform expression" in all_text
    assert "config={" not in all_text


async def test_enabled_in_config_alone_still_rejected(
    tools: AutomationConfigTools,
) -> None:
    """The runtime-only key is a tool parameter, not config."""
    exc_info = await _rejected(
        tools, {"alias": "x", "triggers": [], "actions": [], "enabled": True}
    )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "runtime-only" in error["message"]


async def test_config_id_without_entity_id_is_refused_as_a_duplicate(
    tools: AutomationConfigTools,
) -> None:
    """Creating with another automation's id would write a second one."""
    exc_info = await _rejected(
        tools, {"id": "1700000000000", "triggers": [], "actions": []}
    )

    assert "entity_id='1700000000000'" in json.dumps(error_of(exc_info))


def _assert_routed_to_scene_tools(exc_info: pytest.ExceptionInfo[ToolError]) -> None:
    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    # Pinned to ``suggestions`` so a stray reference elsewhere in the body
    # would not satisfy it.
    suggestions_blob = " ".join(error.get("suggestions") or [])
    assert "ha_config_set_scene" in suggestions_blob
    assert "ha_config_set_helper" in suggestions_blob


@pytest.mark.parametrize("key", ["service", "action"])
async def test_empty_trigger_with_scene_create_is_routed_to_scene_tools(
    tools: AutomationConfigTools, key: str
) -> None:
    """An automation that can never fire and only snapshots a scene is pointed at the scene and helper tools (#1169)."""
    exc_info = await _rejected(
        tools,
        {
            "alias": "Movie Snapshot",
            "triggers": [],
            "actions": [
                {
                    key: "scene.create",
                    "data": {
                        "scene_id": "movie_night",
                        "snapshot_entities": ["light.living_room"],
                    },
                }
            ],
        },
    )

    assert "scene.create" in error_of(exc_info)["message"]
    _assert_routed_to_scene_tools(exc_info)


@pytest.mark.parametrize(
    "config",
    [
        # Written as given: singular root keys reach the gate unchanged.
        {"trigger": [], "action": [{"action": "scene.create"}]},
        # ``triggers: null`` counts as an empty trigger, like ``[]``.
        {"triggers": None, "actions": [{"service": "scene.create"}]},
        # A single action object, not a list.
        {"triggers": [], "actions": {"service": "scene.create"}},
    ],
    ids=["singular-keys", "null-trigger", "single-action-object"],
)
async def test_scene_create_misroute_in_other_shapes(
    tools: AutomationConfigTools, config: dict[str, Any]
) -> None:
    """The misroute is caught however the empty trigger and the actions are written."""
    _assert_routed_to_scene_tools(await _rejected(tools, config))


@pytest.mark.parametrize(
    "wrapper_action",
    [
        {"sequence": [{"service": "scene.create"}]},
        {"parallel": [{"service": "scene.create"}]},
        {"choose": [{"conditions": [], "sequence": [{"service": "scene.create"}]}]},
        {
            "choose": [{"conditions": [], "sequence": [{"service": "noop"}]}],
            "default": [{"service": "scene.create"}],
        },
        {"if": [{"condition": "state"}], "then": [{"service": "scene.create"}]},
        {"if": [{"condition": "state"}], "else": [{"service": "scene.create"}]},
        {
            "choose": [
                {
                    "conditions": [],
                    "sequence": [{"sequence": [{"action": "scene.create"}]}],
                }
            ]
        },
    ],
    ids=[
        "sequence",
        "parallel",
        "choose-option-sequence",
        "choose-default",
        "if-then",
        "if-else",
        "deep-nested",
    ],
)
async def test_nested_scene_create_caught(
    tools: AutomationConfigTools, wrapper_action: dict[str, Any]
) -> None:
    """``scene.create`` nested under Core's wrapper actions is caught like the top-level case."""
    exc_info = await _rejected(
        tools, {"alias": "Nested", "triggers": [], "actions": [wrapper_action]}
    )

    _assert_routed_to_scene_tools(exc_info)


async def test_empty_trigger_scene_create_indices_in_context(
    tools: AutomationConfigTools,
) -> None:
    """Multi-action lists name the indices of the scene.create actions."""
    exc_info = await _rejected(
        tools,
        {
            "alias": "Multi",
            "triggers": [],
            "actions": [
                {"service": "light.turn_on"},
                {"service": "scene.create", "data": {"scene_id": "a"}},
                {"action": "scene.create", "data": {"scene_id": "b"}},
            ],
        },
    )

    assert error_body(exc_info).get("scene_create_action_indices") == [1, 2]


async def test_empty_trigger_draft_is_written(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """A draft with no trigger yet and an ordinary action is not the misroute and is saved."""
    action = {"action": "light.turn_on", "target": {"entity_id": "light.x"}}

    stored = await _written(
        tools, core_client, {"alias": "Draft", "triggers": [], "actions": [action]}
    )

    assert stored["actions"] == [action]


async def test_triggered_scene_snapshot_is_written(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """A scene snapshot taken when a trigger fires is a real use and is saved."""
    stored = await _written(
        tools,
        core_client,
        {
            "alias": "Snapshot on guest mode",
            "triggers": [_TRIGGER],
            "actions": [
                {"action": "scene.create", "data": {"scene_id": "before_guests"}}
            ],
        },
    )

    assert stored["actions"][0]["action"] == "scene.create"


async def test_use_blueprint_empty_lists_are_stripped(
    hass: HomeAssistant,
    tools: AutomationConfigTools,
    core_client: HomeAssistantClient,
) -> None:
    """Core merges a blueprint instance's keys over the rendered blueprint, so an
    empty list sent with ``use_blueprint`` would erase the blueprint's own
    triggers or actions. Either root form is stripped before the write."""
    await hass.async_block_till_done()  # Core copies its own blueprints in.

    stored = await _written(
        tools,
        core_client,
        {
            "alias": "Motion Light",
            "use_blueprint": {
                "path": "homeassistant/motion_light.yaml",
                "input": {
                    "motion_entity": "binary_sensor.motion",
                    "light_target": {"entity_id": "light.kitchen"},
                },
            },
            "triggers": [],
            "action": [],
        },
    )

    assert "use_blueprint" in stored
    assert "triggers" not in stored
    assert "action" not in stored
