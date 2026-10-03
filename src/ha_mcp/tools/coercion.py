"""Coercion and parsing of MCP tool parameters.

Turns JSON-encoded strings, CSV strings and scalar values into the list and
container shapes the tool functions expect. Also holds the ANSI escape pattern
used to clean container and log text.
"""

import json
import re
from typing import Any

from pydantic import BeforeValidator

# Strips ANSI terminal escape codes from container/log output.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_json_param(
    param: str | dict | list | None, param_name: str = "parameter"
) -> dict | list | None:
    """
    Parse flexibly JSON string or return existing dict/list.

    Args:
        param: JSON string, dict, list, or None
        param_name: Parameter name for error context

    Returns:
        Parsed dict/list or original value if already correct type

    Raises:
        ValueError: If JSON parsing fails
    """
    if param is None:
        return None

    if isinstance(param, (dict, list)):
        return param

    if isinstance(param, str):
        try:
            parsed = json.loads(param)
            if not isinstance(parsed, (dict, list)):
                raise ValueError(
                    f"{param_name} must be a JSON object or array, got {type(parsed).__name__}"
                )
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {param_name}: {e}") from e

    raise ValueError(
        f"{param_name} must be string, dict, list, or None, got {type(param).__name__}"
    )


def loads_if_json_container_str(value: Any) -> Any:
    """Parse a JSON-encoded object/array string into its container value.

    A string that starts like an object or array but contains malformed JSON
    raises with the decoder location so ValidationErrorMiddleware can return
    an actionable error. Jinja templates and other strings pass through
    unchanged, leaving Pydantic to select the expected parameter type.

    Public (not module-private): also reused by ``policy/evaluator.py`` to
    normalize stringified args before policy evaluation, outside the
    Pydantic-validator role ``JSON_STRING_COERCION`` below wraps it in.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            is_container_like = re.match(r"\s*[\[{]", value) is not None
            is_standalone_jinja = re.match(r"\s*{[{%#]", value) is not None
            if is_container_like and not is_standalone_jinja:
                raise ValueError(
                    f"Invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
                ) from exc
            return value
        except RecursionError:
            # RecursionError (deeply-nested input) must not escape: Pydantic
            # only converts ValueError/AssertionError into ValidationError.
            return value
        if isinstance(parsed, (dict, list)):
            return parsed
    return value


# Annotated metadata for MCP-exposed dict/list params (issue #1581): coerces a
# JSON-encoded string to its parsed container before validation, without
# re-advertising string in the tool schema (the #1485/#1487/#1492 fix). Some
# MCP client stacks (Claude Desktop stdio among them) pass model-emitted
# stringified objects through unrepaired, so the strict schema boundary alone
# rejects previously-valid traffic.
JSON_STRING_COERCION = BeforeValidator(loads_if_json_container_str)


def _parse_json_to_str_list(s: str, param_name: str) -> list[str]:
    """Parse a JSON string as a list of strings, raising ValueError on failure."""
    try:
        parsed = json.loads(s)
        if not isinstance(parsed, list):
            raise ValueError(f"{param_name} must be a JSON array")
        if not all(isinstance(item, str) for item in parsed):
            raise ValueError(f"{param_name} must be a JSON array of strings")
        return parsed
    except (json.JSONDecodeError, RecursionError) as e:
        # RecursionError: nested past the interpreter's limit is still
        # "not valid JSON" to the caller, which expects ValueError here.
        raise ValueError(f"Invalid JSON in {param_name}: {e}") from e


def parse_string_list_param(
    param: str | list[str] | None,
    param_name: str = "parameter",
    allow_csv: bool = False,
) -> list[str] | None:
    """Parse JSON string array or return existing list of strings.

    Args:
        param: Value to parse.
        param_name: Name for error messages.
        allow_csv: When True, plain strings are split on commas
            (e.g. ``"light,sensor"`` → ``["light", "sensor"]``).
            When False (default), non-JSON strings raise ValueError.
    """
    if param is None:
        return None

    if isinstance(param, list):
        if all(isinstance(item, str) for item in param):
            return param
        raise ValueError(f"{param_name} must be a list of strings")

    if isinstance(param, str):
        if param.strip().startswith("["):
            return _parse_json_to_str_list(param, param_name)
        if allow_csv:
            return [item.strip() for item in param.split(",") if item.strip()]
        return _parse_json_to_str_list(param, param_name)

    raise ValueError(f"{param_name} must be string, list, or None")


def coerce_to_list(value: Any) -> list[Any]:
    """Return value as a list: list → as-is, dict/other → [value], None/falsy → []."""
    if isinstance(value, list):
        return value
    return [value] if value else []
