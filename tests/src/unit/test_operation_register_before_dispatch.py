"""Regression tests for the register-before-dispatch ordering in
``control_device_smart`` (issue #1813 Phase 3, D5b).

The pending operation must be stored BEFORE the service call is dispatched, so a
fast entity's ``state_changed`` event that arrives the instant the call returns
finds a matching pending operation instead of being silently dropped. A dispatch
that then RAISES must flip the just-registered operation to FAILED so a later,
unrelated event for the same entity cannot spuriously complete it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.utils.operation_manager import (
    OperationStatus,
    get_operation_manager,
    update_pending_operations,
)


@pytest.fixture(autouse=True)
def _clear_operations():
    get_operation_manager().operations.clear()
    yield
    get_operation_manager().operations.clear()


@pytest.mark.asyncio
async def test_state_change_arriving_during_dispatch_is_not_dropped(monkeypatch):
    """A state_changed processed the instant call_service returns still completes
    the operation — proving the op was registered before the dispatch."""
    manager = get_operation_manager()
    seen: dict[str, list[str]] = {"completed": []}

    client = MagicMock()

    async def _fire_event_then_return(domain, service, data, **kwargs):
        # Simulate the real race: the entity's state_changed lands (and the
        # listener processes it) WHILE the dispatch call is in flight, before it
        # returns to control_device_smart. Pre-fix, no op exists yet → dropped.
        completed = update_pending_operations(
            "light.kitchen", {"state": "on", "attributes": {}}
        )
        seen["completed"] = completed
        return {}

    client.call_service = AsyncMock(side_effect=_fire_event_then_return)
    client.get_entity_state = AsyncMock(return_value={"state": "off"})

    tools = DeviceControlTools(client=client)
    result = await tools.control_device_smart(
        entity_id="light.kitchen", action="on", validate_first=False
    )

    op_id = result["operation_id"]
    # The event fired mid-dispatch matched the already-registered op.
    assert op_id in seen["completed"]
    assert manager.operations[op_id].status == OperationStatus.COMPLETED


@pytest.mark.asyncio
async def test_dispatch_failure_marks_operation_failed(monkeypatch):
    """When the dispatch raises, the pre-registered op is flipped to FAILED so a
    later unrelated event can't complete a write that never happened."""
    manager = get_operation_manager()

    client = MagicMock()
    client.call_service = AsyncMock(side_effect=RuntimeError("boom"))
    client.get_entity_state = AsyncMock(return_value={"state": "off"})

    tools = DeviceControlTools(client=client)
    # control_device_smart converts the dispatch failure into a structured
    # ToolError (via exception_to_structured_error) — the FAILED-cleanup runs
    # on the way out regardless of which exception type propagates.
    with pytest.raises(ToolError):
        await tools.control_device_smart(
            entity_id="light.kitchen", action="on", validate_first=False
        )

    # Exactly one op was registered, and it is FAILED (not left PENDING).
    ops = list(manager.operations.values())
    assert len(ops) == 1
    assert ops[0].status == OperationStatus.FAILED
    # A later, unrelated state event for the same entity must NOT complete it.
    update_pending_operations("light.kitchen", {"state": "on", "attributes": {}})
    assert ops[0].status == OperationStatus.FAILED
