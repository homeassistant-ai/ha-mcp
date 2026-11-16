"""E2E tests for the ``ha_manage_radio`` tool and the system-health radio includes.

The E2E testcontainer is a bare Home Assistant Core image with NO radio
integrations configured (no ``zwave_js`` / ``zha`` / ``matter`` / ``otbr``).
Without one, this suite verifies over the real MCP/WebSocket/REST plumbing:

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

Stored Thread datasets are also tested without radio hardware.

``TestZwaveNetwork`` runs the Z-Wave actions on Core's ``zwave_js`` integration,
connected to a Z-Wave JS server on zwave-js's mock controller
(``tests/zwave_mock``). The mock has no firmware update service and does not
finish a hard reset, so those two actions, like Zigbee, Matter and the Thread
border router actions, are exercised by the unit tests only.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client import HomeAssistantClient

from ..utilities.assertions import (
    MCPAssertions,
    extract_error_message,
    parse_mcp_result,
    safe_call_tool,
)
from ..utilities.cleanup import CleanupSteps
from ..utilities.wait_helpers import wait_for_tool_result
from ..utilities.zwave_mock import NODE_ID, start_zwave_mock

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,params",
    [
        ("get_config_params", {}),
        ("get_config_param", {"property": 3}),
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
    remains in the disposable HA fixture and is removed with fixture teardown,
    so ``ha_get_system_health`` must read it back as Core lists it.
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
        health = await mcp.call_tool_success(
            "ha_get_system_health", {"include": "thread_network"}
        )
        core = await ha_client.send_websocket_message({"type": "thread/list_datasets"})
        assert health["thread_network"]["datasets"] == core["result"]["datasets"]


# zwave-js's InclusionState, as ``network_status`` reports it.
_INCLUDING = 1
_EXCLUDING = 2
# The parameters ``tests/zwave_mock/server.mjs`` gives its switch.
_LED_BRIGHTNESS = 1
_REPORT_INTERVAL = 2


@dataclass(frozen=True)
class ZwaveNetwork:
    """The ``zwave_js`` config entry and the device of the mock switch."""

    entry_id: str
    device_id: str


async def _radio(mcp_client: Client, action: str, **arguments: Any) -> dict[str, Any]:
    """Run one Z-Wave action that must succeed."""
    async with MCPAssertions(mcp_client) as mcp:
        return await mcp.call_tool_success(
            "ha_manage_radio", {"radio": "zwave", "action": action, **arguments}
        )


async def _switch_device_id(ha_client: HomeAssistantClient, entry_id: str) -> str:
    """The device Home Assistant registered for the mock switch."""
    listed = await ha_client.send_websocket_message(
        {"type": "config/device_registry/list"}
    )
    (device_id,) = [
        device["id"]
        for device in listed["result"]
        if entry_id in device["config_entries"]
        and any(
            domain == "zwave_js" and node.endswith(f"-{NODE_ID}")
            for domain, node in device["identifiers"]
        )
    ]
    return device_id


async def _parameters_listed(mcp_client: Client, device_id: str) -> dict[str, Any]:
    """Wait until the switch's interview has found both parameters."""
    return await wait_for_tool_result(
        mcp_client,
        tool_name="ha_manage_radio",
        arguments={
            "radio": "zwave",
            "action": "get_config_params",
            "device_id": device_id,
        },
        predicate=lambda data: len(data["parameters"]) == 2,
        timeout=60,
        description="the mock switch's parameters are known",
    )


@pytest.fixture(scope="class")
async def zwave_network(
    mcp_client: Client, ha_client: HomeAssistantClient
) -> AsyncIterator[ZwaveNetwork]:
    """Core's ``zwave_js`` integration on a mock controller with one switch.

    The entry is removed afterwards: the session's Home Assistant has no radio
    for every test outside the class.
    """
    container, url = await asyncio.to_thread(start_zwave_mock)
    try:
        async with MCPAssertions(mcp_client) as mcp:
            created = await mcp.call_tool_success(
                "ha_set_integration", {"domain": "zwave_js", "config": {"url": url}}
            )
            entry_id = created["entry_id"]
            try:
                await wait_for_tool_result(
                    mcp_client,
                    tool_name="ha_manage_radio",
                    arguments={"radio": "zwave", "action": "network_status"},
                    predicate=lambda data: len(data.get("nodes", ())) == 2,
                    timeout=60,
                    description="the mock Z-Wave network is loaded",
                )
                device_id = await _switch_device_id(ha_client, entry_id)
                await _parameters_listed(mcp_client, device_id)
                yield ZwaveNetwork(entry_id, device_id)
            finally:
                await mcp.call_tool_success(
                    "ha_remove_helpers_integrations",
                    {"target": entry_id, "confirm": True},
                )
    finally:
        await asyncio.to_thread(container.stop)


async def _inclusion_state(mcp_client: Client) -> int:
    status = await _radio(mcp_client, "network_status")
    state: int = status["controller"]["inclusion_state"]
    return state


