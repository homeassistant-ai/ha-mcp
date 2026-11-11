"""``ha_get_automation_traces`` detail over traces Core itself recorded.

Each automation or script below runs in a real ``hass``; the tool lists its
traces and formats the run Core stored, so every shape it reads is Core's own.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_traces import TraceTools

AUTOMATIONS: list[dict[str, Any]] = [
    {
        "id": "flat",
        "alias": "Flat",
        "triggers": [{"trigger": "event", "event_type": "run_flat"}],
        "conditions": [{"condition": "template", "value_template": "{{ true }}"}],
        "actions": [
            {
                "action": "input_boolean.turn_on",
                "target": {"entity_id": "input_boolean.lamp"},
            },
            {"action": "script.child"},
        ],
    },
    {
        "id": "door",
        "alias": "Door",
        "triggers": [
            {
                "trigger": "template",
                "value_template": "{{ is_state('binary_sensor.door', 'on') }}",
            }
        ],
        "actions": [],
    },
    {
        "id": "appears",
        "alias": "Appears",
        "triggers": [
            {
                "trigger": "template",
                "value_template": "{{ is_state('sensor.appears', 'on') }}",
            }
        ],
        "actions": [],
    },
    {
        "id": "counting",
        "alias": "Counting",
        "triggers": [{"trigger": "event", "event_type": "run_counting"}],
        "actions": [
            {"variables": {"counter": 1}},
            {
                "action": "input_boolean.toggle",
                "target": {"entity_id": "input_boolean.lamp"},
            },
            {"variables": {"counter": 2}},
        ],
    },
    {
        "id": "broken_condition",
        "alias": "Broken condition",
        "triggers": [{"trigger": "event", "event_type": "run_broken_condition"}],
        "conditions": [{"condition": "template", "value_template": "{{ 1 / 0 }}"}],
        "actions": [],
    },
    {
        "id": "false_condition",
        "alias": "False condition",
        "triggers": [{"trigger": "event", "event_type": "run_false_condition"}],
        "conditions": [{"condition": "template", "value_template": "{{ false }}"}],
        "actions": [],
    },
]

SCRIPTS: dict[str, Any] = {
    "child": {
        "sequence": [
            {
                "action": "input_boolean.turn_off",
                "target": {"entity_id": "input_boolean.lamp"},
            }
        ]
    },
    "looping": {
        "sequence": [
            {
                "repeat": {
                    "count": 1,
                    "sequence": [
                        {
                            "action": "input_boolean.toggle",
                            "target": {"entity_id": "input_boolean.lamp"},
                        }
                    ],
                }
            },
            {
                "action": "input_boolean.turn_on",
                "target": {"entity_id": "input_boolean.lamp"},
            },
        ]
    },
}


@pytest.fixture
async def tools(hass: HomeAssistant, core_client: HomeAssistantClient) -> TraceTools:
    """The trace tool on a ``hass`` running the automations and scripts above."""
    hass.states.async_set("binary_sensor.door", "off")
    # ``config`` serves the entity-registry read that finds a trace's item id.
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"lamp": {}}}
    )
    assert await async_setup_component(hass, "script", {"script": SCRIPTS})
    assert await async_setup_component(hass, "automation", {"automation": AUTOMATIONS})
    await hass.async_block_till_done()
    return TraceTools(core_client)


async def _latest_run(
    tools: TraceTools, entity_id: str, **detail: Any
) -> dict[str, Any]:
    """The tool's detail for the newest run Core stored for ``entity_id``."""
    listing = await tools.ha_get_automation_traces(automation_id=entity_id)
    assert listing["traces"], listing
    return await tools.ha_get_automation_traces(
        automation_id=entity_id, run_id=listing["traces"][0]["run_id"], **detail
    )


async def test_trace_reports_trigger_and_actions_in_execution_order(
    hass: HomeAssistant, tools: TraceTools
) -> None:
    hass.bus.async_fire("run_flat")
    await hass.async_block_till_done()

    result = await _latest_run(tools, "automation.flat")

    assert result["success"] is True
    assert result["automation_id"] == "automation.flat"
    assert result["trigger"]["platform"] == "event"
    actions = result["action_trace"]
    assert [action["path"] for action in actions] == ["action/0", "action/1"]
    assert actions[0]["result"]["params"]["service"] == "turn_on"
    assert actions[1]["child_id"]["item_id"] == "child"


