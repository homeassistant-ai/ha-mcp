"""Unit tests for the deep-search branch's error handling inside ha_search.

ha_deep_search is no longer @mcp.tool-decorated — it is invoked only as an
internal helper by the merged ``ha_search`` orchestrator. Errors raised by
the helper are captured by ``asyncio.gather(..., return_exceptions=True)``
and surfaced via the orchestrator's ``partial=True`` + ``errors[]`` path.
The structured-error formatting (no traceback / error_type leak, code +
message + suggestions) is still provided by ``exception_to_structured_error``
inside the helper and survives the gather capture as a stringified
``ToolError`` payload (issue #517).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_search import register_search_tools


def _extract_deep_search_error(response: dict) -> dict:
    """Parse the captured ToolError JSON for the configs (deep-search) branch."""
    assert response.get("partial") is True, "expected partial=True after helper failure"
    errors = response.get("errors", [])
    deep_errors = [e for e in errors if e.get("surface") == "configs"]
    assert deep_errors, f"expected one configs-surface error, got {errors}"
    # ``error`` is the str() of the captured ToolError, which is structured JSON.
    return json.loads(deep_errors[0]["error"])


class TestDeepSearchErrorHandling:
    """The deep-search helper's structured error survives the orchestrator capture."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_add_tool(method):
            name = (
                method.__fastmcp__.name
                if hasattr(method, "__fastmcp__")
                else method.__name__
            )
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        smart_tools = MagicMock()
        smart_tools.deep_search = AsyncMock()
        return smart_tools

    @pytest.fixture
    def ha_search_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_search"]

    @pytest.mark.asyncio
    async def test_error_does_not_leak_traceback_or_raw_fields(
        self, mock_smart_tools, ha_search_tool
    ):
        mock_smart_tools.deep_search = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )

        response = await ha_search_tool(query="test_query")

        err = _extract_deep_search_error(response)
        assert err["success"] is False
        assert isinstance(err["error"], dict), (
            "error must be structured dict, not raw string"
        )
        assert "code" in err["error"]
        assert "message" in err["error"]
        assert "traceback" not in err
        assert "error_type" not in err

    @pytest.mark.asyncio
    async def test_error_includes_search_specific_suggestions(
        self, mock_smart_tools, ha_search_tool
    ):
        mock_smart_tools.deep_search = AsyncMock(
            side_effect=RuntimeError("Something went wrong")
        )

        response = await ha_search_tool(query="test_query")

        err = _extract_deep_search_error(response)
        suggestions = err["error"]["suggestions"]
        assert "Check Home Assistant connection" in suggestions
        assert "Try simpler search terms" in suggestions

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exception_cls,exception_msg,expected_code",
        [
            (ValueError, "invalid input", "VALIDATION_FAILED"),
            (TimeoutError, "timed out", "TIMEOUT_OPERATION"),
            (RuntimeError, "unexpected failure", "INTERNAL_ERROR"),
        ],
    )
    async def test_different_exception_types_produce_correct_error_codes(
        self,
        mock_smart_tools,
        ha_search_tool,
        exception_cls,
        exception_msg,
        expected_code,
    ):
        mock_smart_tools.deep_search = AsyncMock(
            side_effect=exception_cls(exception_msg)
        )

        response = await ha_search_tool(query="test_query")

        err = _extract_deep_search_error(response)
        assert err["success"] is False
        assert err["error"]["code"] == expected_code
