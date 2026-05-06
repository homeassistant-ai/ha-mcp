from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import jmespath
import jmespath.exceptions
import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

_PARAM_NAME = "_jmespath"
_PARAM_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Optional JMESPath expression applied server-side to filter this tool's "
        "response before it is returned, reducing token usage. "
        "Examples: 'state' — single field; "
        "'{id: entity_id, state: state}' — projection; "
        "'entities[?domain==`light`].entity_id' — filter array. "
        "On expression error the full response is returned with a '_jmespath_warning' key."
    ),
}


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
        for tool in tools:
            tool.parameters.setdefault("properties", {})[_PARAM_NAME] = _PARAM_SCHEMA
        return tools

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        args = context.message.arguments or {}
        jmespath_expr: str | None = args.get(_PARAM_NAME)

        if jmespath_expr is not None:
            clean_args = {k: v for k, v in args.items() if k != _PARAM_NAME}
            new_message = context.message.model_copy(update={"arguments": clean_args})
            context = context.copy(message=new_message)

        result = await call_next(context)

        if not jmespath_expr:
            return result

        return _apply_jmespath(result, jmespath_expr)


def _apply_jmespath(result: ToolResult, expr: str) -> ToolResult:
    """Apply a JMESPath expression to the tool result, degrading gracefully on error."""
    data: Any = result.structured_content

    if data is None:
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                try:
                    data = json.loads(text)
                    break
                except (json.JSONDecodeError, ValueError):
                    pass

    if data is None:
        return result

    try:
        filtered = jmespath.compile(expr).search(data)
    except jmespath.exceptions.JMESPathError as exc:
        warning: dict[str, Any] = dict(data) if isinstance(data, dict) else {"result": data}
        warning["_jmespath_warning"] = str(exc)
        return ToolResult(content=warning, structured_content=warning)

    if filtered is None:
        filtered_dict: dict[str, Any] = {}
    elif isinstance(filtered, dict):
        filtered_dict = filtered
    else:
        filtered_dict = {"result": filtered}

    return ToolResult(content=filtered_dict, structured_content=filtered_dict)
