"""Unit tests for JMESPath response-filter middleware (_apply_jmespath)."""

import copy
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mt
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult

from ha_mcp.middleware.response_filter import (
    _PARAM_NAME,
    JMESPathFilterMiddleware,
    _apply_jmespath,
)


def _make_result(data: dict) -> ToolResult:
    return ToolResult(
        content=[mt.TextContent(type="text", text=json.dumps(data))],
        structured_content=data,
    )


class TestApplyJmesPath:
    def test_basic_field_extraction(self):
        result = _make_result({"success": True, "data": {"state": "on", "entity_id": "light.x"}})
        out = _apply_jmespath(result, "data.state")
        parsed = json.loads(out.content[0].text)
        assert parsed["result"] == "on"

    def test_projection_returns_dict_directly(self):
        result = _make_result({"success": True, "data": {"state": "on", "entity_id": "light.x"}})
        out = _apply_jmespath(result, "{s: data.state, id: data.entity_id}")
        parsed = json.loads(out.content[0].text)
        assert parsed["s"] == "on"
        assert parsed["id"] == "light.x"
        assert "result" not in parsed

    def test_scalar_result_wrapped(self):
        result = _make_result({"value": 42})
        out = _apply_jmespath(result, "value")
        parsed = json.loads(out.content[0].text)
        assert parsed == {"result": 42}

    def test_none_result_explicit(self):
        result = _make_result({"data": "hello"})
        out = _apply_jmespath(result, "no_such_key")
        parsed = json.loads(out.content[0].text)
        assert parsed == {"result": None}

    def test_envelope_keys_preserved_on_scalar(self):
        result = _make_result({"success": True, "partial": True, "warning": "degraded", "data": "x"})
        out = _apply_jmespath(result, "data")
        parsed = json.loads(out.content[0].text)
        assert parsed["result"] == "x"
        assert parsed["success"] is True
        assert parsed["partial"] is True
        assert parsed["warning"] == "degraded"

    def test_envelope_keys_preserved_on_projection(self):
        result = _make_result({"success": True, "data": {"state": "on"}})
        out = _apply_jmespath(result, "{s: data.state}")
        parsed = json.loads(out.content[0].text)
        assert parsed["s"] == "on"
        assert parsed["success"] is True

    def test_envelope_key_in_filtered_result_not_overwritten(self):
        result = _make_result({"success": True, "data": {"success": False}})
        out = _apply_jmespath(result, "data")
        parsed = json.loads(out.content[0].text)
        # filtered result is {"success": False}, envelope "success"=True must NOT overwrite it
        assert parsed["success"] is False

    def test_invalid_expression_raises_tool_error(self):
        result = _make_result({"data": "x"})
        with pytest.raises(ToolError) as exc_info:
            _apply_jmespath(result, "!!not valid!!")
        error = json.loads(str(exc_info.value))
        assert error["success"] is False
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    def test_null_structured_content_falls_back_to_text(self):
        text_result = ToolResult(
            content=[mt.TextContent(type="text", text=json.dumps({"state": "off"}))],
            structured_content=None,
        )
        out = _apply_jmespath(text_result, "state")
        parsed = json.loads(out.content[0].text)
        assert parsed["result"] == "off"

    def test_no_parseable_content_passthrough(self):
        raw_result = ToolResult(
            content=[mt.TextContent(type="text", text="not json at all")],
            structured_content=None,
        )
        out = _apply_jmespath(raw_result, "state")
        assert out is raw_result

    def test_structured_content_none_match_explicit(self):
        result = _make_result({"items": []})
        out = _apply_jmespath(result, "items[0]")
        parsed = json.loads(out.content[0].text)
        assert parsed == {"result": None}


class _ToolStub:
    """Minimal stand-in for a FastMCP Tool that holds a threading.RLock."""

    def __init__(self):
        self.parameters = {"type": "object", "properties": {}}
        self._lock = threading.RLock()


class TestOnListToolsDeepCopy:
    """Regression: deepcopy of FastMCP Tool raises 'cannot pickle _thread.RLock'."""

    def test_deepcopy_of_rlock_object_raises(self):
        with pytest.raises(TypeError, match="cannot pickle"):
            copy.deepcopy(_ToolStub())

    @pytest.mark.asyncio
    async def test_on_list_tools_succeeds_when_tool_has_rlock(self):
        context = MagicMock()
        call_next = AsyncMock(return_value=[_ToolStub()])
        middleware = JMESPathFilterMiddleware()

        result = await middleware.on_list_tools(context, call_next)

        assert _PARAM_NAME in result[0].parameters["properties"]

    @pytest.mark.asyncio
    async def test_on_list_tools_does_not_mutate_original_parameters(self):
        stub = _ToolStub()
        context = MagicMock()
        call_next = AsyncMock(return_value=[stub])
        middleware = JMESPathFilterMiddleware()

        await middleware.on_list_tools(context, call_next)

        assert _PARAM_NAME not in stub.parameters["properties"]
