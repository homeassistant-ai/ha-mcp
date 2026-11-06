"""FastMCP middleware that converts argument-validation errors to structured ToolErrors.

When a model passes the wrong type for a tool parameter (e.g. a JSON string where
a dict is required), FastMCP surfaces a validation error with a raw message like
"Input should be a valid dictionary" -- a bare ``pydantic.ValidationError`` on
older FastMCP, or a ``fastmcp.exceptions.ValidationError`` wrapping it (chained
via ``from e``) on FastMCP >= 3.4.3. This middleware intercepts either shape and
converts it to ha-mcp's structured format with actionable guidance. When the
tool's schema can be read, an argument name the tool does not declare is
reported with the closest declared parameter, if one resembles it (a config
key passed top-level on a config-writing tool is told to move inside
``config`` instead), the full parameter list, and, for any other name, a
suggestion for the case where the client's tool list is older than the
server's.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import ValidationError as PydanticValidationError
from pydantic_core import ErrorDetails

from ha_mcp._vendor.fastmcp.exceptions import ValidationError as FastMCPValidationError
from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)

from ..errors import create_validation_error
from ..renamed_tools import CLIENT_TOOL_LIST_REFRESH_STEPS
from ..utils.usage_logger import log_tool_call
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

_RejectionKind = Literal["misplaced", "undeclared", "other"]

# Config root keys that models habitually pass as top-level tool arguments on
# the config-writing tools. When one arrives top-level, the rejection says the
# key belongs inside `config` instead of only listing the valid tool
# parameters (issue #2649 section 1).
#
# Scoped per tool on purpose: only the config-writing tools take a `config`
# body, so only their top-level arguments can be misplaced config keys.
# (Only these two were measured in BAT runs.)
_CONFIG_ROOT_KEYS_BY_TOOL: dict[str, set[str]] = {
    "ha_config_set_automation": {
        "alias",
        "description",
        "triggers",
        "trigger",
        "actions",
        "action",
        "conditions",
        "condition",
        "mode",
        "max",
        "max_exceeded",
        "variables",
        "use_blueprint",
    },
    "ha_config_set_script": {
        "alias",
        "description",
        "mode",
        "sequence",
        "max",
        "max_exceeded",
        "fields",
        "icon",
        "use_blueprint",
    },
}


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
    if tool is None:
        logger.warning(
            "Parameter lookup for %s returned no tool; unknown-argument hint omitted",
            tool_name,
        )
        return None
    properties = tool.parameters.get("properties")
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
    0.6 (typos). A declared name the invented one ends with ranks first
    (``dashboard_url_path`` for ``url_path``), then more shared words; the ratio
    breaks ties.
    """
    unknown_lower = unknown.lower()
    words = set(unknown_lower.split("_")) - {""}
    best: tuple[bool, int, float, str] | None = None
    for candidate in candidates:
        candidate_lower = candidate.lower()
        shared = len(words & set(candidate_lower.split("_")))
        ratio = SequenceMatcher(None, unknown_lower, candidate_lower).ratio()
        if not shared and ratio < 0.6:
            continue
        suffix = unknown_lower.endswith(f"_{candidate_lower}")
        if best is None or (suffix, shared, ratio) > best[:3]:
            best = (suffix, shared, ratio, candidate)
    return best[3] if best else None


def _unknown_argument_hint(param: str, unclaimed: Sequence[str]) -> str:
    """Name an undeclared argument, suggesting the parameter it likely meant."""
    match = _closest_parameter(param, unclaimed)
    return (
        f"unknown parameter, did you mean `{match}`?" if match else "unknown parameter"
    )


def _stale_tool_list_hint(names: Sequence[str]) -> str:
    """Suggestion for undeclared arguments the client's own tool list may show.

    Conditional on purpose: an agent can also invent a name, as agents sent
    ``entity_id`` before any tool declared it (#2877), and then there is no
    stale list to refresh.
    """
    listed = ", ".join(f"`{name}`" for name in names)
    them = "them" if len(names) > 1 else "it"
    return (
        f"If your tool list shows {listed} on this tool, that list is a stale "
        f"copy from before an ha-mcp update renamed or removed {them}, "
        "not a server bug. Ask the user to reconnect this MCP server in their "
        f"client ({CLIENT_TOOL_LIST_REFRESH_STEPS}), then start a new "
        "conversation."
    )


