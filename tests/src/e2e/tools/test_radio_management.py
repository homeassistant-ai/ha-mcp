"""E2E tests for the ``ha_manage_radio`` tool and the system-health radio includes.

The E2E testcontainer is a bare Home Assistant Core image with NO radio
integrations configured (no ``zwave_js`` / ``zha`` / ``matter`` / ``otbr``), so
node-level radio commands cannot succeed end-to-end here. What this suite *can*
verify deterministically over the real MCP/WebSocket/REST plumbing is:

1. Tool registration — ``ha_manage_radio`` is discoverable and callable.
2. The pre-flight validation gates that never reach Home Assistant (unknown
   action, missing required param, destructive-without-confirm). These are
   deterministic regardless of which integrations are installed.
3. Graceful degradation: ``network_status`` on a radio whose config entry is
   absent resolves the entry first and returns the documented
   ``available: False`` / ``not configured`` envelope instead of erroring.
4. The new ``thread_network`` / ``matter_network`` includes on
   ``ha_get_system_health`` return their sections without crashing (each may
   carry an ``error`` marker on a radio-less container).

Stored Thread datasets are also tested without radio hardware. Other happy-path
write actions (commission, inclusion, fabric removal, firmware,
channel migration, ...) require a live radio and are out of reach for CI; they
are exercised by the unit tests instead.
"""

import json
import logging

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client import HomeAssistantClient

from ..utilities.assertions import (
    MCPAssertions,
    extract_error_message,
    parse_mcp_result,
    safe_call_tool,
)
from ..utilities.wait_helpers import wait_for_tool_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,params",
    [
        ("get_config_params", {}),
        ("get_config_param", {"property": 3}),
        ("get_config_param", {"property": 3, "refresh": True}),
    ],
)
async def test_zwave_parameter_reads_surface_real_ha_errors_without_radio(
    mcp_client: Client, action: str, params: dict
) -> None:
    """Read actions reach HA rather than being rejected as unknown MCP actions."""
    async with MCPAssertions(mcp_client) as mcp:
        data = await mcp.call_tool_failure(
            "ha_manage_radio",
            {
                "radio": "zwave",
                "action": action,
                "device_id": "00000000000000000000000000000000",
                "params": params,
            },
        )
    assert "Unknown action" not in json.dumps(data)
    assert "zwave_js/get_config_parameters" in json.dumps(data)


@pytest.mark.asyncio
async def test_ha_manage_radio_is_registered(mcp_client):
    """ha_manage_radio is auto-discovered and exposed by the MCP server."""
    tools = await mcp_client.list_tools()
    tool_names = [tool.name for tool in tools]
    assert "ha_manage_radio" in tool_names, (
        f"ha_manage_radio not registered; available tools: {sorted(tool_names)}"
    )
    logger.info("ha_manage_radio is registered and callable")


@pytest.mark.asyncio
async def test_unknown_action_lists_supported_actions(mcp_client):
    """An unknown action fails before reaching HA and surfaces the supported list.

    The dispatcher rejects any action not in the radio's SUPPORTED map and
    attaches the sorted supported actions (in the ``supported`` context field and
    the ``Use one of: ...`` suggestion) so the agent can self-correct.
    """
    data = await safe_call_tool(
        mcp_client,
        "ha_manage_radio",
        {"radio": "matter", "action": "not_an_action"},
    )
    logger.info(f"Unknown-action result: {data}")

    assert data.get("success") is False, "Unknown action must fail"

    # The supported actions are surfaced via the context ``supported`` list and
    # the suggestion text. Assert on the whole error surface so the test stays
    # robust to exactly where they land.
    haystack = json.dumps(data).lower()
    for expected_action in ("commission", "network_status", "diagnostics"):
        assert expected_action in haystack, (
            f"Expected supported action '{expected_action}' in error surface: {data}"
        )

    supported = data.get("supported")
    if supported is not None:
        assert isinstance(supported, list) and "commission" in supported, (
            f"'supported' should list the radio's actions, got: {supported}"
        )


