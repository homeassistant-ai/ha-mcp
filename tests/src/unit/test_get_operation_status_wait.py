"""Unit tests for ``get_device_operation_status`` polling behavior.

Verifies the contract that the function polls in-memory state every 0.2s while
the operation is PENDING, returning as soon as the status flips (completed,
failed, timeout) or once ``timeout_seconds`` elapses.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

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
async def test_timeout_zero_skips_polling() -> None:
    """timeout_seconds=0 must skip the polling loop entirely (single read, pending payload)."""
    pending = _make_operation(OperationStatus.PENDING)
    tools = DeviceControlTools(client=_client())

    with patch(
        "ha_mcp.tools.device_control.get_operation_from_memory", return_value=pending
    ) as mock_get:
        result = await tools.get_device_operation_status("op-1", timeout_seconds=0)

    assert result["status"] == "pending"
    # No poll loop iterations — only the initial read.
    assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_polls_until_completion_within_timeout() -> None:
    """Pending → completed transition is observed within timeout_seconds."""
    pending = _make_operation(OperationStatus.PENDING)
    completed = _make_operation(OperationStatus.COMPLETED)
    completed.completion_time = completed.start_time + 100

    # Initial fetch (i=0) + 1st poll (i=1) see PENDING; 2nd poll (i=2) sees COMPLETED.
    sequence = [pending, pending, completed]
    call_index = {"i": 0}

    def fake_get(_op_id: str) -> Any:
        i = call_index["i"]
        call_index["i"] = min(i + 1, len(sequence) - 1)
        return sequence[i]

    tools = DeviceControlTools(client=_client())

    # Patch sleep to a no-op so we don't wait the real 0.2s × 2 = 400ms.
    async def fast_sleep(_secs: float) -> None:
        return None

    with (
        patch(
            "ha_mcp.tools.device_control.get_operation_from_memory", side_effect=fake_get
        ),
        patch.object(asyncio, "sleep", new=fast_sleep),
    ):
        result = await tools.get_device_operation_status("op-1", timeout_seconds=2)

    assert result["status"] == "completed"
    # Initial fetch (1) + 2 poll fetches = 3 calls; index is min-capped at 2.
    assert call_index["i"] == 2


@pytest.mark.asyncio
async def test_returns_pending_when_timeout_expires() -> None:
    """If the operation never leaves PENDING, return the pending payload after timeout.

    Uses a fake monotonic clock that advances past the deadline on the second
    poll so the loop exits cleanly without burning real wall time.
    """
    pending = _make_operation(OperationStatus.PENDING)
    tools = DeviceControlTools(client=_client())

    fake_sleep_calls = {"n": 0}

    # Synthetic monotonic clock: deadline math + first-iter check pass; second
    # iter check trips the break after the patched sleep advances the clock.
    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        return clock["t"]

    async def advance_then_sleep(_secs: float) -> None:
        fake_sleep_calls["n"] += 1
        clock["t"] += 0.6  # bigger than timeout, so the next deadline check exits

    with (
        patch(
            "ha_mcp.tools.device_control.get_operation_from_memory",
            return_value=pending,
        ),
        patch("ha_mcp.tools.device_control.time.monotonic", new=fake_monotonic),
        patch.object(asyncio, "sleep", new=advance_then_sleep),
    ):
        result = await tools.get_device_operation_status("op-1", timeout_seconds=0.5)

    assert result["status"] == "pending"
    assert "time_remaining_ms" in result
    # At least one poll cycle ran before the deadline check exited.
    assert fake_sleep_calls["n"] >= 1


@pytest.mark.asyncio
async def test_initial_not_found_raises_resource_not_found() -> None:
    """Initial fetch returning None must raise RESOURCE_NOT_FOUND, not a generic error."""
    tools = DeviceControlTools(client=_client())

    with (
        patch(
            "ha_mcp.tools.device_control.get_operation_from_memory", return_value=None
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await tools.get_device_operation_status("missing-op", timeout_seconds=5)

    # The tool serializes the structured error into ToolError's message as JSON.
    err_text = str(exc_info.value)
    assert "RESOURCE_NOT_FOUND" in err_text
    assert "missing-op" in err_text


@pytest.mark.asyncio
async def test_cleanup_mid_poll_raises_resource_not_found() -> None:
    """If the operation is GC'd between polls, raise RESOURCE_NOT_FOUND.

    Returning the stale "pending" payload would mislead the caller into
    thinking the op is still in flight when it has actually been purged.
    """
    pending = _make_operation(OperationStatus.PENDING)
    # Initial fetch returns pending; first poll sees None (cleaned up).
    sequence: list[DeviceOperation | None] = [pending, None]
    call_index = {"i": 0}

    def fake_get(_op_id: str) -> Any:
        i = call_index["i"]
        call_index["i"] = min(i + 1, len(sequence) - 1)
        return sequence[i]

    async def fast_sleep(_secs: float) -> None:
        return None

    tools = DeviceControlTools(client=_client())

    with (
        patch(
            "ha_mcp.tools.device_control.get_operation_from_memory", side_effect=fake_get
        ),
        patch.object(asyncio, "sleep", new=fast_sleep),
        pytest.raises(ToolError) as exc_info,
    ):
        await tools.get_device_operation_status("op-1", timeout_seconds=2)

    err_text = str(exc_info.value)
    assert "RESOURCE_NOT_FOUND" in err_text
    assert "cleaned up" in err_text.lower()


@pytest.mark.asyncio
async def test_failed_mid_poll_raises_service_call_failed() -> None:
    """If status flips to FAILED mid-poll, raise SERVICE_CALL_FAILED with the error message."""
    pending = _make_operation(OperationStatus.PENDING)
    failed = _make_operation(OperationStatus.FAILED)
    failed.error_message = "device unreachable"
    failed.completion_time = failed.start_time + 200

    sequence = [pending, failed]
    call_index = {"i": 0}

    def fake_get(_op_id: str) -> Any:
        i = call_index["i"]
        call_index["i"] = min(i + 1, len(sequence) - 1)
        return sequence[i]

    async def fast_sleep(_secs: float) -> None:
        return None

    tools = DeviceControlTools(client=_client())

    with (
        patch(
            "ha_mcp.tools.device_control.get_operation_from_memory", side_effect=fake_get
        ),
        patch.object(asyncio, "sleep", new=fast_sleep),
        pytest.raises(ToolError) as exc_info,
    ):
        await tools.get_device_operation_status("op-1", timeout_seconds=2)

    err_text = str(exc_info.value)
    assert "SERVICE_CALL_FAILED" in err_text
    assert "device unreachable" in err_text
