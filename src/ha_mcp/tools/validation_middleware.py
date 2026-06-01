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
            parts: list[str] = []
            for err in errors:
                loc = err.get("loc", ())
                param = ".".join(str(p) for p in loc if p != "__root__")
                hint = _TYPE_HINTS.get(err["type"], err["msg"])
                parts.append(f"`{param}`: {hint}" if param else hint)
            raise_tool_error(
                create_validation_error(
                    "; ".join(parts) if parts else "Invalid argument types.",
                    details=", ".join(err["type"] for err in errors),
                )
            )
