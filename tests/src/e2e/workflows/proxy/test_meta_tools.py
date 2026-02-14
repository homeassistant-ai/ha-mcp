"""
E2E tests for the Tool Search Proxy meta-tools.

Tests the 3 meta-tools that provide proxied tool access:
- ha_find_tools: search for tools by name/category/keyword
- ha_get_tool_details: get full schema and description for a tool
- ha_execute_tool: validate schema proof and dispatch to real implementation

These tests verify the full proxy flow against a live Home Assistant instance.
"""

import json
import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
class TestFindTools:
    """Test the ha_find_tools meta-tool."""

    async def test_find_by_category(self, mcp_client):
        """Test searching for tools by category."""
        result = await mcp_client.call_tool(
            "ha_find_tools", {"query": "zone"}
        )

        data = assert_mcp_success(result, "Find tools by category")
        assert data["count"] > 0, f"Expected zone tools, got: {data}"
        assert "matches" in data
        assert any(
            "zone" in m["tool_name"] for m in data["matches"]
        ), f"Expected zone tools in results: {data['matches']}"
        logger.info(f"Found {data['count']} zone tools")

    async def test_find_by_tool_name(self, mcp_client):
        """Test searching for a specific tool by name."""
        result = await mcp_client.call_tool(
            "ha_find_tools", {"query": "ha_get_addon"}
        )

        data = assert_mcp_success(result, "Find tool by name")
        assert data["count"] >= 1
        assert any(
            m["tool_name"] == "ha_get_addon" for m in data["matches"]
        )
        logger.info("Found ha_get_addon via search")

    async def test_find_no_results(self, mcp_client):
        """Test searching for a non-existent tool."""
        result = await mcp_client.call_tool(
            "ha_find_tools", {"query": "zzz_nonexistent_tool_xyz"}
        )

        data = assert_mcp_success(result, "Find nonexistent tool")
        assert data["count"] == 0
        assert "available_categories" in data
        logger.info(
            "No results returned correctly, "
            f"categories: {data.get('available_categories')}"
        )

    async def test_find_by_keyword(self, mcp_client):
        """Test searching by keyword in description."""
        result = await mcp_client.call_tool(
            "ha_find_tools", {"query": "label"}
        )

        data = assert_mcp_success(result, "Find tools by keyword")
        assert data["count"] > 0
        logger.info(f"Found {data['count']} tools matching 'label'")


@pytest.mark.asyncio
class TestGetToolDetails:
    """Test the ha_get_tool_details meta-tool."""

    async def test_get_details_success(self, mcp_client):
        """Test getting full details for a known tool."""
        result = await mcp_client.call_tool(
            "ha_get_tool_details",
            {"tool_name": "ha_config_get_label"},
        )

        data = assert_mcp_success(result, "Get tool details")
        assert data["tool_name"] == "ha_config_get_label"
        assert "description" in data
        assert "parameters" in data
        assert "schema_hash" in data
        assert len(data["schema_hash"]) == 8
        assert "usage" in data
        logger.info(
            f"Got details for ha_config_get_label, "
            f"schema_hash: {data['schema_hash']}"
        )

    async def test_get_details_not_found(self, mcp_client):
        """Test getting details for a non-existent tool."""
        result = await mcp_client.call_tool(
            "ha_get_tool_details",
            {"tool_name": "ha_nonexistent_tool_xyz"},
        )

        data = parse_mcp_result(result)
        assert data.get("success") is False
        logger.info("Non-existent tool properly returned error")

    async def test_get_details_has_parameters(self, mcp_client):
        """Test that tool details include parameter information."""
        result = await mcp_client.call_tool(
            "ha_get_tool_details",
            {"tool_name": "ha_create_zone"},
        )

        data = assert_mcp_success(result, "Get zone tool details")
        params = data.get("parameters", [])
        assert len(params) > 0, "Zone creation should have parameters"

        # Check parameter structure
        for param in params:
            assert "name" in param
            assert "type" in param
            assert "required" in param

        logger.info(
            f"ha_create_zone has {len(params)} parameters: "
            f"{[p['name'] for p in params]}"
        )


