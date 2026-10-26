"""User-visible MCP behavior across the supported deployment modes."""

import asyncio
from copy import deepcopy
from typing import Any

import pytest

from ..utilities.assertions import extract_error_message
from ._support import Scenario, public_scenario

pytestmark = pytest.mark.asyncio


async def test_rejected_stale_automation_cannot_replace_or_run_the_accepted_edit(
    mcp_client: Any,
) -> None:
    """A second editor's real stale snapshot must not overwrite or execute work."""
    async with public_scenario(mcp_client) as case:
        target = await case.helper("input_text", "idle")
        identifier = case.unique_name()
        case.remove_after("ha_config_remove_automation", identifier=identifier)
        await case.mcp.call_tool_success(
            "ha_config_set_automation",
            {
                "identifier": identifier,
                "config": {
                    "alias": identifier,
                    "triggers": [
                        {"trigger": "event", "event_type": case.unique_name()}
                    ],
                    "actions": [
                        {
                            "action": "input_text.set_value",
                            "target": {"entity_id": target},
                            "data": {"value": "original"},
                        }
                    ],
                },
            },
        )
        old = await case.mcp.call_tool_success(
            "ha_config_get_automation", {"identifier": identifier}
        )
        accepted = await case.mcp.call_tool_success(
            "ha_config_set_automation",
            {
                "identifier": identifier,
                "config_hash": old["config_hash"],
                "python_transform": "config['actions'][0]['data']['value'] = 'accepted'",
            },
        )
        current = await case.mcp.call_tool_success(
            "ha_config_get_automation", {"identifier": identifier}
        )
        assert accepted["config_hash"] == current["config_hash"]
        assert current["config_hash"] != old["config_hash"]
        assert current["config"]["actions"][0]["data"]["value"] == "accepted"

        stale_config = deepcopy(old["config"])
        stale_config["actions"][0]["data"]["value"] = "rejected"
        refused = await case.mcp.call_tool_failure(
            "ha_config_set_automation",
            {
                "identifier": identifier,
                "config_hash": old["config_hash"],
                "config": stale_config,
                "run_actions": True,
            },
        )
        assert "modified since last read" in extract_error_message(refused).lower()
        after_refusal = await case.mcp.call_tool_success(
            "ha_config_get_automation", {"identifier": identifier}
        )
        assert after_refusal["config"] == current["config"]
        assert after_refusal["config_hash"] == current["config_hash"]
        unchanged = await case.mcp.call_tool_success(
            "ha_get_state", {"entity_id": target}
        )
        assert unchanged["data"]["state"] == "idle"

        await case.mcp.call_tool_success(
            "ha_config_set_automation", {"identifier": identifier, "run_actions": True}
        )
        await case.state(target, "accepted")


def _responding_script(*, parallel: bool = False) -> dict[str, Any]:
    return {
        "alias": "Continuity response script",
        "mode": "parallel" if parallel else "single",
        "sequence": [
            *(
                [
                    {
                        "action": "input_boolean.turn_on",
                        "target": {"entity_id": "{{ ready_entity }}"},
                    },
                    {
                        # Readiness stays on until cleanup, even if the peer finishes.
                        "wait_template": "{{ is_state(peer_ready_entity, 'on') }}",
                        "timeout": {"seconds": 10},
                        "continue_on_timeout": False,
                    },
                ]
                if parallel
                else []
            ),
            {
                "action": "input_text.set_value",
                "target": {"entity_id": "{{ target_entity }}"},
                "data": {"value": "{{ requested_value }}"},
            },
            {
                "variables": {
                    "receipt": {
                        "target": "{{ target_entity }}",
                        "value": "{{ requested_value }}",
                        "details": {"values": [0, False, ""], "empty": {}},
                    }
                }
            },
            {"stop": "Return the invocation receipt", "response_variable": "receipt"},
        ],
    }


async def _call_response(
    case: Scenario,
    script_id: str,
    target: str,
    value: str,
    *,
    ready_entity: str | None = None,
    peer_ready_entity: str | None = None,
) -> dict[str, Any]:
    data = {"target_entity": target, "requested_value": value}
    if ready_entity is not None:
        data["ready_entity"] = ready_entity
    if peer_ready_entity is not None:
        data["peer_ready_entity"] = peer_ready_entity
    return await case.mcp.call_tool_success(
        "ha_call_service",
        {
            "domain": "script",
            "service": script_id,
            "data": data,
            "return_response": True,
            "result_fields": ["entity_id", "state"],
        },
    )


