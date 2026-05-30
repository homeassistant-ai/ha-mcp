"""Unit tests for ValidationErrorMiddleware."""

import json

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ha_mcp.tools.validation_middleware import ValidationErrorMiddleware


def _make_mcp() -> FastMCP:
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_dict_param(config: dict) -> dict:
        return {"ok": True}

    @mcp.tool()
    async def ha_test_list_param(items: list) -> dict:
        return {"ok": True}

    @mcp.tool()
    async def ha_test_int_param(count: int) -> dict:
        return {"count": count}

    @mcp.tool()
    async def ha_test_two_params(config: dict, items: list) -> dict:
        return {"ok": True}

    return mcp


@pytest.mark.asyncio
async def test_string_for_dict_gives_actionable_message():
    """Passing a JSON string where a dict is expected raises a structured ToolError."""
    mcp = _make_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_dict_param", {"config": '{"key": "value"}'})

    body = json.loads(str(exc_info.value))
    assert body["success"] is False
    msg = body["error"]["message"]
    assert "config" in msg
    assert "JSON object" in msg


@pytest.mark.asyncio
async def test_string_for_list_gives_actionable_message():
    """Passing a JSON string where a list is expected names the array hint."""
    mcp = _make_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_list_param", {"items": "[1, 2, 3]"})

    body = json.loads(str(exc_info.value))
    msg = body["error"]["message"]
    assert "items" in msg
    assert "JSON array" in msg


@pytest.mark.asyncio
async def test_multiple_type_errors_are_joined():
    """Multiple bad params surface together, joined by ';'."""
    mcp = _make_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "ha_test_two_params", {"config": '{"a": 1}', "items": "[1]"}
        )

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "config" in msg
    assert "items" in msg
    assert ";" in msg


@pytest.mark.asyncio
async def test_scalar_string_is_coerced_not_wrapped():
    """Tripwire for the design assumption: non-strict Pydantic coerces a
    numeric string to int, so the middleware never sees an int error. If a
    FastMCP upgrade flips to strict validation this test fails loudly,
    signalling that int_type/bool_type hints would start mattering."""
    mcp = _make_mcp()
    result = await mcp.call_tool("ha_test_int_param", {"count": "42"})
    assert result.structured_content == {"count": 42}


@pytest.mark.asyncio
async def test_dict_passes_through():
    """Passing a proper dict round-trips through the middleware unchanged."""
    mcp = _make_mcp()
    result = await mcp.call_tool("ha_test_dict_param", {"config": {"key": "value"}})
    assert result.structured_content == {"ok": True}


@pytest.mark.asyncio
async def test_error_is_structured():
    """Error follows ha-mcp's structured format with success/error fields."""
    mcp = _make_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_dict_param", {"config": "not a dict"})

    body = json.loads(str(exc_info.value))
    assert body["success"] is False
    assert "code" in body["error"]
    assert "message" in body["error"]


@pytest.mark.asyncio
async def test_tool_error_from_tool_body_passes_through_unconverted():
    """A ToolError raised inside the tool is NOT re-wrapped by the middleware."""
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_raises_tool_error(entity_id: str) -> dict:
        raise ToolError('{"success": false, "error": {"code": "ENTITY_NOT_FOUND"}}')

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_raises_tool_error", {"entity_id": "light.x"})

    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "ENTITY_NOT_FOUND"