@pytest.mark.asyncio
async def test_missing_required_param_errors(mcp_client):
    """A missing required param fails before reaching HA and names the param.

    ``matter/commission`` requires a setup ``code``; omitting it trips the
    required-arg gate, which lists the missing parameter name.
    """
    data = await safe_call_tool(
        mcp_client,
        "ha_manage_radio",
        {"radio": "matter", "action": "commission"},
    )
    logger.info(f"Missing-param result: {data}")

    assert data.get("success") is False, "Missing required param must fail"

    error_msg = extract_error_message(data)
    assert "code" in error_msg.lower(), (
        f"Error should name the missing 'code' param, got: {error_msg}"
    )
    # ``missing`` is attached at the top level via the error context.
    missing = data.get("missing")
    if missing is not None:
        assert "code" in missing, f"'missing' should list 'code', got: {missing}"


@pytest.mark.asyncio
async def test_destructive_without_confirm_errors(mcp_client):
    """A destructive action without confirm=True is refused before reaching HA.

    ``matter/remove_fabric`` is destructive: with its required ``device_id``
    present but ``confirm`` defaulting to False, the confirm gate rejects it and
    the error tells the caller to pass confirm=True.
    """
    data = await safe_call_tool(
        mcp_client,
        "ha_manage_radio",
        {"radio": "matter", "action": "remove_fabric", "device_id": "x"},
    )
    logger.info(f"Destructive-no-confirm result: {data}")

    assert data.get("success") is False, "Destructive action without confirm must fail"

    error_msg = extract_error_message(data)
    assert "confirm" in error_msg.lower(), (
        f"Error should mention confirm, got: {error_msg}"
    )


@pytest.mark.asyncio
async def test_thread_network_status_degrades_gracefully(mcp_client):
    """thread/network_status returns available=False when no OTBR is configured.

    The handler resolves the ``otbr`` config entry first; with none present on a
    bare container it returns the documented degraded envelope rather than
    erroring.
    """
    result = await mcp_client.call_tool(
        "ha_manage_radio",
        {"radio": "thread", "action": "network_status"},
    )
    data = parse_mcp_result(result)
    logger.info(f"thread/network_status result: {data}")

    assert data.get("success") is True, (
        f"thread/network_status should degrade gracefully, got: {data}"
    )
    assert data.get("available") is False, (
        f"No OTBR configured -> available should be False, got: {data}"
    )
    assert data.get("radio") == "thread"
    warnings = data.get("warnings") or []
    assert any("otbr" in str(w).lower() for w in warnings), (
        f"Degraded payload should warn about the absent otbr integration: {warnings}"
    )


@pytest.mark.asyncio
async def test_zwave_network_status_degrades_gracefully(mcp_client):
    """zwave/network_status returns available=False when zwave_js is not configured."""
    result = await mcp_client.call_tool(
        "ha_manage_radio",
        {"radio": "zwave", "action": "network_status"},
    )
    data = parse_mcp_result(result)
    logger.info(f"zwave/network_status result: {data}")

    assert data.get("success") is True, (
        f"zwave/network_status should degrade gracefully, got: {data}"
    )
    assert data.get("available") is False, (
        f"No zwave_js entry -> available should be False, got: {data}"
    )
    assert data.get("radio") == "zwave"
    warnings = data.get("warnings") or []
    assert any("zwave_js" in str(w).lower() for w in warnings), (
        f"Degraded payload should warn about the absent zwave_js integration: {warnings}"
    )


