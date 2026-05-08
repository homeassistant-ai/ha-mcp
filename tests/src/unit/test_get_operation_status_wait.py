"""Regression tests for ``get_device_operation_status`` honoring ``timeout_seconds``.

Before the fix, the function did a single point-in-time read of the operation
status and returned immediately, ignoring ``timeout_seconds`` despite its
docstring promising "Maximum time to wait for completion."

These tests pin the new behavior: poll memory at 0.2s intervals until the
operation leaves PENDING, or until ``timeout_seconds`` elapses.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.utils.operation_manager import DeviceOperation, OperationStatus


def _make_operation(status: OperationStatus = OperationStatus.PENDING) -> DeviceOperation:
    return DeviceOperation(
        operation_id="op-1",
        entity_id="light.a",
        action="on",
        service_domain="light",
        service_name="turn_on",
        service_data={},
        status=status,
        expected_state={"state": "on"},
        result_state={"state": "on"},
    )


def _client() -> MagicMock:
    client = MagicMock()
    client.base_url = "http://homeassistant.local"
    client.token = "t"
    client.verify_ssl = True
    return client


@pytest.mark.asyncio
async def test_returns_immediately_when_completed() -> None:
    """No polling when the operation is already completed — single read, fast return."""
    op = _make_operation(OperationStatus.COMPLETED)
    op.completion_time = op.start_time + 100  # 100 ms duration

    tools = DeviceControlTools(client=_client())

    with patch(
        "ha_mcp.tools.device_control.get_operation_from_memory", return_value=op
    ) as mock_get:
        result = await tools.get_device_operation_status("op-1", timeout_seconds=5)

    assert result["status"] == "completed"
    assert result["success"] is True
    # Single read — no polling loop ran.
    assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_polls_until_completion_within_timeout() -> None:
    """Pending → completed transition is observed within timeout_seconds."""
    pending = _make_operation(OperationStatus.PENDING)
    completed = _make_operation(OperationStatus.COMPLETED)
    completed.completion_time = completed.start_time + 100

    # Initial fetch + first poll see PENDING; second poll sees COMPLETED.
    sequence = [pending, pending, completed]
    call_index = {"i": 0}

    def fake_get(_op_id: str) -> Any:
        i = call_index["i"]
        call_index["i"] = min(i + 1, len(sequence) - 1)
        return sequence[i]

    tools = DeviceControlTools(client=_client())

    with patch(
        "ha_mcp.tools.device_control.get_operation_from_memory", side_effect=fake_get
    ):
        result = await tools.get_device_operation_status("op-1", timeout_seconds=2)

    assert result["status"] == "completed"
    assert call_index["i"] >= 2  # at least one poll happened


@pytest.mark.asyncio
async def test_returns_pending_when_timeout_expires() -> None:
    """If the operation never leaves PENDING, return the pending payload after timeout."""
    pending = _make_operation(OperationStatus.PENDING)
    tools = DeviceControlTools(client=_client())

    # Use a tiny timeout so the test stays fast; polling interval is 0.2s,
    # so timeout_seconds=0.3 yields ~1 poll attempt before deadline.
    fake_sleep_calls = {"n": 0}

    async def fast_sleep(_secs: float) -> None:
        fake_sleep_calls["n"] += 1

    with (
        patch(
            "ha_mcp.tools.device_control.get_operation_from_memory",
            return_value=pending,
        ),
        patch.object(asyncio, "sleep", new=fast_sleep),
    ):
        result = await tools.get_device_operation_status("op-1", timeout_seconds=1)

    assert result["status"] == "pending"
    assert "time_remaining_ms" in result
    assert fake_sleep_calls["n"] >= 1  # at least one poll cycle
