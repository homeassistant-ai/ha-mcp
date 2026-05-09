"""E2E tests for the JMESPath response-filter middleware."""

import logging

import pytest

from ..utilities.assertions import parse_mcp_result, safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_jmespath_param_present_on_all_tools(mcp_client):
    """Every tool advertised by the server must include the _jmespath parameter."""
    tools = await mcp_client.list_tools()
    assert tools, "Server should expose at least one tool"

    missing = [
        t.name
        for t in tools
        if "_jmespath" not in (t.inputSchema or {}).get("properties", {})
    ]
    assert not missing, (
        f"Tools missing _jmespath in schema ({len(missing)}): {missing[:10]}"
    )
    logger.info(f"✅ _jmespath present on all {len(tools)} tools")


@pytest.mark.asyncio
async def test_on_list_tools_idempotent(mcp_client):
    """Calling list_tools twice must not accumulate or change the _jmespath schema."""
    tools_first = await mcp_client.list_tools()
    tools_second = await mcp_client.list_tools()

    schema_first = {
        t.name: (t.inputSchema or {}).get("properties", {}).get("_jmespath")
        for t in tools_first
    }
    schema_second = {
        t.name: (t.inputSchema or {}).get("properties", {}).get("_jmespath")
        for t in tools_second
    }
    assert schema_first == schema_second, (
        "_jmespath schema must be identical across list_tools calls (no mutation of registry objects)"
    )
    logger.info(f"✅ _jmespath schema stable across {len(tools_first)} tools")


@pytest.mark.asyncio
async def test_jmespath_projection_reduces_response(mcp_client):
    """A projection expression returns only the requested fields."""
    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": "sun.sun", "_jmespath": "{entity_id: data.entity_id, state: data.state}"},
    )

    data = parse_mcp_result(result)

    assert "entity_id" in data, f"Projection must include entity_id; got {data}"
    assert "state" in data, f"Projection must include state; got {data}"
    assert "attributes" not in data, f"Projection must exclude attributes; got {data}"
    logger.info(f"✅ Projection returned: {data}")


@pytest.mark.asyncio
async def test_jmespath_projection_preserves_envelope(mcp_client):
    """Envelope fields (success, partial, warning) survive a sub-field projection."""
    # ha_config_list_areas returns {"success": True, "count": N, "areas": [...]} directly
    # (no add_timezone_metadata wrapper), so success is at the top level where the
    # middleware's envelope merge loop looks for it.
    result = await mcp_client.call_tool(
        "ha_config_list_areas",
        {"_jmespath": "areas[*].name"},
    )
    data = parse_mcp_result(result)

    # areas[*].name returns a list — wrapped in {"result": [...]} by the middleware.
    # The envelope key "success" must be re-attached alongside the filtered result.
    assert "success" in data, f"Envelope 'success' must be preserved; got keys: {list(data)}"
    assert "result" in data, f"Filtered list must be under 'result'; got keys: {list(data)}"
    logger.info(f"✅ Envelope preserved after projection: {data}")


@pytest.mark.asyncio
async def test_jmespath_invalid_expression_raises_error(mcp_client):
    """An invalid JMESPath expression raises a structured ToolError."""
    data = await safe_call_tool(
        mcp_client,
        "ha_get_state",
        {"entity_id": "sun.sun", "_jmespath": "!!not valid!!"},
    )

    assert data.get("success") is False, (
        f"Expected success=False for invalid expression; got: {data}"
    )
    error = data.get("error", {})
    assert isinstance(error, dict), f"Expected structured error dict; got: {error!r}"
    assert error.get("code") == "VALIDATION_INVALID_PARAMETER", (
        f"Expected VALIDATION_INVALID_PARAMETER error code; got: {error.get('code')!r}"
    )
    logger.info(f"✅ Invalid expression raised ToolError: {error.get('message')!r}")


@pytest.mark.asyncio
async def test_jmespath_none_result(mcp_client):
    """An expression that matches nothing returns explicit null, not an empty dict."""
    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": "sun.sun", "_jmespath": "nonexistent_field_xyz"},
    )
    data = parse_mcp_result(result)

    assert "result" in data, f"None-match must use explicit {{'result': None}}; got: {data}"
    assert data["result"] is None, f"result must be null; got: {data['result']!r}"
    logger.info("✅ None-match returned {'result': None}")


@pytest.mark.asyncio
async def test_jmespath_param_not_passed_to_tool(mcp_client):
    """The _jmespath parameter must be stripped before the tool receives arguments."""
    # ha_get_state has a strict required param 'entity_id'. If _jmespath were passed
    # through, FastMCP schema validation would reject it on a tool with no extra=allow.
    # A successful call proves the middleware stripped it cleanly.
    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": "sun.sun", "_jmespath": "data.state"},
    )
    data = parse_mcp_result(result)
    # With 'data.state' expression the result should be a scalar wrapped in {"result": ...}
    assert "result" in data, f"Expected scalar result wrapped in dict; got: {data}"
    logger.info(f"✅ Tool received clean args; filtered result: {data}")
