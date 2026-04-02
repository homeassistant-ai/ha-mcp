"""
End-to-End tests for ha_manage_custom_tool (sandboxed code execution).

This test suite validates:
- Feature flag behavior (disabled by default, enabled with ENABLE_CODE_MODE)
- Basic sandbox code execution and result return
- call_tool bridge to existing MCP tools
- Sandbox security constraints (no filesystem, no classes, recursive self-call)
- Resource limit enforcement (timeout)
- Input validation (empty code, empty justification, save_as format)
- Saved tools lifecycle (save, run, list, overwrite)

Feature Flag: Set ENABLE_CODE_MODE=true to enable.
"""

import logging
import os

import pytest

from ..utilities.assertions import safe_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEATURE_FLAG = "ENABLE_CODE_MODE"
TOOL_NAME = "ha_manage_custom_tool"


@pytest.fixture(scope="module")
def code_mode_enabled(ha_container_with_fresh_config):
    """Enable code mode feature flag for the test module."""
    os.environ[FEATURE_FLAG] = "true"
    logger.info("Code mode feature flag enabled")
    yield
    os.environ.pop(FEATURE_FLAG, None)


@pytest.fixture
async def mcp_client_with_code_mode(code_mode_enabled, mcp_server):
    """Create MCP client with code mode enabled."""
    from fastmcp import Client

    client = Client(mcp_server.mcp)
    async with client:
        logger.debug("FastMCP client with code mode connected")
        yield client


async def _check_tool_available(mcp_client) -> tuple[bool, str | None]:
    """Check if ha_manage_custom_tool is available in the MCP server."""
    try:
        tools = await mcp_client.list_tools()
        tool_names = [t.name for t in tools]
        if TOOL_NAME not in tool_names:
            return False, f"Tool {TOOL_NAME} not registered"
        return True, None
    except Exception as e:
        return False, f"Error checking tools: {e}"


def _skip_if_unavailable(result: tuple[bool, str | None], test_name: str):
    available, error = result
    if not available:
        pytest.skip(f"{test_name}: {error}")


# ---------------------------------------------------------------------------
# Feature flag / registration
# ---------------------------------------------------------------------------


class TestCodeModeAvailability:
    """Test ha_manage_custom_tool availability and feature flag behavior."""

    async def test_feature_flag_disabled_by_default(self, ha_container_with_fresh_config):
        """Verify tool is NOT registered when feature flag is disabled."""
        import ha_mcp.config as config_mod
        from fastmcp import Client
        from ha_mcp.server import HomeAssistantSmartMCPServer

        # Ensure flag is OFF for this test
        original = os.environ.pop(FEATURE_FLAG, None)
        try:
            config_mod._settings = None  # Reset singleton

            server = HomeAssistantSmartMCPServer(
                client=None,
                server_name="test-disabled",
            )
            client = Client(server.mcp)
            async with client:
                tools = await client.list_tools()
                tool_names = [t.name for t in tools]
                assert TOOL_NAME not in tool_names, (
                    f"Tool should NOT be registered when flag is off, "
                    f"but found in: {tool_names}"
                )
                logger.info("Correctly: tool not registered when flag disabled")
        finally:
            if original:
                os.environ[FEATURE_FLAG] = original
            config_mod._settings = None  # Reset for other tests

    async def test_tool_registered_when_enabled(self, mcp_client_with_code_mode):
        """Verify tool IS registered when feature flag is enabled."""
        available, error = await _check_tool_available(mcp_client_with_code_mode)
        assert available, f"Tool should be registered: {error}"
        logger.info("ha_manage_custom_tool is registered and available")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestCodeModeValidation:
    """Test input validation for ha_manage_custom_tool."""

    async def test_empty_code_rejected(self, mcp_client_with_code_mode):
        """Empty code with no mode set must be rejected."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Empty code validation")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": "", "justification": "testing empty code"},
        )
        assert data.get("success") is False, f"Empty code should fail: {data}"
        logger.info("Correctly rejected empty code")

    async def test_whitespace_code_rejected(self, mcp_client_with_code_mode):
        """Whitespace-only code must be rejected."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Whitespace code validation")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": "   \n  ", "justification": "testing whitespace code"},
        )
        assert data.get("success") is False, f"Whitespace code should fail: {data}"
        logger.info("Correctly rejected whitespace-only code")

    async def test_empty_justification_rejected(self, mcp_client_with_code_mode):
        """Empty justification must be rejected when code is provided."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Empty justification validation")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": "42", "justification": ""},
        )
        assert data.get("success") is False, f"Empty justification should fail: {data}"
        logger.info("Correctly rejected empty justification")

    async def test_invalid_save_as_rejected(self, mcp_client_with_code_mode):
        """Invalid save_as names must be rejected."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Invalid save_as validation")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "42",
                "justification": "test",
                "save_as": "../../bad-name!",
            },
        )
        assert data.get("success") is False, f"Bad save_as should fail: {data}"
        logger.info("Correctly rejected invalid save_as name")

    async def test_no_mode_specified(self, mcp_client_with_code_mode):
        """Calling with no code, no run_saved, no list_saved must error."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "No mode specified")

        data = await safe_call_tool(
            mcp_client_with_code_mode, TOOL_NAME, {}
        )
        assert data.get("success") is False, f"No mode should fail: {data}"
        logger.info("Correctly rejected no-mode call")


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------


class TestCodeModeExecution:
    """Test basic sandbox code execution."""

    async def test_simple_expression(self, mcp_client_with_code_mode):
        """Simple expression returns its value."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Simple expression")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": "2 + 2", "justification": "E2E test: simple arithmetic"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        assert data["data"]["result"] == 4, f"2+2 should equal 4: {data}"
        logger.info("Simple expression returned correct result")

    async def test_dict_result(self, mcp_client_with_code_mode):
        """Code returning a dict works correctly."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Dict result")

        code = 'items = [1, 2, 3, 4, 5]\n{"total": sum(items), "count": len(items)}'
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: dict return value"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        result = data["data"]["result"]
        assert result["total"] == 15, f"Sum should be 15: {data}"
        assert result["count"] == 5, f"Count should be 5: {data}"
        logger.info("Dict result returned correctly")

    async def test_justification_in_response(self, mcp_client_with_code_mode):
        """Justification is nested inside data in the response."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Justification in response")

        justification = "E2E test: verify justification passthrough"
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": "'hello'", "justification": justification},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        assert data["data"]["justification"] == justification, (
            f"Justification should be in data: {data}"
        )
        logger.info("Justification correctly included in response")


