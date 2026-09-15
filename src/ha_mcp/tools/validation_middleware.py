"""FastMCP middleware that converts argument-validation errors to structured ToolErrors.

When a model passes the wrong type for a tool parameter (e.g. a JSON string where
a dict is required), FastMCP surfaces a validation error with a raw message like
"Input should be a valid dictionary" -- a bare ``pydantic.ValidationError`` on
older FastMCP, or a ``fastmcp.exceptions.ValidationError`` wrapping it (chained
via ``from e``) on FastMCP >= 3.4.3. This middleware intercepts either shape and
converts it to ha-mcp's structured format with actionable guidance. When the
tool's schema can be read, an argument name the tool does not declare is
reported with the closest declared parameter, if one resembles it, and the full
parameter list.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ha_mcp._vendor.fastmcp.exceptions import ValidationError as FastMCPValidationError
from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)

from ..errors import create_validation_error
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

# Maps Pydantic error types to model-readable fix hints.
# FastMCP uses non-strict Pydantic: scalar mismatches (bool, int) are coerced
# rather than rejected, so only dict_type and list_type fire among type errors.
_TYPE_HINTS: dict[str, str] = {
    "dict_type": (
        "expected a JSON object. "
        'Pass {"key": "value"} directly, not a JSON-encoded string.'
    ),
    "list_type": (
        "expected a JSON array. Pass [...] directly, not a JSON-encoded string."
    ),
}

_UNKNOWN_ARGUMENT = "unexpected_keyword_argument"


async def _tool_parameter_names(context: MiddlewareContext | None) -> list[str] | None:
    """Return the called tool's declared parameter names, or None if unavailable."""
    fastmcp_context = getattr(context, "fastmcp_context", None)
    tool_name = getattr(getattr(context, "message", None), "name", None)
    if fastmcp_context is None or not tool_name:
        return None
    try:
        tool = await fastmcp_context.fastmcp.get_tool(tool_name)
    except Exception:
        logger.warning(
            "Parameter lookup for %s failed; unknown-argument hint omitted",
            tool_name,
            exc_info=True,
        )
        return None
    properties = tool.parameters.get("properties") if tool is not None else None
    if not isinstance(properties, dict):
        logger.warning(
            "No parameter schema for %s; unknown-argument hint omitted", tool_name
        )
        return None
    return list(properties)


def _closest_parameter(unknown: str, candidates: Sequence[str]) -> str | None:
    """Return the declared parameter an invented argument name most likely meant.

    A candidate qualifies by sharing an underscore-separated word with the name
    (``dashboard_url`` for ``url_path``) or by a similarity ratio of at least
    0.6 (typos). Shared words rank first; the ratio breaks ties.
    """
    unknown_lower = unknown.lower()
    words = set(unknown_lower.split("_")) - {""}
    best: tuple[int, float, str] | None = None
    for candidate in candidates:
        candidate_lower = candidate.lower()
        shared = len(words & set(candidate_lower.split("_")))
        ratio = SequenceMatcher(None, unknown_lower, candidate_lower).ratio()
        if not shared and ratio < 0.6:
            continue
        if best is None or (shared, ratio) > best[:2]:
            best = (shared, ratio, candidate)
    return best[2] if best else None


def _unknown_argument_hint(param: str, unclaimed: Sequence[str]) -> str:
    """Name an undeclared argument, suggesting the parameter it likely meant."""
    match = _closest_parameter(param, unclaimed)
    return (
        f"unknown parameter, did you mean `{match}`?" if match else "unknown parameter"
    )


def _type_hint(errs: list[Any]) -> str:
    """Prefer an actionable container hint (dict_type/list_type); else the raw message."""
    return next(
        (_TYPE_HINTS[e["type"]] for e in errs if e["type"] in _TYPE_HINTS),
        errs[0]["msg"],
    )


class ValidationErrorMiddleware(Middleware):
    """Convert argument-validation failures into structured ToolErrors."""

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        try:
            result = await call_next(context)
        except (PydanticValidationError, FastMCPValidationError) as exc:
            # fastmcp >= 3.4.3 re-raises an argument-validation failure as
            # ``fastmcp.exceptions.ValidationError`` wrapping the pydantic error
            # (chained via ``from e``); older fastmcp raises the pydantic error
            # directly. Recover the pydantic errors from whichever shape arrived,
            # and let any other fastmcp ValidationError (e.g. a return-value
            # failure with no pydantic cause) propagate unchanged.
            pydantic_exc = (
                exc if isinstance(exc, PydanticValidationError) else exc.__cause__
            )
            if not isinstance(pydantic_exc, PydanticValidationError):
                raise
            errors = pydantic_exc.errors(include_url=False)
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

            valid_parameters = (
                await _tool_parameter_names(context)
                if any(err["type"] == _UNKNOWN_ARGUMENT for err in errors)
                else None
            )
            # A parameter the call already supplied is never what a second,
            # invented argument meant.
            supplied = (
                getattr(getattr(context, "message", None), "arguments", None) or {}
            )
            unclaimed = [p for p in valid_parameters or () if p not in supplied]

            parts: list[str] = []
            for param, errs in grouped.items():
                if (
                    valid_parameters is not None
                    and errs[0]["type"] == _UNKNOWN_ARGUMENT
                ):
                    hint = _unknown_argument_hint(param, unclaimed)
                else:
                    hint = _type_hint(errs)
                parts.append(f"`{param}`: {hint}" if param else hint)
            message = "; ".join(parts) if parts else "Invalid argument types."
            if valid_parameters is not None:
                separator = " " if message.endswith((".", "?")) else ". "
                listing = ", ".join(valid_parameters) or "none"
                message += f"{separator}Valid parameters: {listing}."
            raise_tool_error(
                create_validation_error(
                    message,
                    details=", ".join(dict.fromkeys(err["type"] for err in errors)),
                    context=(
                        {"valid_parameters": valid_parameters}
                        if valid_parameters is not None
                        else None
                    ),
                )
            )
        return result
