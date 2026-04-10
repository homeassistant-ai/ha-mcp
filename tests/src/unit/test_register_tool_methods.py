"""Unit tests for register_tool_methods helper."""

import logging
from unittest.mock import MagicMock

from fastmcp.tools import tool

from ha_mcp.tools.helpers import register_tool_methods


class _SampleTools:
    @tool(name="ha_sample_tool", tags={"Test"})
    async def ha_sample_tool(self) -> dict:
        """Sample tool."""
        return {"success": True}

    async def not_a_tool(self) -> None:
        """Plain method without @tool decorator."""

    def _private_helper(self) -> None:
        pass


class _EmptyTools:
    def plain_method(self) -> None:
        pass


class TestRegisterToolMethods:
    def test_discovers_tool_decorated_methods(self):
        mcp = MagicMock()
        instance = _SampleTools()
        register_tool_methods(mcp, instance)

        assert mcp.add_tool.call_count == 1
        registered = mcp.add_tool.call_args[0][0]
        assert registered.__name__ == "ha_sample_tool"

    def test_skips_non_tool_methods(self):
        mcp = MagicMock()
        register_tool_methods(mcp, _SampleTools())

        registered_names = [
            call.args[0].__name__ for call in mcp.add_tool.call_args_list
        ]
        assert "not_a_tool" not in registered_names
        assert "_private_helper" not in registered_names

    def test_warns_on_zero_tools(self, caplog):
        mcp = MagicMock()
        with caplog.at_level(logging.WARNING):
            register_tool_methods(mcp, _EmptyTools())

        assert mcp.add_tool.call_count == 0
        assert "No @tool-decorated methods found on _EmptyTools" in caplog.text