async def test_template_trigger_states_are_reported_as_state_strings(
    hass: HomeAssistant, tools: TraceTools
) -> None:
    hass.states.async_set("binary_sensor.door", "on")
    await hass.async_block_till_done()

    result = await _latest_run(tools, "automation.door")

    assert result["trigger"]["platform"] == "template"
    assert result["trigger"]["entity_id"] == "binary_sensor.door"
    assert result["trigger"]["from_state"] == "off"
    assert result["trigger"]["to_state"] == "on"


async def test_template_trigger_without_a_previous_state_keeps_it_null(
    hass: HomeAssistant, tools: TraceTools
) -> None:
    """The entity did not exist before the change that fired the trigger."""
    hass.states.async_set("sensor.appears", "on")
    await hass.async_block_till_done()

    result = await _latest_run(tools, "automation.appears")

    assert "from_state" in result["trigger"]
    assert result["trigger"]["from_state"] is None
    assert result["trigger"]["to_state"] == "on"


async def test_variables_appear_at_the_steps_that_changed_them(
    hass: HomeAssistant, tools: TraceTools
) -> None:
    hass.bus.async_fire("run_counting")
    await hass.async_block_till_done()

    actions = (await _latest_run(tools, "automation.counting"))["action_trace"]

    # Core adds the run's context to the first step's variables as well.
    counters = [(action.get("variables") or {}).get("counter") for action in actions]
    assert counters == [1, None, 2]


async def test_script_steps_are_reported_as_actions(
    hass: HomeAssistant, tools: TraceTools
) -> None:
    await hass.services.async_call("script", "looping", blocking=True)

    result = await _latest_run(tools, "script.looping")

    paths = {action["path"] for action in result["action_trace"]}
    assert {"sequence/0", "sequence/0/repeat/sequence/0", "sequence/1"} <= paths


async def test_condition_step_error_surfaced(
    hass: HomeAssistant, tools: TraceTools
) -> None:
    """HA 2026.7 always records template errors on the failing step —
    the condition formatter must not drop them."""
    hass.bus.async_fire("run_broken_condition")
    await hass.async_block_till_done()

    result = await _latest_run(tools, "automation.broken_condition")

    assert result["condition_results"][0]["error"]


async def test_condition_step_without_error_has_no_error_key(
    hass: HomeAssistant, tools: TraceTools
) -> None:
    hass.bus.async_fire("run_false_condition")
    await hass.async_block_till_done()

    result = await _latest_run(tools, "automation.false_condition")

    assert result["condition_results"][0]["result"] is False
    assert "error" not in result["condition_results"][0]


async def test_detailed_adds_the_run_context(
    hass: HomeAssistant, tools: TraceTools
) -> None:
    hass.bus.async_fire("run_flat")
    await hass.async_block_till_done()

    default = await _latest_run(tools, "automation.flat")
    detailed = await _latest_run(tools, "automation.flat", detailed=True)

    assert "context" not in default
    assert detailed["context"]["id"]


_SECTION_KEYS = (
    "trigger",
    "condition_results",
    "action_trace",
    "config_summary",
    "context",
)
_METADATA_KEYS = ("success", "automation_id", "run_id", "timestamp", "state")


@pytest.mark.parametrize(
    ("sections", "kept"),
    [
        pytest.param("actions", {"action_trace"}, id="single"),
        pytest.param(
            "trigger,conditions", {"trigger", "condition_results"}, id="comma-separated"
        ),
        pytest.param("not_a_real_section", set(), id="unknown-dropped"),
        pytest.param(
            " Actions , Trigger ", {"action_trace", "trigger"}, id="whitespace-and-case"
        ),
    ],
)
async def test_sections_keep_only_the_requested_parts_and_the_metadata(
    hass: HomeAssistant, tools: TraceTools, sections: str, kept: set[str]
) -> None:
    hass.bus.async_fire("run_flat")
    await hass.async_block_till_done()
    full = await _latest_run(tools, "automation.flat", detailed=True)
    assert set(_SECTION_KEYS) <= set(full), full

    result = await _latest_run(
        tools, "automation.flat", detailed=True, sections=sections
    )

    assert {key for key in _SECTION_KEYS if key in result} == kept
    assert all(key in result for key in _METADATA_KEYS)
