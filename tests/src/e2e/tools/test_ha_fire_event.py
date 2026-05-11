"""
E2E tests for ha_fire_event tool - fire events onto the HA event bus.
"""

import logging

import pytest
from fastmcp.exceptions import ToolError

from ..utilities.assertions import assert_mcp_success, safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_fire_event_no_data(mcp_client):
    """Fire a custom event without event data."""
    result = await mcp_client.call_tool(
        "ha_fire_event",
        {"event_type": "test_mcp_event_no_data"},
    )
    data = assert_mcp_success(result, "fire event without data")
    assert data["success"] is True
    assert data["event_type"] == "test_mcp_event_no_data"
    assert "message" in data
    logger.info("Fired event without data: %s", data)


@pytest.mark.asyncio
async def test_fire_event_with_dict_data(mcp_client):
    """Fire a custom event with dict event data."""
    result = await mcp_client.call_tool(
        "ha_fire_event",
        {"event_type": "test_mcp_event_with_data", "data": {"key": "value", "count": 1}},
    )
    data = assert_mcp_success(result, "fire event with dict data")
    assert data["success"] is True
    assert data["event_type"] == "test_mcp_event_with_data"
    logger.info("Fired event with dict data: %s", data)


@pytest.mark.asyncio
async def test_fire_event_with_json_string_data(mcp_client):
    """Fire a custom event with JSON-string event data (auto-parsed)."""
    result = await mcp_client.call_tool(
        "ha_fire_event",
        {"event_type": "test_mcp_event_json_str", "data": '{"source": "e2e_test"}'},
    )
    data = assert_mcp_success(result, "fire event with JSON string data")
    assert data["success"] is True
    assert data["event_type"] == "test_mcp_event_json_str"
    logger.info("Fired event with JSON string data: %s", data)


@pytest.mark.asyncio
async def test_fire_event_list_data_rejected(mcp_client):
    """Event data must be a dict — a JSON array is rejected with ToolError."""
    with pytest.raises(ToolError):
        await mcp_client.call_tool(
            "ha_fire_event",
            {"event_type": "test_mcp_event_bad_data", "data": "[1, 2, 3]"},
        )


@pytest.mark.asyncio
async def test_fire_builtin_event_type(mcp_client):
    """Fire a built-in HA event type to verify the API accepts it."""
    result = await mcp_client.call_tool(
        "ha_fire_event",
        {"event_type": "homeassistant_start"},
    )
    data = assert_mcp_success(result, "fire homeassistant_start event")
    assert data["success"] is True
    assert data["event_type"] == "homeassistant_start"
    logger.info("Fired homeassistant_start: %s", data)