@pytest.mark.asyncio
class TestExecuteTool:
    """Test the ha_execute_tool meta-tool."""

    async def test_execute_read_only_tool(self, mcp_client):
        """Test executing a read-only proxied tool (list labels)."""
        # Step 1: Get schema hash
        details_result = await mcp_client.call_tool(
            "ha_get_tool_details",
            {"tool_name": "ha_config_get_label"},
        )
        details = assert_mcp_success(details_result, "Get details")
        schema_hash = details["schema_hash"]

        # Step 2: Execute through proxy
        exec_result = await mcp_client.call_tool(
            "ha_execute_tool",
            {
                "tool_name": "ha_config_get_label",
                "args": json.dumps({}),
                "tool_schema": schema_hash,
            },
        )

        data = assert_mcp_success(exec_result, "Execute list labels")
        assert "labels" in data or "count" in data, (
            f"Expected label data in response: {data}"
        )
        logger.info("Proxy execute of ha_config_get_label succeeded")

    async def test_execute_rejects_wrong_schema(self, mcp_client):
        """Test that execute rejects calls with wrong schema hash."""
        result = await mcp_client.call_tool(
            "ha_execute_tool",
            {
                "tool_name": "ha_config_get_label",
                "args": "{}",
                "tool_schema": "wrong_hash",
            },
        )

        data = parse_mcp_result(result)
        assert data.get("success") is False, (
            "Should reject wrong schema hash"
        )
        logger.info("Wrong schema hash correctly rejected")

    async def test_execute_rejects_missing_tool(self, mcp_client):
        """Test that execute rejects calls for non-existent tools."""
        result = await mcp_client.call_tool(
            "ha_execute_tool",
            {
                "tool_name": "ha_nonexistent_xyz",
                "args": "{}",
                "tool_schema": "anything",
            },
        )

        data = parse_mcp_result(result)
        assert data.get("success") is False
        logger.info("Non-existent tool correctly rejected")

    async def test_execute_rejects_invalid_json(self, mcp_client):
        """Test that execute rejects invalid JSON in args."""
        # Get a valid schema hash first
        details_result = await mcp_client.call_tool(
            "ha_get_tool_details",
            {"tool_name": "ha_config_get_label"},
        )
        details = assert_mcp_success(details_result, "Get details")
        schema_hash = details["schema_hash"]

        result = await mcp_client.call_tool(
            "ha_execute_tool",
            {
                "tool_name": "ha_config_get_label",
                "args": "not valid json",
                "tool_schema": schema_hash,
            },
        )

        data = parse_mcp_result(result)
        assert data.get("success") is False
        logger.info("Invalid JSON correctly rejected")

    async def test_full_discovery_and_execute_flow(self, mcp_client):
        """Test the complete proxy flow: find → details → execute.

        This simulates what an LLM would do:
        1. Search for tools
        2. Get details for a specific tool
        3. Execute it with the schema hash
        """
        # 1. Find tools
        find_result = await mcp_client.call_tool(
            "ha_find_tools", {"query": "label"}
        )
        find_data = assert_mcp_success(find_result, "Find label tools")
        assert find_data["count"] > 0

        # Pick the get_label tool from results
        tool_name = None
        for match in find_data["matches"]:
            if "get_label" in match["tool_name"]:
                tool_name = match["tool_name"]
                break
        assert tool_name, "Should find a get_label tool"
        logger.info(f"Step 1: Found tool '{tool_name}'")

        # 2. Get details
        details_result = await mcp_client.call_tool(
            "ha_get_tool_details", {"tool_name": tool_name}
        )
        details_data = assert_mcp_success(
            details_result, "Get tool details"
        )
        assert "schema_hash" in details_data
        schema_hash = details_data["schema_hash"]
        logger.info(f"Step 2: Got schema_hash '{schema_hash}'")

        # 3. Execute
        exec_result = await mcp_client.call_tool(
            "ha_execute_tool",
            {
                "tool_name": tool_name,
                "args": json.dumps({}),
                "tool_schema": schema_hash,
            },
        )
        exec_data = assert_mcp_success(exec_result, "Execute tool")
        logger.info(f"Step 3: Tool executed successfully: {list(exec_data.keys())}")