def _argument_hint(
    param: str,
    errs: Sequence[ErrorDetails],
    unclaimed: Sequence[str],
    valid_parameters: list[str] | None,
    config_keys: set[str] | None,
) -> tuple[str, _RejectionKind]:
    """Hint for one rejected argument.

    Returns the hint text and the rejection kind: ``misplaced`` for a config
    root key passed at the top level of a config-writing tool, which is told
    to move inside ``config`` (issue #2649), ``undeclared`` for any other name
    the tool does not declare, and ``other`` for a type error or for any
    rejection when the tool's schema could not be read.
    """
    if valid_parameters is None or errs[0]["type"] != _UNKNOWN_ARGUMENT:
        return _type_hint(errs), "other"
    if config_keys is not None and param in config_keys:
        hint = (
            f"unknown parameter; `{param}` is a config key, not a tool parameter "
            "— move it inside `config`"
        )
        return hint, "misplaced"
    return _unknown_argument_hint(param, unclaimed), "undeclared"


def _rejection_suggestions(
    misplaced_keys: Sequence[str], undeclared_names: Sequence[str]
) -> list[str]:
    """Suggestions for undeclared arguments; empty keeps the generic ones.

    A misplaced config key repeats the move into ``config`` (issue #2649
    section 1); any other undeclared name may come from a tool list the client
    stored before an update renamed or removed it (#2968, #2978).
    """
    suggestions = [
        f"Move `{key}` inside the `config` argument, e.g. config={{'{key}': ...}}."
        for key in misplaced_keys
    ]
    if undeclared_names:
        suggestions.append(_stale_tool_list_hint(undeclared_names))
    return suggestions


def _record_rejection(
    tool_name: str | None, arguments: dict[str, Any], message: str
) -> None:
    """Put a rejected call in the usage log that ha_report_issue reads.

    The call never reaches the tool, so the tool's own usage-log wrapper never
    runs; without this entry a report shows no trace of the call or of the
    argument names the client sent.
    """
    if tool_name:
        log_tool_call(
            tool_name=tool_name,
            parameters=dict(arguments),
            execution_time_ms=0.0,
            success=False,
            error_message=message,
        )


def _type_hint(errs: Sequence[ErrorDetails]) -> str:
    """Prefer a dict_type/list_type hint; else the first error's raw message."""
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
            grouped: dict[str, list[ErrorDetails]] = {}
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
            message_obj = getattr(context, "message", None)
            supplied = getattr(message_obj, "arguments", None) or {}
            tool_name = getattr(message_obj, "name", None)
            unclaimed: list[str] = []
            config_keys: set[str] | None = None
            if valid_parameters is not None:
                # Skip parameters the call already supplied: a second, invented
                # argument almost never means one of them.
                unclaimed = [p for p in valid_parameters if p not in supplied]
                config_keys = (
                    _CONFIG_ROOT_KEYS_BY_TOOL.get(tool_name) if tool_name else None
                )

            parts: list[str] = []
            misplaced_keys: list[str] = []
            undeclared_names: list[str] = []
            for param, errs in grouped.items():
                hint, kind = _argument_hint(
                    param, errs, unclaimed, valid_parameters, config_keys
                )
                if kind == "misplaced":
                    misplaced_keys.append(param)
                elif kind == "undeclared":
                    undeclared_names.append(param)
                parts.append(f"`{param}`: {hint}" if param else hint)
            message = "; ".join(parts) if parts else "Invalid argument types."
            error_context: dict[str, Any] | None = None
            if valid_parameters is not None:
                separator = " " if message.endswith((".", "?")) else ". "
                listing = ", ".join(valid_parameters) or "none"
                message += f"{separator}Valid parameters: {listing}."
                error_context = {"valid_parameters": valid_parameters}
            _record_rejection(tool_name, supplied, message)
            raise_tool_error(
                create_validation_error(
                    message,
                    details=", ".join(dict.fromkeys(err["type"] for err in errors)),
                    context=error_context,
                    suggestions=_rejection_suggestions(
                        misplaced_keys, undeclared_names
                    ),
                )
            )
        return result
