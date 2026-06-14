"""FastMCP middleware that converts Pydantic validation errors to structured ToolErrors.

When a model passes the wrong type for a tool parameter (e.g. a JSON string where
a dict is required), FastMCP raises a PydanticValidationError with a raw message
like "Input should be a valid dictionary". This middleware intercepts those errors
and converts them to ha-mcp's structured format with actionable guidance.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import ValidationError as PydanticValidationError

from ..errors import create_validation_error
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

# Maps Pydantic error types to model-readable fix hints.
# FastMCP uses non-strict Pydantic: scalar mismatches (bool, int) are coerced
# rather than rejected, so only dict_type and list_type fire in practice.
_TYPE_HINTS: dict[str, str] = {
    "dict_type": (
        "expected a JSON object. "
        'Pass {"key": "value"} directly, not a JSON-encoded string.'
    ),
    "list_type": (
        "expected a JSON array. Pass [...] directly, not a JSON-encoded string."
    ),
}


class ValidationErrorMiddleware(Middleware):
    """Convert PydanticValidationError from argument validation into ToolErrors."""

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        try:
            return await call_next(context)
        except PydanticValidationError as exc:
            errors = exc.errors(include_url=False)
            # Group by the real argument path. A union param like
            # `str | list[str]` emits one error per arm with loc (param, "str"),
            # (param, "list[str]"); without grouping the user saw `param.str` /
            # `param.list[str]` instead of `param` (#1601). We keep the param
            # name plus any numeric list indices (so a bad element still reports
            # `monday.1`) but drop the non-numeric union-arm tags.
            grouped: dict[str, list[Any]] = {}
            for err in errors:
                loc = [str(p) for p in err.get("loc", ()) if p != "__root__"]
                if loc:
                    key = ".".join([loc[0], *(p for p in loc[1:] if p.isdigit())])
                else:
                    key = ""
                grouped.setdefault(key, []).append(err)

            parts: list[str] = []
            for param, errs in grouped.items():
                # Prefer an actionable container hint when any arm produced one
                # (dict_type/list_type); else fall back to the first raw message.
                hint = next(
                    (_TYPE_HINTS[e["type"]] for e in errs if e["type"] in _TYPE_HINTS),
                    errs[0]["msg"],
                )
                parts.append(f"`{param}`: {hint}" if param else hint)
            raise_tool_error(
                create_validation_error(
                    "; ".join(parts) if parts else "Invalid argument types.",
                    details=", ".join(dict.fromkeys(err["type"] for err in errors)),
                )
            )
