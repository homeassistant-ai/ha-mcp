"""Polling and MCP validation against the real device operation store."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.exceptions import ValidationError as FastMCPValidationError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.utils.operation_manager import OperationStatus, get_operation_manager


@pytest.fixture
def pending() -> Iterator[str]:
    manager = get_operation_manager()
    op = manager.create_operation("light.a", "on", "light", "turn_on", {})
    try:
        yield op
    finally:
        manager.operations.pop(op, None)


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> DeviceControlTools:
    return DeviceControlTools(client=core_client)


@pytest.mark.parametrize("completed", [True, False])
async def test_completed_or_zero_timeout_never_polls(
    tools: DeviceControlTools, pending: str, completed: bool
) -> None:
    manager = get_operation_manager()
    if completed:
        manager.update_operation_status(pending, OperationStatus.COMPLETED)
    with patch.object(manager, "get_operation", wraps=manager.get_operation) as reads:
        result = await tools.get_device_operation_status(
            pending, timeout_seconds=5 if completed else 0
        )
    reads.assert_called_once_with(pending)
    assert result["status"] == ("completed" if completed else "pending")
    if completed:
        assert result["success"] is True
    else:
        assert "time_remaining_ms" in result


@pytest.mark.parametrize("change", ["completed", "failed", "cleaned"])
async def test_change_during_a_poll(
    tools: DeviceControlTools, pending: str, change: str
) -> None:
    manager = get_operation_manager()
    loop = asyncio.get_running_loop()
    if change == "completed":
        loop.call_later(0.01, manager.update_operation_status, pending, OperationStatus.COMPLETED)
        result = await tools.get_device_operation_status(pending, timeout_seconds=2)
        assert result["status"] == "completed"
        return
    if change == "cleaned":
        loop.call_later(0.01, manager.operations.pop, pending)
        expected = ("RESOURCE_NOT_FOUND", "cleaned up")
    else:
        loop.call_later(
            0.01, manager.update_operation_status, pending,
            OperationStatus.FAILED, None, "device unreachable",
        )
        expected = ("SERVICE_CALL_FAILED", "device unreachable")
    with pytest.raises(ToolError) as raised:
        await tools.get_device_operation_status(pending, timeout_seconds=2)
    for text in expected:
        assert text in str(raised.value)


async def test_fractional_deadline_returns_the_pending_operation(
    tools: DeviceControlTools, pending: str
) -> None:
    with patch("ha_mcp.tools.device_control.asyncio.sleep", wraps=asyncio.sleep) as sleep:
        result = await tools.get_device_operation_status(pending, timeout_seconds=0.05)
    assert result["status"] == "pending"
    assert "time_remaining_ms" in result
    assert sleep.await_count >= 1
    assert all(0 < call.args[0] <= 0.05 for call in sleep.await_args_list)


async def test_initial_not_found_raises_resource_not_found(tools: DeviceControlTools) -> None:
    with pytest.raises(ToolError) as raised:
        await tools.get_device_operation_status("missing-op", timeout_seconds=5)
    assert "RESOURCE_NOT_FOUND" in str(raised.value)
    assert "missing-op" in str(raised.value)


@pytest.mark.asyncio
async def test_registered_status_tool_accepts_fractional_timeout(
    core_client: HomeAssistantClient, pending: str
) -> None:
    """The MCP boundary preserves a fractional timeout for follow-up calls."""
    from ha_mcp._vendor.fastmcp import FastMCP
    from ha_mcp.tools.tools_service import register_service_tools

    mcp = FastMCP("test")
    tools = DeviceControlTools(client=core_client)
    register_service_tools(mcp, core_client, device_tools=tools)
    tool = await mcp.get_tool("ha_get_operation_status")
    assert tool is not None
    with patch.object(
        tools, "get_device_operation_status", wraps=tools.get_device_operation_status
    ) as status:
        result = await tool.run({"operation_id": pending, "timeout_seconds": 0.05})
    assert result.structured_content["operation_id"] == pending
    assert result.structured_content["status"] == "pending"
    status.assert_awaited_once_with(operation_id=pending, timeout_seconds=0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_timeout", [float("inf"), "inf", float("nan")])
async def test_registered_status_tool_rejects_non_finite_timeout(
    core_client: HomeAssistantClient, invalid_timeout: float | str
) -> None:
    """The public polling window must always have a finite deadline."""
    from ha_mcp._vendor.fastmcp import FastMCP
    from ha_mcp.tools.tools_service import register_service_tools

    mcp = FastMCP("test")
    register_service_tools(
        mcp, core_client, device_tools=DeviceControlTools(client=core_client)
    )
    tool = await mcp.get_tool("ha_get_operation_status")
    assert tool is not None

    with pytest.raises(FastMCPValidationError):
        await tool.run({"operation_id": "op-1", "timeout_seconds": invalid_timeout})
