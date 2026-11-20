"""Polling and MCP validation against the real device operation store."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.exceptions import ValidationError as FastMCPValidationError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import device_control
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.utils.operation_manager import OperationStatus, get_operation_manager


@pytest.fixture
def pending(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    # A busy runner must not expire an operation during the test.
    now = time.time()
    monkeypatch.setattr(
        "ha_mcp.utils.operation_manager.time", SimpleNamespace(time=lambda: now)
    )
    manager = get_operation_manager()
    op = manager.create_operation("light.a", "on", "light", "turn_on", {})
    try:
        yield op
    finally:
        manager.operations.pop(op, None)


@pytest.fixture(autouse=True)
def poll_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Advance the tool's monotonic clock on sleep and let scheduled updates run."""
    now = 0.0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        nonlocal now
        assert delay > 0
        delays.append(delay)
        now += delay
        await asyncio.sleep(0)

    monkeypatch.setattr(device_control, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(device_control, "asyncio", SimpleNamespace(sleep=sleep))
    return delays


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


async def test_a_poll_sees_the_operation_complete(
    tools: DeviceControlTools, pending: str, poll_sleeps: list[float]
) -> None:
    manager = get_operation_manager()
    asyncio.get_running_loop().call_soon(
        manager.update_operation_status, pending, OperationStatus.COMPLETED
    )

    result = await tools.get_device_operation_status(pending, timeout_seconds=2)

    assert result["status"] == "completed"
    # The first read was still pending: the answer came from a later poll.
    assert poll_sleeps


@pytest.mark.parametrize(
    ("gone", "code", "reason"),
    [
        (False, "SERVICE_CALL_FAILED", "device unreachable"),
        (True, "RESOURCE_NOT_FOUND", "cleaned up"),
    ],
    ids=["failed", "cleaned"],
)
async def test_a_poll_reports_an_operation_that_failed_or_is_gone(
    tools: DeviceControlTools, pending: str, gone: bool, code: str, reason: str
) -> None:
    manager = get_operation_manager()
    loop = asyncio.get_running_loop()
    if gone:
        loop.call_soon(manager.operations.pop, pending)
    else:
        loop.call_soon(
            manager.update_operation_status,
            pending,
            OperationStatus.FAILED,
            None,
            reason,
        )

    with pytest.raises(ToolError) as raised:
        await tools.get_device_operation_status(pending, timeout_seconds=2)

    assert code in str(raised.value)
    assert reason in str(raised.value)


async def test_fractional_deadline_returns_the_pending_operation(
    tools: DeviceControlTools, pending: str, poll_sleeps: list[float]
) -> None:
    result = await tools.get_device_operation_status(pending, timeout_seconds=0.05)
    assert result["status"] == "pending"
    assert "time_remaining_ms" in result
    assert sum(poll_sleeps) == pytest.approx(0.05)


async def test_initial_not_found_raises_resource_not_found(
    tools: DeviceControlTools,
) -> None:
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
