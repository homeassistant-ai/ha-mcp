"""
E2E tests for ha_call_event tool - publish events onto the HA event bus.
"""

import logging

import pytest
from fastmcp.exceptions import ToolError

from ..utilities.assertions import assert_mcp_success

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
async def test_call_builtin_event_type(mcp_client):
    """Publish a built-in HA event type to verify the API accepts it."""
    result = await mcp_client.call_tool(
        "ha_call_event",
        {"event_type": "homeassistant_start"},
    )
    data = assert_mcp_success(result, "call homeassistant_start event")
    assert data["success"] is True
    assert data["event_type"] == "homeassistant_start"
    logger.info("Called homeassistant_start: %s", data)