# ---------------------------------------------------------------------------
# call_tool bridge
# ---------------------------------------------------------------------------


class TestCodeModeCallTool:
    """Test call_tool bridge to existing MCP tools."""

    async def test_call_tool_get_overview(self, mcp_client_with_code_mode):
        """call_tool can invoke ha_get_overview."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "call_tool bridge")

        code = 'result = await call_tool("ha_get_overview", {})\nresult.get("success", False)'
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: call_tool bridge"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        logger.info("call_tool bridge successfully invoked ha_get_overview")

    async def test_call_tool_search_entities(self, mcp_client_with_code_mode):
        """call_tool can invoke ha_search_entities and return results."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "call_tool search")

        code = (
            'result = await call_tool("ha_search_entities", {"query": "sun"})\n'
            'entities = result.get("data", {}).get("entities", [])\n'
            '{"found": len(entities) > 0, "count": len(entities)}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: call_tool search"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        result = data["data"]["result"]
        assert result["found"] is True, f"Should find entities: {data}"
        logger.info("call_tool bridge searched entities (found %d)", result["count"])

    async def test_call_tool_error_returns_dict(self, mcp_client_with_code_mode):
        """call_tool returns error dict (not exception) when tool fails."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "call_tool error handling")

        code = (
            'result = await call_tool("ha_get_state", '
            '{"entity_id": "nonexistent.entity_12345"})\n'
            'result.get("success", "missing")'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: call_tool error handling"},
        )
        assert data.get("success") is True, (
            f"Sandbox should succeed even when inner tool fails: {data}"
        )
        assert data["data"]["result"] is False, (
            f"Inner tool should return success=False: {data}"
        )
        logger.info("call_tool correctly returned error dict for failed tool")

    async def test_call_tool_nonexistent_tool(self, mcp_client_with_code_mode):
        """call_tool with nonexistent tool returns error dict."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "call_tool nonexistent tool")

        code = (
            'result = await call_tool("ha_nonexistent_tool_xyz", {})\n'
            'result.get("success", "missing")'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: nonexistent tool"},
        )
        assert data.get("success") is True, (
            f"Sandbox should succeed: {data}"
        )
        assert data["data"]["result"] is False, (
            f"Nonexistent tool should return success=False: {data}"
        )
        logger.info("call_tool correctly handled nonexistent tool")


# ---------------------------------------------------------------------------
# Sandbox security
# ---------------------------------------------------------------------------


class TestCodeModeSecurity:
    """Test sandbox security constraints."""

    async def test_no_filesystem_access(self, mcp_client_with_code_mode):
        """Sandbox must block filesystem access."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "No filesystem access")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "open('/etc/passwd').read()",
                "justification": "E2E test: verify filesystem blocked",
            },
        )
        assert data.get("success") is False, (
            f"Filesystem access should be blocked: {data}"
        )
        error_details = str(data.get("error", ""))
        assert "open" in error_details.lower() or "not defined" in error_details.lower() or "sandbox" in error_details.lower(), (
            f"Error should mention the sandbox violation, got: {error_details}"
        )
        logger.info("Correctly blocked filesystem access")

    async def test_no_class_definitions(self, mcp_client_with_code_mode):
        """Sandbox must block class definitions."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "No class definitions")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "class Foo:\n    pass\nFoo()",
                "justification": "E2E test: verify class definitions blocked",
            },
        )
        assert data.get("success") is False, (
            f"Class definitions should be blocked: {data}"
        )
        error_details = str(data.get("error", ""))
        assert "class" in error_details.lower() or "syntax" in error_details.lower(), (
            f"Error should mention class/syntax violation, got: {error_details}"
        )
        logger.info("Correctly blocked class definitions")

    async def test_syntax_error_handled(self, mcp_client_with_code_mode):
        """Syntax errors return structured error, not crash."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Syntax error handling")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "def foo(:\n  pass",
                "justification": "E2E test: syntax error handling",
            },
        )
        assert data.get("success") is False, (
            f"Syntax error should return failure: {data}"
        )
        error_details = str(data.get("error", ""))
        assert "syntax" in error_details.lower() or "parse" in error_details.lower(), (
            f"Error should mention syntax, got: {error_details}"
        )
        logger.info("Correctly handled syntax error")

    async def test_recursive_self_call_blocked(self, mcp_client_with_code_mode):
        """Sandbox code must not be able to call ha_manage_custom_tool recursively."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Recursive self-call")

        code = (
            'result = await call_tool("ha_manage_custom_tool", '
            '{"code": "1+1", "justification": "nested"})\n'
            'result'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: recursive self-call"},
        )
        assert data.get("success") is True, (
            f"Sandbox should succeed (blocked call returns error dict): {data}"
        )
        result = data["data"]["result"]
        assert result.get("success") is False, (
            f"Recursive call should be blocked: {result}"
        )
        assert "cannot be called" in result.get("error", {}).get("message", ""), (
            f"Should explain why blocked: {result}"
        )
        logger.info("Correctly blocked recursive self-invocation")


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


