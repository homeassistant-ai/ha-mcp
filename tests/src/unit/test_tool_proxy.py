"""Unit tests for the Tool Search Proxy."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tool_proxy import (
    PROXY_MODULES,
    ToolProxyRegistry,
    _MockMCP,
    register_proxy_tools,
)


def _make_registry() -> ToolProxyRegistry:
    """Build a small registry with zone + label tools for testing."""
    reg = ToolProxyRegistry()
    reg.register_tool(
        name="ha_get_zone",
        description="Get zone information.",
        parameters={"type": "object", "properties": {
            "zone_id": {"type": "string", "description": "Zone ID"},
        }},
        annotations={"readOnlyHint": True, "tags": ["zone"]},
        implementation=AsyncMock(return_value={"success": True, "zones": []}),
        module="tools_zones",
    )
    reg.register_tool(
        name="ha_create_zone",
        description="Create a new zone.",
        parameters={"type": "object", "properties": {
            "name": {"type": "string"}, "latitude": {"type": "number"},
            "longitude": {"type": "number"},
        }, "required": ["name", "latitude", "longitude"]},
        annotations={"destructiveHint": True, "tags": ["zone"]},
        implementation=AsyncMock(return_value={"success": True}),
        module="tools_zones",
    )
    reg.register_tool(
        name="ha_config_get_label",
        description="Get label info.",
        parameters={"type": "object", "properties": {
            "label_id": {"type": "string"},
        }},
        annotations={"readOnlyHint": True, "tags": ["label"]},
        implementation=AsyncMock(return_value={"success": True, "labels": []}),
        module="tools_labels",
    )
    return reg


class TestToolProxyRegistry:
    def setup_method(self):
        self.registry = _make_registry()

    def test_tool_count(self):
        assert self.registry.tool_count == 3

    def test_find_by_name(self):
        results = self.registry.find_tools("ha_get_zone")
        assert len(results) == 1
        assert results[0]["tool_name"] == "ha_get_zone"

    def test_find_by_category(self):
        results = self.registry.find_tools("zone")
        assert {r["tool_name"] for r in results} == {"ha_get_zone", "ha_create_zone"}

    def test_find_by_keyword(self):
        assert self.registry.find_tools("label")[0]["tool_name"] == "ha_config_get_label"

    def test_find_no_match(self):
        assert len(self.registry.find_tools("nonexistent")) == 0

    def test_find_case_insensitive(self):
        assert len(self.registry.find_tools("ZONE")) == 2

    def test_get_details_existing(self):
        d = self.registry.get_tool_details("ha_get_zone")
        assert d is not None
        assert d["tool_name"] == "ha_get_zone"
        for key in ("description", "parameters", "schema_hash"):
            assert key in d
        assert len(d["schema_hash"]) == 8

    def test_get_details_missing(self):
        assert self.registry.get_tool_details("nonexistent") is None

    def test_get_details_parameters(self):
        d = self.registry.get_tool_details("ha_create_zone")
        assert d is not None
        names = {p["name"] for p in d["parameters"]}
        assert names == {"name", "latitude", "longitude"}
        assert all(p["required"] for p in d["parameters"])

    def test_schema_hash_validates(self):
        d = self.registry.get_tool_details("ha_get_zone")
        assert self.registry.validate_schema("ha_get_zone", d["schema_hash"])

    def test_schema_hash_rejects_wrong(self):
        assert not self.registry.validate_schema("ha_get_zone", "wrong")

    def test_schema_hash_rejects_missing_tool(self):
        assert not self.registry.validate_schema("nonexistent", "x")

    def test_get_catalog(self):
        cat = self.registry.get_catalog()
        assert len(cat["zone"]) == 2 and len(cat["label"]) == 1

    def test_summary_destructive_flag(self):
        r = self.registry.find_tools("ha_create_zone")
        assert r[0]["is_destructive"] is True
        r2 = self.registry.find_tools("ha_get_zone")
        assert r2[0]["is_destructive"] is False


class TestMockMCP:
    def test_captures_decorated_function(self):
        mock = _MockMCP()

        @mock.tool(annotations={"readOnlyHint": True, "tags": ["test"]})
        async def ha_test_tool(name: str) -> dict:
            """A test tool."""
            return {"result": name}

        assert len(mock.captured_tools) == 1
        assert mock.captured_tools[0]["name"] == "ha_test_tool"
        assert mock.captured_tools[0]["description"] == "A test tool."

    def test_captures_multiple_tools(self):
        mock = _MockMCP()

        @mock.tool(annotations={})
        async def tool_a() -> dict:
            """A."""
            return {}

        @mock.tool(annotations={})
        async def tool_b() -> dict:
            """B."""
            return {}

        assert len(mock.captured_tools) == 2


class TestMetaToolIntegration:
    def setup_method(self):
        self.mcp = _MockMCP()
        self.registry = _make_registry()
        self.test_impl = self.registry.get_tool("ha_get_zone")["implementation"]
        register_proxy_tools(self.mcp, MagicMock(), self.registry)
        self.meta = {t["name"]: t["implementation"] for t in self.mcp.captured_tools}

    @pytest.mark.asyncio
    async def test_find_tools_results(self):
        r = await self.meta["ha_find_tools"](query="zone")
        assert r["success"] and r["count"] == 2

    @pytest.mark.asyncio
    async def test_find_tools_no_results(self):
        r = await self.meta["ha_find_tools"](query="nonexistent")
        assert r["success"] and r["count"] == 0 and "available_categories" in r

    @pytest.mark.asyncio
    async def test_get_details_success(self):
        r = await self.meta["ha_get_tool_details"](tool_name="ha_get_zone")
        assert r["success"] and "schema_hash" in r and "usage" in r

    @pytest.mark.asyncio
    async def test_get_details_not_found(self):
        r = await self.meta["ha_get_tool_details"](tool_name="fake")
        assert r["success"] is False

    @pytest.mark.asyncio
    async def test_execute_success(self):
        d = await self.meta["ha_get_tool_details"](tool_name="ha_get_zone")
        r = await self.meta["ha_execute_tool"](
            tool_name="ha_get_zone", args=json.dumps({}), tool_schema=d["schema_hash"],
        )
        assert r["success"] is True
        self.test_impl.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_rejects_wrong_schema(self):
        r = await self.meta["ha_execute_tool"](
            tool_name="ha_get_zone", args="{}", tool_schema="wrong",
        )
        assert r["success"] is False
        self.test_impl.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_rejects_missing_tool(self):
        r = await self.meta["ha_execute_tool"](
            tool_name="nope", args="{}", tool_schema="x",
        )
        assert r["success"] is False

    @pytest.mark.asyncio
    async def test_execute_rejects_invalid_json(self):
        d = await self.meta["ha_get_tool_details"](tool_name="ha_get_zone")
        r = await self.meta["ha_execute_tool"](
            tool_name="ha_get_zone", args="not json", tool_schema=d["schema_hash"],
        )
        assert r["success"] is False

    @pytest.mark.asyncio
    async def test_execute_rejects_missing_required_params(self):
        d = await self.meta["ha_get_tool_details"](tool_name="ha_create_zone")
        r = await self.meta["ha_execute_tool"](
            tool_name="ha_create_zone",
            args=json.dumps({"name": "Test"}),
            tool_schema=d["schema_hash"],
        )
        assert r["success"] is False
        assert "latitude" in r["error"]["message"]


class TestProxyModulesConfig:
    def test_expected_modules(self):
        expected = {"tools_zones", "tools_labels", "tools_addons",
                    "tools_voice_assistant", "tools_traces"}
        assert PROXY_MODULES == expected
        assert all(m.startswith("tools_") for m in PROXY_MODULES)