def _assert_receipt(result: dict[str, Any], target: str, value: str) -> None:
    assert result["service_response"] == {
        "target": target,
        "value": value,
        "details": {"values": [0, False, ""], "empty": {}},
    }
    values = result["service_response"]["details"]["values"]
    assert type(values[0]) is int
    assert values[1] is False


async def test_service_response_keeps_payload_and_projected_mutation_together(
    mcp_client: Any,
) -> None:
    """A mutating response service must expose its state change and full payload."""
    async with public_scenario(mcp_client) as case:
        target = await case.helper("input_text", "idle")
        script_id = await case.script(_responding_script())
        result = await _call_response(case, script_id, target, "committed")

        _assert_receipt(result, target, "committed")
        records = result["result"]
        assert isinstance(records, list)
        assert {"entity_id": target, "state": "committed"} in records
        assert all(set(record) <= {"entity_id", "state"} for record in records)
        await case.state(target, "committed")


async def test_concurrent_script_calls_keep_each_response_with_its_own_effect(
    mcp_client: Any,
) -> None:
    """Concurrent calls must not mix invocation variables, responses or targets."""
    async with public_scenario(mcp_client) as case:
        left = await case.helper("input_text", "idle")
        right = await case.helper("input_text", "idle")
        left_ready = await case.helper("input_boolean", False)
        right_ready = await case.helper("input_boolean", False)
        script_id = await case.script(_responding_script(parallel=True))

        results = await asyncio.gather(
            _call_response(
                case,
                script_id,
                left,
                "left invocation",
                ready_entity=left_ready,
                peer_ready_entity=right_ready,
            ),
            _call_response(
                case,
                script_id,
                right,
                "right invocation",
                ready_entity=right_ready,
                peer_ready_entity=left_ready,
            ),
            return_exceptions=True,
        )
        # Join both invocations before cleanup even when one call fails.
        for result, target, value in zip(
            results,
            (left, right),
            ("left invocation", "right invocation"),
            strict=True,
        ):
            assert isinstance(result, dict), f"Concurrent MCP call failed: {result!r}"
            _assert_receipt(result, target, value)
            await case.state(target, value)


async def _turn_on_label(case: Scenario, label_id: str) -> None:
    await case.mcp.call_tool_success(
        "ha_call_service",
        {
            "domain": "input_boolean",
            "service": "turn_on",
            "data": {"label_id": label_id},
        },
    )


async def test_label_reassignment_changes_service_targets_without_stale_members(
    mcp_client: Any,
) -> None:
    """Clearing and reassigning a label must change which entities a call controls."""
    async with public_scenario(mcp_client) as case:
        removed = await case.helper("input_boolean", False)
        retained = await case.helper("input_boolean", False)
        added = await case.helper("input_boolean", False)
        label = await case.mcp.call_tool_success(
            "ha_config_set_label", {"name": case.unique_name()}
        )
        label_id = label["label_id"]
        case.remove_after("ha_config_remove_label", label_id=label_id)
        await case.mcp.call_tool_success(
            "ha_set_entity", {"entity_id": [removed, retained], "labels": [label_id]}
        )
        await _turn_on_label(case, label_id)
        await case.state(removed, "on")
        await case.state(retained, "on")
        await case.state(added, "off")

        await case.mcp.call_tool_success(
            "ha_call_service",
            {
                "domain": "input_boolean",
                "service": "turn_off",
                "entity_id": ",".join([removed, retained, added]),
                "wait": False,
            },
        )
        for entity_id in (removed, retained, added):
            await case.state(entity_id, "off")
        await case.mcp.call_tool_success(
            "ha_set_entity", {"entity_id": removed, "labels": []}
        )
        await case.mcp.call_tool_success(
            "ha_set_entity", {"entity_id": added, "labels": [label_id]}
        )
        for entity_id, expected in (
            (removed, []),
            (retained, [label_id]),
            (added, [label_id]),
        ):
            readback = await case.mcp.call_tool_success(
                "ha_get_entity", {"entity_id": entity_id}
            )
            assert readback["entity_entry"]["labels"] == expected

        await _turn_on_label(case, label_id)
        await case.state(retained, "on")
        await case.state(added, "on")
        spared = await case.mcp.call_tool_success(
            "ha_get_state", {"entity_id": removed}
        )
        assert spared["data"]["state"] == "off"
