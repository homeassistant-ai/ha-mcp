"""Unit tests for ``_extract_tool_result`` in ``ha_mcp.tools.tools_code``.

The helper converts a FastMCP call_tool return value (which can be a
ToolResult, a list of content objects, or a basic Python type) into a
shape Monty can hand back to sandbox code (basic types only). The
list-passthrough branch is the M3 fix from the round-3 review: naked
list returns must NOT be string-repr'd, they must reach the sandbox
as iterable lists.
"""

import json
from dataclasses import dataclass
from typing import Any

from ha_mcp.tools.tools_code import _extract_tool_result


@dataclass
class _FakeContentBlock:
    """Stand-in for a FastMCP content block — has ``.text`` and ``.type``
    attributes the helper uses to recognize content-list shape."""

    text: str
    type: str = "text"


@dataclass
class _FakeToolResult:
    """Stand-in for a FastMCP ToolResult — has a ``.content`` list of
    blocks plus optional ``isError`` flag."""

    content: list[Any]
    isError: bool = False


class TestBasicTypePassthrough:
    """Basic types Monty natively handles must round-trip unchanged."""

    def test_string_passthrough(self):
        assert _extract_tool_result("hello") == "hello"

    def test_int_passthrough(self):
        assert _extract_tool_result(42) == 42

    def test_float_passthrough(self):
        assert _extract_tool_result(3.14) == 3.14

    def test_bool_passthrough(self):
        assert _extract_tool_result(True) is True
        assert _extract_tool_result(False) is False

    def test_none_passthrough(self):
        assert _extract_tool_result(None) is None

    def test_dict_passthrough(self):
        d = {"key": "value", "n": 1}
        assert _extract_tool_result(d) is d


class TestNakedListPassthrough:
    """The M3 round-3 fix: lists that don't carry FastMCP content blocks
    must reach the sandbox as iterable lists, not as ``str(list)``
    reprs. Sandbox code commonly does ``for item in result:`` which
    breaks on a string-repr."""

    def test_naked_list_of_dicts(self):
        """Tool returning ``[{"id": 1}, {"id": 2}]`` (the example
        Patch76 explicitly called out in the round-3 review)."""
        payload = [{"id": 1}, {"id": 2}]
        result = _extract_tool_result(payload)
        assert result == payload
        assert isinstance(result, list)
        assert result[0]["id"] == 1

    def test_naked_list_of_strings(self):
        payload = ["alpha", "beta", "gamma"]
        result = _extract_tool_result(payload)
        assert result == payload
        assert isinstance(result, list)

    def test_naked_list_of_ints(self):
        payload = [1, 2, 3, 4, 5]
        result = _extract_tool_result(payload)
        assert result == payload

    def test_naked_list_mixed_basic_types(self):
        payload = ["x", 1, True, None, {"k": "v"}]
        result = _extract_tool_result(payload)
        assert result == payload

    def test_empty_list_passthrough(self):
        """Empty list must pass through too — a tool that finds no
        results should hand back ``[]`` not the string ``"[]"``."""
        assert _extract_tool_result([]) == []

    def test_naked_list_dicts_without_content_keys(self):
        """Dicts that don't carry ``text`` or ``type`` keys are clearly
        data, not content blocks. Pass through."""
        payload = [{"entity_id": "light.foo", "state": "on"}]
        result = _extract_tool_result(payload)
        assert result == payload


class TestContentBlockDetection:
    """The helper recognizes content blocks by attribute (FastMCP's
    Pydantic-model shape) so they're not misclassified as a naked
    data list."""

    def test_attribute_blocks_extracted(self):
        """Pydantic-model-style content blocks: ``.text`` extracted,
        JSON-decoded if possible."""
        blocks = [
            _FakeContentBlock(text='{"answer": 42}', type="text"),
        ]
        result = _extract_tool_result(blocks)
        # JSON-decoded payload reaches the sandbox.
        assert result == {"answer": 42}


class TestToolResultExtraction:
    """End-to-end: a FastMCP ToolResult with content blocks gets
    JSON-decoded to a basic type."""

    def test_tool_result_json_payload(self):
        result = _extract_tool_result(
            _FakeToolResult(
                content=[_FakeContentBlock(text=json.dumps({"ok": True}))]
            )
        )
        assert result == {"ok": True}

    def test_tool_result_text_payload(self):
        """Non-JSON text falls through as a string."""
        result = _extract_tool_result(
            _FakeToolResult(content=[_FakeContentBlock(text="plain text")])
        )
        assert result == "plain text"

    def test_tool_result_with_is_error_returns_error_dict(self):
        """isError=True wraps the payload in ``{"error": ...}`` so
        sandbox code can branch on the failure."""
        result = _extract_tool_result(
            _FakeToolResult(
                content=[_FakeContentBlock(text=json.dumps({"detail": "boom"}))],
                isError=True,
            )
        )
        assert isinstance(result, dict)
        assert "error" in result