async def _stop(
    ha_client: HomeAssistantClient, network: ZwaveNetwork, command: str
) -> None:
    """Put the controller back to idle for the next test."""
    reply = await ha_client.send_websocket_message(
        {"type": f"zwave_js/{command}", "entry_id": network.entry_id}
    )
    assert reply.get("success"), reply


@pytest.mark.container_only
class TestZwaveNetwork:
    """``ha_manage_radio`` on a loaded Z-Wave network."""

    async def test_network_status_reports_the_controller_and_its_nodes(
        self, mcp_client: Client, zwave_network: ZwaveNetwork
    ) -> None:
        status = await _radio(mcp_client, "network_status")

        assert status["config_entry_id"] == zwave_network.entry_id
        assert status["controller"]["own_node_id"] == 1
        assert [node["node_id"] for node in status["nodes"]] == [1, NODE_ID]
        assert status["total_count"] == 2

    async def test_diagnostics_reads_the_node_behind_a_device(
        self, mcp_client: Client, zwave_network: ZwaveNetwork
    ) -> None:
        result = await _radio(
            mcp_client, "diagnostics", device_id=zwave_network.device_id
        )

        assert result["node_status"]["node_id"] == NODE_ID
        assert result["node_status"]["is_controller_node"] is False

    async def test_a_reachable_node_answers_a_ping(
        self, mcp_client: Client, zwave_network: ZwaveNetwork
    ) -> None:
        result = await _radio(mcp_client, "ping", device_id=zwave_network.device_id)

        assert result["action"] == "ping"

    async def test_parameters_are_listed_with_their_metadata(
        self, mcp_client: Client, zwave_network: ZwaveNetwork
    ) -> None:
        listed = await _parameters_listed(mcp_client, zwave_network.device_id)

        by_number = {p["property"]: p for p in listed["parameters"].values()}
        assert set(by_number) == {_LED_BRIGHTNESS, _REPORT_INTERVAL}
        brightness = by_number[_LED_BRIGHTNESS]["metadata"]
        assert (brightness["label"], brightness["min"], brightness["max"]) == (
            "LED brightness",
            0,
            10,
        )

    async def test_a_parameter_refresh_is_answered_by_the_device(
        self, mcp_client: Client, zwave_network: ZwaveNetwork
    ) -> None:
        result = await _radio(
            mcp_client,
            "get_config_param",
            device_id=zwave_network.device_id,
            params={"property": _LED_BRIGHTNESS, "refresh": True},
        )

        assert result["source"] == "device_request"
        assert result["parameter"]["property"] == _LED_BRIGHTNESS

    async def test_a_written_parameter_reads_back(
        self, mcp_client: Client, zwave_network: ZwaveNetwork
    ) -> None:
        written = await _radio(
            mcp_client,
            "set_config_param",
            device_id=zwave_network.device_id,
            params={"property": _REPORT_INTERVAL, "value": 900},
        )
        assert written["result"]["status"] == "accepted"

        read = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_manage_radio",
            arguments={
                "radio": "zwave",
                "action": "get_config_param",
                "device_id": zwave_network.device_id,
                "params": {"property": _REPORT_INTERVAL},
            },
            predicate=lambda data: data["parameter"]["value"] == 900,
            description="the written parameter is read back",
        )
        assert read["source"] == "cache"

    async def test_a_reinterviewed_node_is_usable_again(
        self, mcp_client: Client, zwave_network: ZwaveNetwork
    ) -> None:
        result = await _radio(
            mcp_client, "reinterview", device_id=zwave_network.device_id
        )

        assert result["long_running"] is True
        await _parameters_listed(mcp_client, zwave_network.device_id)

    async def test_a_network_route_rebuild_starts(
        self, mcp_client: Client, zwave_network: ZwaveNetwork
    ) -> None:
        result = await _radio(mcp_client, "rebuild_routes", params={"scope": "network"})

        assert (result["scope"], result["result"]) == ("network", True)

    async def test_inclusion_starts_from_a_dsk_alone(
        self,
        mcp_client: Client,
        ha_client: HomeAssistantClient,
        zwave_network: ZwaveNetwork,
    ) -> None:
        """Core refuses a DSK under its default strategy; the tool sends S2."""
        try:
            result = await _radio(
                mcp_client,
                "add",
                params={"dsk": "11111-22222-33333-44444-55555-11111-22222-33333"},
            )

            assert result["result"] is True
            assert await _inclusion_state(mcp_client) == _INCLUDING
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
                await _stop(ha_client, zwave_network, "stop_inclusion")

    async def test_removing_a_device_opens_an_exclusion_window(
        self,
        mcp_client: Client,
        ha_client: HomeAssistantClient,
        zwave_network: ZwaveNetwork,
    ) -> None:
        try:
            result = await _radio(mcp_client, "remove_device", confirm=True)

            assert (result["mode"], result["result"]) == ("exclusion", True)
            assert await _inclusion_state(mcp_client) == _EXCLUDING
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
                await _stop(ha_client, zwave_network, "stop_exclusion")