@pytest.mark.asyncio
async def test_system_health_thread_and_matter_includes(mcp_client):
    """ha_get_system_health surfaces the thread_network/matter_network sections.

    Both are WebSocket-backed sections. On a container whose system_health
    baseline is unavailable the whole call surfaces a "not available" error
    (skip, mirroring the sibling zwave_network/zha_network tests). When the
    baseline is up, both sections are present; on a radio-less container each may
    carry an ``error`` marker, which is acceptable.
    """
    result = await mcp_client.call_tool(
        "ha_get_system_health", {"include": "thread_network,matter_network"}
    )
    data = parse_mcp_result(result)
    logger.info(f"system_health thread+matter result: {data}")

    if not data.get("success"):
        error_msg = str(data.get("error", ""))
        if "not available" in error_msg.lower():
            pytest.skip("system_health not available in test environment")
        pytest.fail(f"system_health thread+matter include failed: {error_msg}")

    assert "thread_network" in data, (
        "Missing 'thread_network' when include='thread_network'"
    )
    assert "matter_network" in data, (
        "Missing 'matter_network' when include='matter_network'"
    )
    # Both sections are dicts; on a radio-less container they legitimately carry
    # an absent-integration ``error`` marker, so assert shape, not content.
    assert isinstance(data["thread_network"], dict)
    assert isinstance(data["matter_network"], dict)


@pytest.mark.asyncio
async def test_thread_dataset_lifecycle_without_border_router(
    mcp_client: Client, ha_client: HomeAssistantClient
) -> None:
    """Switch preferences and remove stale datasets through real HA commands.

    HA cannot clear the preferred dataset through its API. The final dataset
    remains in the disposable HA fixture and is removed with fixture teardown.
    """
    # Valid synthetic dataset: channel 15, PAN 0x1234, no border router.
    dataset_tlv = (
        "0E080000000000010000000300000F35060004001FFFE0020811111111222222220708FDAD70BF"
        "E5AA15DD051000112233445566778899AABBCCDDEEFF030E4F70656E54687265616444656D6F01"
        "0212340410445F2B5CA6F2A93A55CE570A70EFEECB0C0402A0F7F8"
    )
    async with MCPAssertions(mcp_client) as mcp:
        entries = await ha_client.list_config_entries()
        if not any(entry["domain"] == "thread" for entry in entries):
            await mcp.call_tool_success("ha_set_integration", {"domain": "thread"})
        initial = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_manage_radio",
            arguments={"radio": "thread", "action": "list_datasets"},
            predicate=lambda data: data.get("success") is True,
            description="Thread dataset API is ready",
        )
        assert initial["datasets"]["datasets"] == []

        for source, pan_id in (
            ("e2e-old", "1111111122222222"),
            ("e2e-new", "1111111122222233"),
        ):
            await mcp.call_tool_success(
                "ha_manage_radio",
                {
                    "radio": "thread",
                    "action": "add_dataset",
                    "params": {
                        "source": source,
                        "tlv": dataset_tlv.replace("1111111122222222", pan_id),
                    },
                },
            )
        listed = await mcp.call_tool_success(
            "ha_manage_radio", {"radio": "thread", "action": "list_datasets"}
        )
        datasets = {
            d["source"]: d["dataset_id"] for d in listed["datasets"]["datasets"]
        }
        old_id, new_id = datasets["e2e-old"], datasets["e2e-new"]
        await mcp.call_tool_success(
            "ha_manage_radio",
            {
                "radio": "thread",
                "action": "set_preferred_dataset",
                "params": {"dataset_id": old_id},
            },
        )
        await mcp.call_tool_failure(
            "ha_manage_radio",
            {
                "radio": "thread",
                "action": "delete_dataset",
                "params": {"dataset_id": old_id},
                "confirm": True,
            },
            expected_error="attempt to remove preferred dataset",
        )
        await mcp.call_tool_success(
            "ha_manage_radio",
            {
                "radio": "thread",
                "action": "set_preferred_dataset",
                "params": {"dataset_id": new_id},
            },
        )
        await mcp.call_tool_success(
            "ha_manage_radio",
            {
                "radio": "thread",
                "action": "delete_dataset",
                "params": {"dataset_id": old_id},
                "confirm": True,
            },
        )
        final = await mcp.call_tool_success(
            "ha_manage_radio", {"radio": "thread", "action": "list_datasets"}
        )
        assert [
            (d["dataset_id"], d["preferred"]) for d in final["datasets"]["datasets"]
        ] == [(new_id, True)]
