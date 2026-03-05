"""
Simple connection test to verify E2E test setup works.
"""

import logging

import pytest

from ..utilities.assertions import assert_mcp_success, assert_search_results

logger = logging.getLogger(__name__)


# WebSocket event loop issue fixed - re-enabling test
@pytest.mark.asyncio
async def test_simple_connection(mcp_client):
    """Test basic MCP client connection and tool execution."""
    logger.info("☀️ Testing basic MCP connection with sun.sun entity")

    # Test a simple tool that doesn't use WebSocket - just get state
    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": "sun.sun"},  # Sun entity should always exist
    )

    # Parse and verify the result using standard assertion utility
    data = assert_mcp_success(result, "Get state request")

    # Verify we got state data
    state_data = data.get("data", {})
    assert "state" in state_data, f"Missing state in data: {state_data}"
    assert "entity_id" in state_data, f"Missing entity_id in data: {state_data}"

    logger.info(f"✅ Entity: {state_data.get('entity_id')}")
    logger.info(f"✅ State: {state_data.get('state')}")
    logger.info("✅ Simple connection test completed successfully")


@pytest.mark.asyncio
async def test_tool_listing(mcp_client):
    """Test that MCP client can list available tools."""
    logger.info("🛠️ Testing MCP tool listing capability")

    tools = await mcp_client.list_tools()
    assert len(tools) > 0, "Should have some tools available"
    logger.info(f"✅ MCP client has {len(tools)} tools available")

    # Verify some expected tools are present
    tool_names = [tool.name for tool in tools]
    expected_tools = ["ha_search_entities", "ha_get_overview", "ha_get_state"]

    for expected in expected_tools:
        assert expected in tool_names, f"Missing expected tool: {expected}"

    logger.info("✅ All expected tools found")


# WebSocket event loop issue fixed - re-enabling test
@pytest.mark.asyncio
async def test_entity_search(mcp_client):
    """Test basic entity search functionality."""
    logger.info("🔍 Testing entity search with 'light' query")

    result = await mcp_client.call_tool(
        "ha_search_entities", {"query": "light", "limit": 5}
    )

    # Parse and verify using standard assertion utility
    data = assert_mcp_success(result, "Entity search")

    # Use specialized search assertion utility
    search_data = data.get("data", data)  # Handle nested data structure
    assert_search_results(
        search_data, min_results=0
    )  # Allow 0 results in test environment

    results = search_data.get("results", [])
    logger.info(f"✅ Found {len(results)} entities matching 'light'")

    # Just verify we get some structure back, don't require specific entities
    if results:
        first_result = results[0]
        assert "entity_id" in first_result, (
            f"Missing entity_id in result: {first_result}"
        )
        logger.info(f"✅ Sample entity: {first_result.get('entity_id')}")
