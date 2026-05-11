"""
E2E tests for ha_call_event tool - publish events onto the HA event bus.
"""

import logging
import uuid

import pytest
from fastmcp.exceptions import ToolError

from ..utilities.assertions import assert_mcp_success, wait_for_automation
from ..utilities.wait_helpers import wait_for_entity_state

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_call_event_no_data(mcp_client):
    """Publish a custom event without event data."""
    result = await mcp_client.call_tool(
        "ha_call_event",
        {"event_type": "test_mcp_event_no_data"},
    )
    data = assert_mcp_success(result, "call event without data")
    assert data["success"] is True
    assert data["event_type"] == "test_mcp_event_no_data"
    assert "message" in data
    logger.info("Called event without data: %s", data)


@pytest.mark.asyncio
async def test_call_event_with_dict_data(mcp_client):
    """Publish a custom event with dict event data."""
    result = await mcp_client.call_tool(
        "ha_call_event",
        {"event_type": "test_mcp_event_with_data", "data": {"key": "value", "count": 1}},
    )
    data = assert_mcp_success(result, "call event with dict data")
    assert data["success"] is True
    assert data["event_type"] == "test_mcp_event_with_data"
    logger.info("Called event with dict data: %s", data)


@pytest.mark.asyncio
async def test_call_event_with_json_string_data(mcp_client):
    """Publish a custom event with JSON-string event data (auto-parsed)."""
    result = await mcp_client.call_tool(
        "ha_call_event",
        {"event_type": "test_mcp_event_json_str", "data": '{"source": "e2e_test"}'},
    )
    data = assert_mcp_success(result, "call event with JSON string data")
    assert data["success"] is True
    assert data["event_type"] == "test_mcp_event_json_str"
    logger.info("Called event with JSON string data: %s", data)


@pytest.mark.asyncio
async def test_call_event_list_data_rejected(mcp_client):
    """Event data must be a dict — a JSON array is rejected with ToolError."""
    with pytest.raises(ToolError):
        await mcp_client.call_tool(
            "ha_call_event",
            {"event_type": "test_mcp_event_bad_data", "data": "[1, 2, 3]"},
        )


@pytest.mark.asyncio
async def test_call_event_delivery_verified(mcp_client):
    """Prove that ha_call_event delivers events to HA subscribers end-to-end.

    Creates an input_boolean flag and an automation that turns it on when the
    custom event fires. After calling ha_call_event, polls until the flag
    reaches 'on', confirming event delivery through the HA event bus.
    """
    suffix = uuid.uuid4().hex[:8]
    event_type = f"test_mcp_event_delivery_{suffix}"
    boolean_id = None
    automation_id = None

    # 1. Create input_boolean flag (starts 'off')
    boolean_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": "input_boolean", "name": f"e2e event delivery {suffix}"},
    )
    boolean_data = assert_mcp_success(boolean_result, "Create input_boolean flag")
    boolean_id = (
        boolean_data.get("entity_id")
        or f"input_boolean.{boolean_data['helper_data']['id']}"
    )
    logger.info("Created flag entity: %s", boolean_id)

    try:
        # 2. Create automation: event trigger → turn on the flag
        automation_config = {
            "alias": f"e2e event delivery {suffix}",
            "trigger": [{"platform": "event", "event_type": event_type}],
            "action": [
                {
                    "service": "input_boolean.turn_on",
                    "target": {"entity_id": boolean_id},
                }
            ],
        }
        create_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {"config": automation_config},
        )
        create_data = assert_mcp_success(create_result, "Create delivery-probe automation")
        automation_id = create_data.get("entity_id") or create_data.get("automation_id")
        logger.info("Created automation: %s", automation_id)

        # 3. Wait for automation to be fully registered before firing
        config = await wait_for_automation(mcp_client, automation_id, timeout=15)
        assert config is not None, f"Automation {automation_id} not registered within 15s"

        # 4. Fire the event via ha_call_event
        event_result = await mcp_client.call_tool(
            "ha_call_event",
            {"event_type": event_type},
        )
        data = assert_mcp_success(event_result, "fire custom event")
        assert data["success"] is True
        assert data["event_type"] == event_type
        logger.info("Fired event %r: %s", event_type, data)

        # 5. Wait for flag to flip 'on' — proves event reached the bus
        state_reached = await wait_for_entity_state(
            mcp_client, boolean_id, "on", timeout=15
        )
        assert state_reached, (
            f"Event {event_type!r} was not delivered: "
            f"{boolean_id} did not reach 'on' within 15s"
        )
        logger.info("Event delivery verified: %s reached 'on'", boolean_id)

    finally:
        if automation_id:
            try:
                await mcp_client.call_tool(
                    "ha_config_remove_automation",
                    {"identifier": automation_id},
                )
            except Exception as exc:
                logger.warning("Cleanup failed for automation %s: %s", automation_id, exc)
        if boolean_id:
            try:
                await mcp_client.call_tool(
                    "ha_delete_helpers_integrations",
                    {
                        "target": boolean_id,
                        "helper_type": "input_boolean",
                        "confirm": True,
                    },
                )
            except Exception as exc:
                logger.warning("Cleanup failed for helper %s: %s", boolean_id, exc)
