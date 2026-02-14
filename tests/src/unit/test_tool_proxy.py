"""
Unit tests for the Tool Search Proxy pattern.

Tests the proxy registry, meta-tools, and schema enforcement
without requiring a live Home Assistant instance.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tool_proxy import (
    PROXY_MODULES,
    ToolProxyRegistry,
    _MockMCP,
    register_proxy_tools,
)


# ─── ToolProxyRegistry unit tests ───


class TestToolProxyRegistry:
    """Test the server-side tool registry."""

    def setup_method(self):
        self.registry = ToolProxyRegistry()
        # Register some sample tools
        self.registry.register_tool(
            name="ha_get_zone",
            description="Get zone information - list all zones or get details for a specific one.\n\nEXAMPLES:\n- List all zones: ha_get_zone()",
            parameters={
                "type": "object",
                "properties": {
                    "zone_id": {
                        "type": "string",
                        "description": "Zone ID to get details for.",
                    }
                },
            },
            annotations={"readOnlyHint": True, "tags": ["zone"], "title": "Get Zone"},
            implementation=AsyncMock(return_value={"success": True, "zones": []}),
            module="tools_zones",
        )
        self.registry.register_tool(
            name="ha_create_zone",
            description="Create a new Home Assistant zone for presence detection.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Display name"},
                    "latitude": {"type": "number", "description": "Latitude"},
                    "longitude": {"type": "number", "description": "Longitude"},
                },
                "required": ["name", "latitude", "longitude"],
            },
            annotations={"destructiveHint": True, "tags": ["zone"], "title": "Create Zone"},
            implementation=AsyncMock(return_value={"success": True}),
            module="tools_zones",
        )
        self.registry.register_tool(
            name="ha_config_get_label",
            description="Get label info - list all labels or get a specific one by ID.",
            parameters={
                "type": "object",
                "properties": {
                    "label_id": {"type": "string", "description": "Label ID"},
                },
            },
            annotations={"readOnlyHint": True, "tags": ["label"], "title": "Get Label"},
            implementation=AsyncMock(return_value={"success": True, "labels": []}),
            module="tools_labels",
        )

    def test_tool_count(self):
        assert self.registry.tool_count == 3

    def test_find_by_name(self):
        results = self.registry.find_tools("ha_get_zone")
        assert len(results) == 1
        assert results[0]["tool_name"] == "ha_get_zone"

    def test_find_by_category(self):
        results = self.registry.find_tools("zone")
        assert len(results) == 2
        names = {r["tool_name"] for r in results}
        assert names == {"ha_get_zone", "ha_create_zone"}

    def test_find_by_keyword(self):
        results = self.registry.find_tools("label")
        assert len(results) == 1
        assert results[0]["tool_name"] == "ha_config_get_label"

    def test_find_no_match(self):
        results = self.registry.find_tools("nonexistent_tool")
        assert len(results) == 0

    def test_find_case_insensitive(self):
        results = self.registry.find_tools("ZONE")
        assert len(results) == 2

    def test_get_details_existing(self):
        details = self.registry.get_tool_details("ha_get_zone")
        assert details is not None
        assert details["tool_name"] == "ha_get_zone"
        assert "description" in details
        assert "parameters" in details
        assert "schema_hash" in details
        assert len(details["schema_hash"]) == 8  # md5[:8]

    def test_get_details_missing(self):
        details = self.registry.get_tool_details("nonexistent")
        assert details is None

    def test_get_details_has_parameters(self):
        details = self.registry.get_tool_details("ha_create_zone")
        assert details is not None
        params = details["parameters"]
        assert len(params) == 3
        names = {p["name"] for p in params}
        assert names == {"name", "latitude", "longitude"}
        # Check required flags
        required_params = [p for p in params if p["required"]]
        assert len(required_params) == 3

    def test_schema_hash_validates(self):
        details = self.registry.get_tool_details("ha_get_zone")
        assert details is not None
        assert self.registry.validate_schema("ha_get_zone", details["schema_hash"])

    def test_schema_hash_rejects_wrong(self):
        assert not self.registry.validate_schema("ha_get_zone", "wrong_hash")

    def test_schema_hash_rejects_empty(self):
        assert not self.registry.validate_schema("ha_get_zone", "")

    def test_schema_hash_rejects_missing_tool(self):
        assert not self.registry.validate_schema("nonexistent", "anything")

    def test_get_catalog(self):
        catalog = self.registry.get_catalog()
        assert "zone" in catalog
        assert "label" in catalog
        assert len(catalog["zone"]) == 2
        assert len(catalog["label"]) == 1

    def test_summary_includes_destructive_flag(self):
        results = self.registry.find_tools("ha_create_zone")
        assert len(results) == 1
        assert results[0]["is_destructive"] is True

    def test_summary_readonly_flag(self):
        results = self.registry.find_tools("ha_get_zone")
        assert len(results) == 1
        assert results[0]["is_destructive"] is False


# ─── MockMCP tests ───


class TestMockMCP:
    """Test that the mock captures tool registrations correctly."""

    def test_captures_decorated_function(self):
        mock = _MockMCP()

        @mock.tool(annotations={"readOnlyHint": True, "tags": ["test"]})
        async def ha_test_tool(name: str) -> dict:
            """A test tool."""
            return {"result": name}

        assert len(mock.captured_tools) == 1
        assert mock.captured_tools[0]["name"] == "ha_test_tool"
        assert mock.captured_tools[0]["description"] == "A test tool."
        assert mock.captured_tools[0]["annotations"]["readOnlyHint"] is True

    def test_captures_multiple_tools(self):
        mock = _MockMCP()

        @mock.tool(annotations={})
        async def tool_a() -> dict:
            """Tool A."""
            return {}

        @mock.tool(annotations={})
        async def tool_b() -> dict:
            """Tool B."""
            return {}

        assert len(mock.captured_tools) == 2


# ─── Meta-tool integration tests ───


class TestMetaToolIntegration:
    """Test the meta-tools (ha_find_tools, ha_get_tool_details, ha_execute_tool)."""

    def setup_method(self):
        """Set up a mock MCP and register proxy tools."""
        self.mcp = _MockMCP()
        self.registry = ToolProxyRegistry()

        # Add a test tool to the registry
        self.test_impl = AsyncMock(
            return_value={"success": True, "zones": [], "count": 0}
        )
        self.registry.register_tool(
            name="ha_get_zone",
            description="Get zone information.",
            parameters={
                "type": "object",
                "properties": {
                    "zone_id": {"type": "string", "description": "Zone ID"},
                },
            },
            annotations={"readOnlyHint": True, "tags": ["zone"]},
            implementation=self.test_impl,
            module="tools_zones",
        )

        # Register meta-tools using the mock MCP
        register_proxy_tools(self.mcp, MagicMock(), self.registry)

        # Extract the registered meta-tool implementations
        self.meta_tools = {t["name"]: t["implementation"] for t in self.mcp.captured_tools}

    @pytest.mark.asyncio
    async def test_find_tools_returns_results(self):
        result = await self.meta_tools["ha_find_tools"](query="zone")
        assert result["success"] is True
        assert result["count"] == 1
        assert result["matches"][0]["tool_name"] == "ha_get_zone"

    @pytest.mark.asyncio
    async def test_find_tools_no_results(self):
        result = await self.meta_tools["ha_find_tools"](query="nonexistent")
        assert result["success"] is True
        assert result["count"] == 0
        assert "available_categories" in result

    @pytest.mark.asyncio
    async def test_get_tool_details_success(self):
        result = await self.meta_tools["ha_get_tool_details"](tool_name="ha_get_zone")
        assert result["success"] is True
        assert result["tool_name"] == "ha_get_zone"
        assert "schema_hash" in result
        assert "parameters" in result
        assert "usage" in result

    @pytest.mark.asyncio
    async def test_get_tool_details_not_found(self):
        result = await self.meta_tools["ha_get_tool_details"](tool_name="fake_tool")
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_success(self):
        # First get the schema hash
        details = await self.meta_tools["ha_get_tool_details"](tool_name="ha_get_zone")
        schema_hash = details["schema_hash"]

        # Execute with valid schema hash
        result = await self.meta_tools["ha_execute_tool"](
            tool_name="ha_get_zone",
            args=json.dumps({}),
            tool_schema=schema_hash,
        )
        assert result["success"] is True
        self.test_impl.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_tool_rejects_wrong_schema(self):
        result = await self.meta_tools["ha_execute_tool"](
            tool_name="ha_get_zone",
            args="{}",
            tool_schema="wrong_hash",
        )
        assert result["success"] is False
        assert "schema_hash" in result["error"].lower() or "tool_schema" in result["error"].lower()
        self.test_impl.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_tool_rejects_missing_tool(self):
        result = await self.meta_tools["ha_execute_tool"](
            tool_name="nonexistent",
            args="{}",
            tool_schema="anything",
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_rejects_invalid_json(self):
        details = await self.meta_tools["ha_get_tool_details"](tool_name="ha_get_zone")
        result = await self.meta_tools["ha_execute_tool"](
            tool_name="ha_get_zone",
            args="not json",
            tool_schema=details["schema_hash"],
        )
        assert result["success"] is False
        assert "JSON" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_rejects_missing_required_params(self):
        """Test that execute validates required parameters."""
        # Add a tool with required params
        self.registry.register_tool(
            name="ha_create_zone",
            description="Create zone.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "latitude": {"type": "number"},
                },
                "required": ["name", "latitude"],
            },
            annotations={"destructiveHint": True, "tags": ["zone"]},
            implementation=AsyncMock(),
            module="tools_zones",
        )

        # Get schema hash for create_zone
        details = await self.meta_tools["ha_get_tool_details"](tool_name="ha_create_zone")
        schema_hash = details["schema_hash"]

        # Try to execute without required params
        result = await self.meta_tools["ha_execute_tool"](
            tool_name="ha_create_zone",
            args=json.dumps({"name": "Test"}),  # missing latitude
            tool_schema=schema_hash,
        )
        assert result["success"] is False
        assert "latitude" in result["error"]


# ─── PROXY_MODULES config tests ───


class TestProxyModulesConfig:
    """Test that the proxy module configuration is correct."""

    def test_proxy_modules_are_valid_module_names(self):
        """All proxy modules should follow the tools_*.py naming convention."""
        for module_name in PROXY_MODULES:
            assert module_name.startswith("tools_"), (
                f"Proxy module '{module_name}' doesn't follow tools_*.py convention"
            )

    def test_proxy_modules_count(self):
        """Phase 1 should have exactly 5 proxy modules."""
        assert len(PROXY_MODULES) == 5

    def test_expected_modules_present(self):
        """Verify the expected Phase 1 modules are configured."""
        expected = {
            "tools_zones",
            "tools_labels",
            "tools_addons",
            "tools_voice_assistant",
            "tools_traces",
        }
        assert PROXY_MODULES == expected