class TestCodeModeResourceLimits:
    """Test resource limit enforcement."""

    async def test_timeout_enforced(self, mcp_client_with_code_mode):
        """Code that exceeds the time limit is terminated."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Timeout enforcement")

        code = "i = 0\nwhile True:\n    i += 1\ni"
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: timeout enforcement"},
        )
        assert data.get("success") is False, (
            f"Infinite loop should be terminated: {data}"
        )
        error_details = str(data.get("error", ""))
        assert "duration" in error_details.lower() or "time" in error_details.lower() or "limit" in error_details.lower(), (
            f"Error should mention timeout/duration, got: {error_details}"
        )
        logger.info("Correctly terminated code that exceeded time limit")


# ---------------------------------------------------------------------------
# Saved tools
# ---------------------------------------------------------------------------


class TestSavedTools:
    """Test save/run/list workflow for custom tools."""

    async def test_save_and_run(self, mcp_client_with_code_mode):
        """Save a tool via save_as, then re-run it via run_saved."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Save and run")

        # Create and save
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "40 + 2",
                "justification": "E2E test: save and rerun",
                "save_as": "e2e_answer",
            },
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        assert data["data"]["result"] == 42, f"Should return 42: {data}"
        assert data["data"].get("saved_as") == "e2e_answer", (
            f"Should confirm save: {data}"
        )

        # Re-run saved tool
        data2 = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"run_saved": "e2e_answer"},
        )
        assert data2.get("success") is True, f"Re-run should succeed: {data2}"
        assert data2["data"]["result"] == 42, f"Re-run should return 42: {data2}"
        assert data2["data"]["saved_tool"] == "e2e_answer", (
            f"Should reference saved tool: {data2}"
        )
        logger.info("Save and re-run workflow works correctly")

    async def test_overwrite_saved_tool(self, mcp_client_with_code_mode):
        """Saving with the same name overwrites the previous tool."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Overwrite saved tool")

        # Save v1
        await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": "1", "justification": "v1", "save_as": "e2e_overwrite"},
        )

        # Save v2 with same name
        await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": "2", "justification": "v2", "save_as": "e2e_overwrite"},
        )

        # Run — should get v2
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"run_saved": "e2e_overwrite"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        assert data["data"]["result"] == 2, (
            f"Should run v2 (overwritten), got: {data}"
        )
        logger.info("Overwrite correctly replaced saved tool")

    async def test_list_saved_tools(self, mcp_client_with_code_mode):
        """list_saved=True returns saved tools."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "List saved tools")

        # Save a tool first
        save_data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "'listed'",
                "justification": "E2E test: list saved tools",
                "save_as": "e2e_listed",
            },
        )
        assert save_data.get("success") is True, f"Save should succeed: {save_data}"

        # List
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"list_saved": True},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        tools = data.get("data", {})
        assert "e2e_listed" in tools, f"Should contain saved tool: {data}"
        assert tools["e2e_listed"]["code"] == "'listed'", (
            f"Code should match: {data}"
        )
        logger.info("List saved tools returns correct data")

    async def test_run_nonexistent_saved_tool(self, mcp_client_with_code_mode):
        """Running a nonexistent saved tool returns error."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Run nonexistent saved tool")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"run_saved": "nonexistent_e2e_tool_12345"},
        )
        assert data.get("success") is False, (
            f"Nonexistent tool should fail: {data}"
        )
        logger.info("Correctly rejected nonexistent saved tool")
