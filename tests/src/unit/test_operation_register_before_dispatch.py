"""Regression tests for the register-before-dispatch ordering in
``control_device_smart`` (issue #1813 Phase 3, D5b).

The pending operation must be stored BEFORE the service call is dispatched, so a
fast entity's ``state_changed`` event that arrives the instant the call returns
finds a matching pending operation instead of being silently dropped. A dispatch
that then RAISES must flip the just-registered operation to FAILED so a later,
unrelated event for the same entity cannot spuriously complete it.

The service call goes through the server's own client to a real ``hass`` and
Core's ``light`` integration to a light device (``_switched_states``). The
``state_changed`` events Core fires are handed to the operation manager as they
fire, which is what the server's listener does, so each confirming state is
Core's own.
"""

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.utils.operation_manager import (
    OperationStatus,
    get_operation_manager,
    update_pending_operations,
)

from ._operations import operations_without_listener as operations_without_listener
from ._switched_states import set_up_devices


def _fed_to_operations(hass: HomeAssistant) -> list[str]:
    """Hand each ``state_changed`` Core fires to the operation manager as it
    fires, as the server's own listener does; the ids it completed."""
    completed: list[str] = []

    @callback
    def _on_change(event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        if state is not None:
            completed.extend(
                update_pending_operations(state.entity_id, dict(state.as_dict()))
            )

    hass.bus.async_listen(EVENT_STATE_CHANGED, _on_change)
    return completed


@pytest.mark.asyncio
async def test_state_change_arriving_during_dispatch_is_not_dropped(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A state_changed processed while call_service is still out completes the
    operation, which proves the op was registered before the dispatch."""
    manager = get_operation_manager()
    await set_up_devices(hass, {"light.kitchen": "off"})
    # The light turns on, and Core fires its state_changed, before the service
    # call returns to control_device_smart.
    completed = _fed_to_operations(hass)

    tools = DeviceControlTools(client=core_client)
    result = await tools.control_device_smart(
        entity_id="light.kitchen", action="on", validate_first=False
    )

    op_id = result["operation_id"]
    assert op_id in completed
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
    completed = _fed_to_operations(hass)
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    assert completed == []
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
    devices = await set_up_devices(hass, {"light.kitchen": "off"})
    # The light turns on, so Core's state_changed completes the op while the
    # dispatch is in flight; THEN the device fails the call.
    devices.behaviour["light.kitchen"] = "fail_after"
    completed = _fed_to_operations(hass)

    tools = DeviceControlTools(client=core_client)
    with pytest.raises(ToolError):
        await tools.control_device_smart(
            entity_id="light.kitchen", action="on", validate_first=False
        )

    light = hass.states.get("light.kitchen")
    assert light is not None
    assert light.state == "on"
    ops = list(manager.operations.values())
    assert len(ops) == 1
    assert completed == [ops[0].operation_id]
    # The op completed mid-dispatch and MUST stay COMPLETED (not downgraded to FAILED).
    assert ops[0].status == OperationStatus.COMPLETED
