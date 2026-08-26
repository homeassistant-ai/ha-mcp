"""Unit tests for the bulk operation-status summary contract.

Pins the batch-item behaviour (per-item failures never abort the batch)
and the summary semantics: ``completion_percentage`` is the TERMINAL
fraction (completed + failed + not_found over total) so it agrees with
``all_complete``, while ``success_rate`` carries the successful
fraction. Nothing else in the repo reads ``completion_percentage``, so
without this pin it could silently drift back to the success fraction
it used to be.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.utils.operation_manager import DeviceOperation, OperationStatus


def _operation(op_id: str, status: OperationStatus) -> DeviceOperation:
    op = DeviceOperation(
        operation_id=op_id,
        entity_id="light.test",
        action="turn_on",
        service_domain="light",
        service_name="turn_on",
        service_data={},
        status=status,
        timeout_ms=1000,
    )
    if status != OperationStatus.PENDING:
        op.completion_time = time.time() * 1000
    return op


@pytest.fixture
def bulk_tools(monkeypatch):
    """DeviceControlTools wired to an in-test operation store."""
    store: dict[str, DeviceOperation] = {}
    monkeypatch.setattr(
        "ha_mcp.tools.device_control.get_operation_from_memory", store.get
    )
    tools = DeviceControlTools(client=MagicMock())
    return tools, store


@pytest.mark.asyncio
async def test_all_terminal_batch_reports_full_completion(bulk_tools):
    tools, store = bulk_tools
    store["op-done"] = _operation("op-done", OperationStatus.COMPLETED)
    store["op-failed"] = _operation("op-failed", OperationStatus.FAILED)
    # "op-missing" is absent from the store -> not_found entry.

    result = await tools.get_bulk_operation_status(
        ["op-done", "op-failed", "op-missing"], timeout_seconds=0
    )

    assert result["success"] is True
    assert result["total_operations"] == 3
    assert result["completed"] == 1
    assert result["failed"] == 1
    assert result["not_found"] == 1
    assert result["pending"] == 0
    assert result["all_complete"] is True
    # Terminal fraction: 3 of 3 operations reached a terminal outcome,
    # even though only 1 succeeded.
    assert result["summary"]["completion_percentage"] == 100.0
    assert result["summary"]["success_rate"] == "1/3"
    assert len(result["detailed_results"]) == 3


@pytest.mark.asyncio
async def test_pending_batch_reports_partial_completion(bulk_tools):
    tools, store = bulk_tools
    store["op-done"] = _operation("op-done", OperationStatus.COMPLETED)
    store["op-pending"] = DeviceOperation(
        operation_id="op-pending",
        entity_id="light.test",
        action="turn_on",
        service_domain="light",
        service_name="turn_on",
        service_data={},
        status=OperationStatus.PENDING,
        timeout_ms=60000,
    )

    result = await tools.get_bulk_operation_status(
        ["op-done", "op-pending"], timeout_seconds=0
    )

    assert result["pending"] == 1
    assert result["all_complete"] is False
    assert result["summary"]["completion_percentage"] == 50.0


@pytest.mark.asyncio
async def test_bulk_wait_window_observes_completion_inside_it(bulk_tools):
    # Pins the shared wait window itself: a pending operation that flips
    # to completed INSIDE the window must come back completed. Under a 0s
    # snapshot (the state the review flagged) the poll loop never runs
    # and this reports pending.
    tools, store = bulk_tools
    op = DeviceOperation(
        operation_id="op-flips",
        entity_id="light.test",
        action="turn_on",
        service_domain="light",
        service_name="turn_on",
        service_data={},
        status=OperationStatus.PENDING,
        timeout_ms=60000,
    )
    store["op-flips"] = op

    async def flip_soon() -> None:
        await asyncio.sleep(0.3)
        op.status = OperationStatus.COMPLETED
        op.completion_time = time.time() * 1000

    result, _ = await asyncio.gather(
        tools.get_bulk_operation_status(["op-flips"], timeout_seconds=5),
        flip_soon(),
    )

    assert result["completed"] == 1
    assert result["pending"] == 0
    assert result["all_complete"] is True


@pytest.mark.asyncio
async def test_per_item_failure_entries_keep_structured_context(bulk_tools):
    tools, store = bulk_tools
    store["op-failed"] = _operation("op-failed", OperationStatus.FAILED)

    result = await tools.get_bulk_operation_status(["op-failed"], timeout_seconds=0)

    (entry,) = result["detailed_results"]
    assert entry["operation_id"] == "op-failed"
    assert entry["status"] == "failed"
    assert entry["success"] is False
    # The whole structured payload is preserved: error body plus the
    # top-level context keys the single-op error carries.
    assert entry["error"]["code"] == "SERVICE_CALL_FAILED"
    assert entry["entity_id"] == "light.test"
    assert entry["action"] == "turn_on"
