"""``ha_bulk_control`` operations mode: which rows run, which are skipped, and
what each one reports.

The tool runs on the server's own client against a real ``hass``; the lights
are devices below Core's own ``light`` integration (``_switched_states``), so
every call and state is Core's. Without the component every valid row is its
own service call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.errors import ErrorCode
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_service import register_service_tools

from ._operations import operations_without_listener as operations_without_listener
from ._switched_states import Devices, Received, set_up_devices


@pytest.fixture
async def devices(hass: HomeAssistant) -> Devices:
    lights = ("light.valid", "light.kitchen", "light.ok", "light.also_ok")
    return await set_up_devices(hass, dict.fromkeys(lights, "off"))


async def _rejected_batch(
    client: HomeAssistantClient, operations: list[Any]
) -> dict[str, Any]:
    """The structured payload of a batch the tool refused as a whole."""
    with pytest.raises(ToolError) as exc_info:
        await DeviceControlTools(client).bulk_device_control(operations)
    payload: dict[str, Any] = json.loads(str(exc_info.value))
    assert payload["success"] is False
    return payload


async def _run_tool(
    client: HomeAssistantClient, operations: list[Any]
) -> dict[str, Any]:
    """Call the registered ``ha_bulk_control`` as an MCP client would."""
    mcp = FastMCP("test")
    register_service_tools(mcp, client, device_tools=DeviceControlTools(client))
    tool = await mcp.get_tool("ha_bulk_control")
    result = await tool.run({"operations": operations})
    data: dict[str, Any] = result.structured_content or {}
    return data


# --- a batch with nothing valid is refused ------------------------------------


async def test_an_empty_batch_is_refused(core_client: HomeAssistantClient) -> None:
    payload = await _rejected_batch(core_client, [])

    assert "No operations provided" in payload["error"]["message"]


@pytest.mark.parametrize(
    ("operation", "named"),
    [
        pytest.param({"action": "on"}, ["entity_id"], id="no-entity-id"),
        pytest.param({"entity_id": "light.valid"}, ["action"], id="no-action"),
        pytest.param({}, ["entity_id", "action"], id="neither"),
    ],
)
async def test_an_all_invalid_batch_dispatches_nothing_and_names_the_missing_fields(
    core_client: HomeAssistantClient,
    devices: Devices,
    operation: dict[str, Any],
    named: list[str],
) -> None:
    payload = await _rejected_batch(core_client, [operation])

    assert "All 1 operation(s) failed validation" in payload["error"]["message"]
    (detail,) = payload["skipped_details"]
    assert detail["index"] == 0
    assert all(field in detail["error"]["message"] for field in named)
    assert devices.calls == []


async def test_rows_that_are_not_objects_are_each_reported(
    core_client: HomeAssistantClient,
) -> None:
    payload = await _rejected_batch(core_client, ["not a dict", 123, None])

    assert "All 3 operation(s) failed validation" in payload["error"]["message"]
    assert [d["index"] for d in payload["skipped_details"]] == [0, 1, 2]
    assert all(
        "not a dict" in d["error"]["message"] for d in payload["skipped_details"]
    )


async def test_a_refused_batch_tells_the_caller_the_row_shape(
    core_client: HomeAssistantClient,
) -> None:
    payload = await _rejected_batch(core_client, [{"action": "on"}])

    suggestions = payload["error"]["suggestions"]
    assert any("entity_id" in s for s in suggestions)
    assert any("action" in s for s in suggestions)
    assert any("service='turn_off'" in s for s in suggestions)


async def test_a_skipped_row_is_echoed_back_as_sent(
    core_client: HomeAssistantClient,
) -> None:
    original_op = {"action": "on", "parameters": {"brightness": 100}}

    payload = await _rejected_batch(core_client, [original_op])

    assert payload["skipped_details"][0]["operation"] == original_op


# --- invalid rows are skipped, valid ones still run ---------------------------


async def test_invalid_rows_are_skipped_while_the_valid_row_runs(
    core_client: HomeAssistantClient, devices: Devices
) -> None:
    result = await DeviceControlTools(core_client).bulk_device_control(
        [
            {"entity_id": "light.valid", "action": "on"},
            {"action": "off"},
            {"entity_id": "switch.test"},
        ]
    )

    assert (result["total_operations"], result["successful_commands"]) == (3, 1)
    assert result["skipped_operations"] == 2
    assert [d["index"] for d in result["skipped_details"]] == [1, 2]
    assert devices.targets() == ["light.valid"]


async def test_a_service_style_row_is_skipped_without_rejecting_the_batch(
    core_client: HomeAssistantClient, devices: Devices
) -> None:
    result = await DeviceControlTools(core_client).bulk_device_control(
        [
            {"entity_id": "light.invalid", "action": "off", "service": "turn_off"},
            {"entity_id": "light.valid", "action": "off"},
        ]
    )

    assert (result["skipped_operations"], result["successful_commands"]) == (1, 1)
    (detail,) = result["skipped_details"]
    assert detail["index"] == 0
    assert "service" in detail["error"]["message"]
    assert devices.targets() == ["light.valid"]


# --- the registered tool: schema failures stay per row ------------------------


async def test_a_service_style_row_sent_to_the_tool_is_skipped_and_the_rest_run(
    core_client: HomeAssistantClient, devices: Devices
) -> None:
    data = await _run_tool(
        core_client,
        [
            {"entity_id": "light.invalid", "action": "off", "service": "turn_off"},
            {"entity_id": "light.valid", "action": "off"},
        ],
    )

    assert (data["successful_commands"], data["skipped_operations"]) == (1, 1)
    assert "service" in data["skipped_details"][0]["error"]["message"]
    assert devices.targets() == ["light.valid"]


async def test_a_malformed_row_never_writes_its_values_to_the_log(
    core_client: HomeAssistantClient,
    devices: Devices,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        await _run_tool(
            core_client,
            [
                {"entity_id": "light.valid", "action": "off"},
                {
                    "entity_id": "light.alarm",
                    "action": "off",
                    "service_data": {"code": "SENTINEL-SECRET-1234"},
                },
            ],
        )

    assert "failed schema validation" in caplog.text
    assert "SENTINEL-SECRET-1234" not in caplog.text


async def test_every_malformed_row_is_reported_without_aborting_the_batch(
    core_client: HomeAssistantClient, devices: Devices
) -> None:
    data = await _run_tool(
        core_client,
        [
            {"entity_id": "", "action": "off"},
            {
                "entity_id": "light.negative_timeout",
                "action": "off",
                "timeout_seconds": -0.5,
            },
            {
                "entity_id": "light.bad_parameters",
                "action": "on",
                "parameters": "{not-json",
            },
            {
                "entity_id": "light.null_timeout",
                "action": "off",
                "timeout_seconds": None,
            },
            {
                "entity_id": "light.null_validation",
                "action": "off",
                "validate_first": None,
            },
            {"entity_id": "light.valid", "action": "off"},
            {
                "entity_id": "light.integer_validation",
                "action": "off",
                "validate_first": 1,
            },
            {
                "entity_id": "light.string_validation",
                "action": "off",
                "validate_first": "false",
            },
            # float(True) is 1.0: a bool must not become a one-second timeout.
            {
                "entity_id": "light.bool_timeout",
                "action": "off",
                "timeout_seconds": True,
            },
            # The advertised schema is strict: no coercing a numeric string.
            {
                "entity_id": "light.string_timeout",
                "action": "off",
                "timeout_seconds": "10",
            },
            # Present-but-null parameters are malformed, in both spellings.
            {"entity_id": "light.null_parameters", "action": "off", "parameters": None},
            {
                "entity_id": "light.json_null_parameters",
                "action": "off",
                "parameters": "null",
            },
            # float() of an int this size raises OverflowError.
            {
                "entity_id": "light.oversized_timeout",
                "action": "off",
                "timeout_seconds": 10**400,
            },
            # json.loads raises RecursionError past the interpreter depth limit.
            {
                "entity_id": "light.deep_parameters",
                "action": "off",
                "parameters": "[" * 200_000 + "]" * 200_000,
            },
            # A non-string key must not make sorted()/join() abort the batch.
            {"entity_id": "light.int_key", "action": "off", 1: "x"},
        ],
    )

    assert (data["successful_commands"], data["skipped_operations"]) == (1, 14)
    assert [d["index"] for d in data["skipped_details"]] == [
        *range(5),
        *range(6, 15),
    ]
    messages = [d["error"]["message"] for d in data["skipped_details"]]
    assert any("required fields" in m for m in messages)
    # The decoder's reason and offset show the sender where its string broke.
    json_message = next(m for m in messages if "invalid JSON parameters" in m)
    assert "at position 1" in json_message
    assert sum("validate_first" in m for m in messages) == 3
    assert sum("timeout_seconds" in m for m in messages) == 5
    assert sum("must be a JSON object" in m for m in messages) == 2
    assert any("nested too deeply" in m for m in messages)
    assert any("unsupported key(s): 1" in m for m in messages)
    assert devices.targets() == ["light.valid"]


async def test_a_numeric_entity_id_and_list_parameters_are_skipped(
    core_client: HomeAssistantClient, devices: Devices
) -> None:
    """A numeric ``entity_id`` passes the missing-field check and a JSON list
    passes the decoder; each still needs its own skip."""
    data = await _run_tool(
        core_client,
        [
            {"entity_id": 123, "action": "off"},
            {
                "entity_id": "light.list_parameters",
                "action": "on",
                "parameters": "[1, 2]",
            },
            {"entity_id": "light.valid", "action": "off"},
        ],
    )

    assert data["successful_commands"] == 1
    assert [d["index"] for d in data["skipped_details"]] == [0, 1]
    messages = [d["error"]["message"] for d in data["skipped_details"]]
    assert "requires string entity_id and action" in messages[0]
    assert "parameters must be a JSON object" in messages[1]
    assert devices.targets() == ["light.valid"]


async def test_light_parameters_sent_as_json_reach_the_light_service(
    core_client: HomeAssistantClient, devices: Devices
) -> None:
    await _run_tool(
        core_client,
        [
            {
                "entity_id": "light.kitchen",
                "action": "on",
                "parameters": '{"brightness_pct": 42}',
            }
        ],
    )

    # Core's light service turns the percentage into its 0-255 brightness.
    assert devices.calls == [
        Received("light.kitchen", "turn_on", {"brightness": round(42 * 255 / 100)})
    ]


# --- a failing row keeps its own error, the others still run ------------------


async def test_a_missing_entity_does_not_stop_the_sequential_batch(
    core_client: HomeAssistantClient, devices: Devices
) -> None:
    result = await DeviceControlTools(core_client).bulk_device_control(
        [
            {"entity_id": "light.ok", "action": "on"},
            {"entity_id": "light.missing", "action": "on"},
            {"entity_id": "light.also_ok", "action": "on"},
        ],
        parallel=False,
    )

    assert (result["total_operations"], result["successful_commands"]) == (3, 2)
    assert len(result["results"]) == 3
    assert result["results"][1]["error"]["code"] == ErrorCode.ENTITY_NOT_FOUND
    assert devices.targets() == ["light.ok", "light.also_ok"]


async def test_a_parallel_row_keeps_its_own_error_code(
    core_client: HomeAssistantClient, devices: Devices
) -> None:
    """Not flattened into SERVICE_CALL_FAILED."""
    result = await DeviceControlTools(core_client).bulk_device_control(
        [
            {"entity_id": "light.ok", "action": "on"},
            {"entity_id": "light.kitchen", "action": "fly"},
        ],
        parallel=True,
    )

    assert (result["total_operations"], result["successful_commands"]) == (2, 1)
    assert result["results"][1]["error"]["code"] == ErrorCode.SERVICE_INVALID_ACTION
    assert devices.targets() == ["light.ok"]
