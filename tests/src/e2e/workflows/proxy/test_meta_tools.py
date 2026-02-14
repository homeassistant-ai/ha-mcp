"""E2E tests for the 3 proxy meta-tools against a live HA instance."""

import json
import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
class TestFindTools:
    async def test_find_by_category(self, mcp_client):
        result = await mcp_client.call_tool("ha_find_tools", {"query": "zone"})
        data = assert_mcp_success(result, "find by category")
        assert data["count"] > 0 and "matches" in data
        assert any("zone" in m["tool_name"] for m in data["matches"])

    async def test_find_by_tool_name(self, mcp_client):
        result = await mcp_client.call_tool("ha_find_tools", {"query": "ha_get_addon"})
        data = assert_mcp_success(result, "find by name")
        assert any(m["tool_name"] == "ha_get_addon" for m in data["matches"])

    async def test_find_no_results(self, mcp_client):
        result = await mcp_client.call_tool(
            "ha_find_tools", {"query": "zzz_nonexistent_xyz"},
        )
        data = assert_mcp_success(result, "find nonexistent")
        assert data["count"] == 0 and "available_categories" in data

    async def test_find_by_keyword(self, mcp_client):
        result = await mcp_client.call_tool("ha_find_tools", {"query": "label"})
        data = assert_mcp_success(result, "find by keyword")
        assert data["count"] > 0


@pytest.mark.asyncio
class TestGetToolDetails:
    async def test_get_details_success(self, mcp_client):
        result = await mcp_client.call_tool(
            "ha_get_tool_details", {"tool_name": "ha_config_get_label"},
        )
        data = assert_mcp_success(result, "get details")
        assert data["tool_name"] == "ha_config_get_label"
        for key in ("description", "parameters", "schema_hash", "usage"):
            assert key in data
        assert len(data["schema_hash"]) == 8

    async def test_get_details_not_found(self, mcp_client):
        result = await mcp_client.call_tool(
            "ha_get_tool_details", {"tool_name": "ha_nonexistent_xyz"},
        )
        assert parse_mcp_result(result).get("success") is False

    async def test_get_details_has_parameters(self, mcp_client):
        result = await mcp_client.call_tool(
            "ha_get_tool_details", {"tool_name": "ha_create_zone"},
        )
        params = assert_mcp_success(result, "zone details").get("parameters", [])
        assert len(params) > 0
        assert all("name" in p and "type" in p and "required" in p for p in params)


@pytest.mark.asyncio
class TestExecuteTool:
    async def _get_hash(self, mcp_client, tool_name: str) -> str:
        """Helper: get schema_hash for a tool."""
        r = await mcp_client.call_tool("ha_get_tool_details", {"tool_name": tool_name})
        return assert_mcp_success(r, "get hash")["schema_hash"]

    async def test_execute_read_only_tool(self, mcp_client):
        h = await self._get_hash(mcp_client, "ha_config_get_label")
        result = await mcp_client.call_tool("ha_execute_tool", {
            "tool_name": "ha_config_get_label",
            "args": json.dumps({}), "tool_schema": h,
        })
        data = assert_mcp_success(result, "execute label list")
        assert "labels" in data or "count" in data

    async def test_execute_rejects_wrong_schema(self, mcp_client):
        result = await mcp_client.call_tool("ha_execute_tool", {
            "tool_name": "ha_config_get_label",
            "args": "{}", "tool_schema": "wrong_hash",
        })
        assert parse_mcp_result(result).get("success") is False

    async def test_execute_rejects_missing_tool(self, mcp_client):
        result = await mcp_client.call_tool("ha_execute_tool", {
            "tool_name": "ha_nonexistent_xyz",
            "args": "{}", "tool_schema": "anything",
        })
        assert parse_mcp_result(result).get("success") is False

    async def test_execute_rejects_invalid_json(self, mcp_client):
        h = await self._get_hash(mcp_client, "ha_config_get_label")
        result = await mcp_client.call_tool("ha_execute_tool", {
            "tool_name": "ha_config_get_label",
            "args": "not valid json", "tool_schema": h,
        })
        assert parse_mcp_result(result).get("success") is False

    async def test_full_discovery_and_execute_flow(self, mcp_client):
        """Complete proxy flow: find -> details -> execute."""
        # 1. Find
        find_data = assert_mcp_success(
            await mcp_client.call_tool("ha_find_tools", {"query": "label"}),
            "find",
        )
        tool_name = next(
            (m["tool_name"] for m in find_data["matches"] if "get_label" in m["tool_name"]),
            None,
        )
        assert tool_name, "Should find a get_label tool"

        # 2. Details
        details = assert_mcp_success(
            await mcp_client.call_tool("ha_get_tool_details", {"tool_name": tool_name}),
            "details",
        )
        assert "schema_hash" in details

        # 3. Execute
        assert_mcp_success(
            await mcp_client.call_tool("ha_execute_tool", {
                "tool_name": tool_name,
                "args": json.dumps({}),
                "tool_schema": details["schema_hash"],
            }),
            "execute",
        )
