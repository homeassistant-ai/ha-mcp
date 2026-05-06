"""E2E tests for the JMESPath response-filter middleware."""

import logging

import pytest

from ..utilities.assertions import parse_mcp_result

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
async def test_jmespath_invalid_expression_degrades_gracefully(mcp_client):
    """An invalid JMESPath expression returns the full response plus a warning."""
    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": "sun.sun", "_jmespath": "!!not valid!!"},
    )

    data = parse_mcp_result(result)

    assert "_jmespath_warning" in data, (
        f"Expected _jmespath_warning in degraded response; got keys: {list(data)}"
    )
    logger.info(f"✅ Graceful degradation: warning = {data['_jmespath_warning']!r}")


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
