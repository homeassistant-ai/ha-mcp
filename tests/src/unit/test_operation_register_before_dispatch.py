"""Regression tests for the register-before-dispatch ordering in
``control_device_smart`` (issue #1813 Phase 3, D5b).

The pending operation must be stored BEFORE the service call is dispatched, so a
fast entity's ``state_changed`` event that arrives the instant the call returns
finds a matching pending operation instead of being silently dropped. A dispatch
that then RAISES must flip the just-registered operation to FAILED so a later,
unrelated event for the same entity cannot spuriously complete it.

The service call goes through the server's own client to a real ``hass``; the
service handler stands in for the entity, and the ``state_changed`` the listener
would deliver is applied from inside it, while the dispatch is in flight.
"""

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.utils.operation_manager import (
    OperationStatus,
    get_operation_manager,
    update_pending_operations,
)

from ._operations import operations_without_listener  # noqa: F401


@pytest.mark.asyncio
async def test_state_change_arriving_during_dispatch_is_not_dropped(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A state_changed processed the instant call_service returns still completes
    the operation — proving the op was registered before the dispatch."""
    manager = get_operation_manager()
    seen: dict[str, list[str]] = {"completed": []}

    async def _fire_event_then_return(call: ServiceCall) -> None:
        # The real race: the entity's state_changed lands (and the listener
        # processes it) WHILE the dispatch call is in flight, before it returns
        # to control_device_smart. Pre-fix, no op exists yet → dropped.
        seen["completed"] = update_pending_operations(
            "light.kitchen", {"state": "on", "attributes": {}}
        )

    hass.services.async_register("light", "turn_on", _fire_event_then_return)

    tools = DeviceControlTools(client=core_client)
    result = await tools.control_device_smart(
        entity_id="light.kitchen", action="on", validate_first=False
    )

    op_id = result["operation_id"]
    # The event fired mid-dispatch matched the already-registered op.
    assert op_id in seen["completed"]
    assert manager.operations[op_id].status == OperationStatus.COMPLETED


@pytest.mark.asyncio
async def test_dispatch_failure_marks_operation_failed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """When the dispatch raises, the pre-registered op is flipped to FAILED so a
    later unrelated event can't complete a write that never happened."""
    manager = get_operation_manager()

    tools = DeviceControlTools(client=core_client)
    # No light.turn_on service: Core refuses the call, and the client's error
    # exercises the generic ``except Exception`` dispatch-failure branch
    # (control_device_smart converts it into a structured ToolError via
    # exception_to_structured_error).
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
async def test_landed_write_completed_mid_dispatch_is_not_downgraded(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """M-terminal: register-before-dispatch means the confirming ``state_changed`` can
    COMPLETE the op mid-dispatch (the write DID land); if ``call_service`` then raises
    for an unrelated reason, ``fail_pending_operation`` must NOT downgrade the terminal
    COMPLETED status to FAILED and misreport a landed write."""
    manager = get_operation_manager()

    async def _complete_then_raise(call: ServiceCall) -> None:
        # The confirming event lands and completes the op WHILE the dispatch is in
        # flight (the write reached HA); THEN the call raises for an unrelated reason.
        update_pending_operations("light.kitchen", {"state": "on", "attributes": {}})
        raise HomeAssistantError("post-landing failure")

    hass.services.async_register("light", "turn_on", _complete_then_raise)

    tools = DeviceControlTools(client=core_client)
    with pytest.raises(ToolError):
        await tools.control_device_smart(
            entity_id="light.kitchen", action="on", validate_first=False
        )

    ops = list(manager.operations.values())
    assert len(ops) == 1
    # The op completed mid-dispatch and MUST stay COMPLETED (not downgraded to FAILED).
    assert ops[0].status == OperationStatus.COMPLETED
