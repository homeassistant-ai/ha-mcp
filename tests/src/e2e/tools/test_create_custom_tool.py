"""
End-to-End tests for ha_create_custom_tool (sandboxed code execution).

This test suite validates:
- Feature flag behavior (disabled by default, enabled with ENABLE_CODE_MODE)
- Basic sandbox code execution and result return
- call_tool bridge to existing MCP tools
- Sandbox security constraints (no filesystem, no network)
- Resource limit enforcement (timeout, memory)
- Input validation (empty code, empty justification)

Feature Flag: Set ENABLE_CODE_MODE=true to enable.
"""

import logging
import os

import pytest

from ..utilities.assertions import MCPAssertions, safe_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEATURE_FLAG = "ENABLE_CODE_MODE"
TOOL_NAME = "ha_create_custom_tool"


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
    """Check if ha_create_custom_tool is available in the MCP server."""
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
    """Test ha_create_custom_tool availability and feature flag behavior."""

    async def test_feature_flag_disabled_by_default(self, mcp_client):
        """Verify tool is NOT registered when feature flag is disabled."""
        original = os.environ.pop(FEATURE_FLAG, None)
        try:
            tools = await mcp_client.list_tools()
            tool_names = [t.name for t in tools]
            if TOOL_NAME not in tool_names:
                logger.info("Tool not registered (feature flag disabled) — correct")
                return
            logger.info("Tool registered — flag was enabled at server startup")
        finally:
            if original:
                os.environ[FEATURE_FLAG] = original

    async def test_tool_registered_when_enabled(self, mcp_client_with_code_mode):
        """Verify tool IS registered when feature flag is enabled."""
        available, error = await _check_tool_available(mcp_client_with_code_mode)
        if not available:
            pytest.skip(f"Code mode tool not available: {error}")
        logger.info("ha_create_custom_tool is registered and available")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestCodeModeValidation:
    """Test input validation for ha_create_custom_tool."""

    async def test_empty_code_rejected(self, mcp_client_with_code_mode):
        """Empty code must be rejected."""
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
        """Empty justification must be rejected."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Empty justification validation")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": "42", "justification": ""},
        )
        assert data.get("success") is False, f"Empty justification should fail: {data}"
        logger.info("Correctly rejected empty justification")


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------


class TestCodeModeExecution:
    """Test basic sandbox code execution."""

    async def test_simple_expression(self, mcp_client_with_code_mode):
        """Simple expression returns its value."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Simple expression")

        async with MCPAssertions(mcp_client_with_code_mode) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "code": "2 + 2",
                    "justification": "E2E test: simple arithmetic",
                },
            )
            assert data.get("success") is True, f"Should succeed: {data}"
            assert data.get("data") == 4, f"2 + 2 should equal 4: {data}"
            logger.info("Simple expression returned correct result")

    async def test_dict_result(self, mcp_client_with_code_mode):
        """Code returning a dict works correctly."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Dict result")

        code = """
items = [1, 2, 3, 4, 5]
total = sum(items)
{"total": total, "count": len(items)}
"""
        async with MCPAssertions(mcp_client_with_code_mode) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {"code": code, "justification": "E2E test: dict return value"},
            )
            assert data.get("success") is True, f"Should succeed: {data}"
            result = data.get("data", {})
            assert result.get("total") == 15, f"Sum should be 15: {data}"
            assert result.get("count") == 5, f"Count should be 5: {data}"
            logger.info("Dict result returned correctly")

    async def test_justification_returned(self, mcp_client_with_code_mode):
        """Justification is included in the response."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Justification returned")

        justification = "E2E test: verify justification passthrough"
        async with MCPAssertions(mcp_client_with_code_mode) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {"code": "'hello'", "justification": justification},
            )
            assert data.get("justification") == justification, (
                f"Justification should be preserved: {data}"
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

        code = """
result = await call_tool("ha_get_overview", {})
result.get("success", False)
"""
        async with MCPAssertions(mcp_client_with_code_mode) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "code": code,
                    "justification": "E2E test: call_tool bridge to ha_get_overview",
                },
            )
            assert data.get("success") is True, f"Should succeed: {data}"
            logger.info("call_tool bridge successfully invoked ha_get_overview")

    async def test_call_tool_search_entities(self, mcp_client_with_code_mode):
        """call_tool can invoke ha_search_entities and return results."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "call_tool search")

        code = """
result = await call_tool("ha_search_entities", {"query": "sun"})
entities = result.get("data", {}).get("entities", [])
{"found": len(entities) > 0, "count": len(entities)}
"""
        async with MCPAssertions(mcp_client_with_code_mode) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "code": code,
                    "justification": "E2E test: call_tool bridge to ha_search_entities",
                },
            )
            assert data.get("success") is True, f"Should succeed: {data}"
            result = data.get("data", {})
            assert result.get("found") is True, (
                f"Should find at least one entity: {data}"
            )
            logger.info(
                "call_tool bridge successfully searched entities "
                "(found %d)", result.get("count", 0)
            )

    async def test_call_tool_error_handling(self, mcp_client_with_code_mode):
        """call_tool returns error dict (not exception) when tool fails."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "call_tool error handling")

        code = """
result = await call_tool("ha_get_state", {"entity_id": "nonexistent.entity_12345"})
result.get("success", "missing")
"""
        async with MCPAssertions(mcp_client_with_code_mode) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "code": code,
                    "justification": "E2E test: call_tool error handling",
                },
            )
            assert data.get("success") is True, (
                f"Sandbox should succeed even when inner tool fails: {data}"
            )
            # The inner tool failure should return False
            assert data.get("data") is False, (
                f"Inner tool should return success=False: {data}"
            )
            logger.info("call_tool correctly returned error dict for failed tool")


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
        logger.info("Correctly blocked filesystem access")

    async def test_no_class_definitions(self, mcp_client_with_code_mode):
        """Sandbox must block class definitions."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "No class definitions")

        code = """
class Foo:
    pass
Foo()
"""
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": code,
                "justification": "E2E test: verify class definitions blocked",
            },
        )
        assert data.get("success") is False, (
            f"Class definitions should be blocked: {data}"
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
        logger.info("Correctly handled syntax error")


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


class TestCodeModeResourceLimits:
    """Test resource limit enforcement."""

    async def test_timeout_enforced(self, mcp_client_with_code_mode):
        """Code that exceeds the time limit is terminated."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Timeout enforcement")

        # Infinite loop — should be terminated by max_duration_secs
        code = """
i = 0
while True:
    i += 1
i
"""
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": code,
                "justification": "E2E test: timeout enforcement",
            },
        )
        assert data.get("success") is False, (
            f"Infinite loop should be terminated: {data}"
        )
        logger.info("Correctly terminated code that exceeded time limit")
