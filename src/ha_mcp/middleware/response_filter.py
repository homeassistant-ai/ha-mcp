from __future__ import annotations

import copy
import json
import logging
from collections.abc import Sequence
from typing import Any

import jmespath
import jmespath.exceptions
import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult

from ha_mcp.errors import ErrorCode, create_error_response

logger = logging.getLogger(__name__)

_PARAM_NAME = "_jmespath"
_PARAM_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Optional JMESPath filter for results - see jmespath.org"
        "Examples: '{areas:areas[*].{id: area_id, name: name}'."
    ),
}

_ENVELOPE_KEYS = ("success", "partial", "warning", "error", "count", "message")


class JMESPathFilterMiddleware(Middleware):
    """Adds an optional _jmespath parameter to every tool.

    Agents supply a JMESPath expression; the server filters the response before
    returning it, reducing the tokens consumed by large HA payloads.
    """

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        result = []
        for tool in tools:
            # Shallow-copy the Tool to avoid mutating the registry.
            # Full deepcopy raises "cannot pickle '_thread.RLock' object" because
            # FastMCP Tool objects hold threading locks internally; only the
            # parameters dict (plain JSON Schema) needs to be copied.
            tool_copy = copy.copy(tool)
            tool_copy.parameters = copy.deepcopy(tool.parameters)
            tool_copy.parameters.setdefault("properties", {})[_PARAM_NAME] = _PARAM_SCHEMA
            result.append(tool_copy)
        return result

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        args = context.message.arguments or {}
        jmespath_expr: str | None = args.get(_PARAM_NAME)

        if jmespath_expr is not None:
            context.message.arguments = {k: v for k, v in args.items() if k != _PARAM_NAME}

        result = await call_next(context)

        if not jmespath_expr:
            return result

        return _apply_jmespath(result, jmespath_expr)


def _apply_jmespath(result: ToolResult, expr: str) -> ToolResult:
    """Apply a JMESPath expression to the tool result."""
    data: Any = result.structured_content

    if data is None:
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                try:
                    data = json.loads(text)
                    break
                except (json.JSONDecodeError, ValueError):
                    logger.warning("_jmespath: text block is not JSON, skipping filter")

    if data is None:
        return result

    try:
        filtered = jmespath.compile(expr).search(data)
    except jmespath.exceptions.JMESPathError as exc:
        raise ToolError(json.dumps(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Invalid JMESPath expression: {exc}",
            context={"expression": expr},
            suggestions=["Check JMESPath syntax — see https://jmespath.org/examples.html"],
        ))) from exc

    if filtered is None:
        filtered_dict: dict[str, Any] = {"result": None}
    elif isinstance(filtered, dict):
        filtered_dict = filtered
    else:
        filtered_dict = {"result": filtered}

    if isinstance(data, dict):
        for key in _ENVELOPE_KEYS:
            if key in data and key not in filtered_dict:
                filtered_dict[key] = data[key]

    return ToolResult(
        content=[mt.TextContent(type="text", text=json.dumps(filtered_dict))],
        structured_content=filtered_dict,
    )
