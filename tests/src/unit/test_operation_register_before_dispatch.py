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
    # A RuntimeError exercises the generic ``except Exception`` dispatch-failure branch
    # (control_device_smart converts it into a structured ToolError via
    # exception_to_structured_error). The ``except ToolError`` branch is covered
    # separately by ``test_dispatch_toolerror_marks_operation_failed``.
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


@pytest.mark.asyncio
async def test_dispatch_toolerror_marks_operation_failed(monkeypatch):
    """A dispatch raising ToolError exercises the ``except ToolError`` branch (distinct
    from the generic ``except Exception``) and also flips the pre-registered op to
    FAILED, so a later unrelated event can't complete a write that never happened."""
    manager = get_operation_manager()

    client = MagicMock()
    client.call_service = AsyncMock(side_effect=ToolError("dispatch rejected"))
    client.get_entity_state = AsyncMock(return_value={"state": "off"})

    tools = DeviceControlTools(client=client)
    with pytest.raises(ToolError):
        await tools.control_device_smart(
            entity_id="light.kitchen", action="on", validate_first=False
        )

    ops = list(manager.operations.values())
    assert len(ops) == 1
    assert ops[0].status == OperationStatus.FAILED
    # A later, unrelated state event for the same entity must NOT complete it.
    update_pending_operations("light.kitchen", {"state": "on", "attributes": {}})
    assert ops[0].status == OperationStatus.FAILED


@pytest.mark.asyncio
async def test_landed_write_completed_mid_dispatch_is_not_downgraded(monkeypatch):
    """M-terminal: register-before-dispatch means the confirming ``state_changed`` can
    COMPLETE the op mid-dispatch (the write DID land); if ``call_service`` then raises
    for an unrelated reason, ``fail_pending_operation`` must NOT downgrade the terminal
    COMPLETED status to FAILED and misreport a landed write."""
    manager = get_operation_manager()

    client = MagicMock()

    async def _complete_then_raise(domain, service, data, **kwargs):
        # The confirming event lands and completes the op WHILE the dispatch is in
        # flight (the write reached HA); THEN the call raises for an unrelated reason.
        update_pending_operations("light.kitchen", {"state": "on", "attributes": {}})
        raise RuntimeError("post-landing failure")

    client.call_service = AsyncMock(side_effect=_complete_then_raise)
    client.get_entity_state = AsyncMock(return_value={"state": "off"})

    tools = DeviceControlTools(client=client)
    with pytest.raises(ToolError):
        await tools.control_device_smart(
            entity_id="light.kitchen", action="on", validate_first=False
        )

    ops = list(manager.operations.values())
    assert len(ops) == 1
    # The op completed mid-dispatch and MUST stay COMPLETED (not downgraded to FAILED).
    assert ops[0].status == OperationStatus.COMPLETED
