"""
Shared utility functions for MCP tool modules.

This module provides common helper functions used across multiple tool registration modules.
"""

import json
from typing import Any


def coerce_bool_param(
    value: bool | str | None,
    param_name: str = "parameter",
    default: bool | None = None,
) -> bool | None:
    """
    Coerce a value to a boolean, handling string inputs from AI tools.

    AI assistants using XML-style function calls pass boolean parameters as strings
    (e.g., "true" instead of true). This function safely converts such inputs.

    Args:
        value: The value to coerce (bool, str, or None)
        param_name: Parameter name for error messages
        default: Default value to return if value is None

    Returns:
        The coerced boolean value, or default if value is None

    Raises:
        ValueError: If the value cannot be converted to a boolean
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        value = value.strip().lower()
        if not value:
            return default
        if value in ("true", "1", "yes", "on"):
            return True
        if value in ("false", "0", "no", "off"):
            return False
        raise ValueError(
            f"{param_name} must be a boolean value, got '{value}'"
        )

    raise ValueError(
        f"{param_name} must be bool or string, got {type(value).__name__}"
    )


def coerce_int_param(
    value: int | str | None,
    param_name: str = "parameter",
    default: int | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int | None:
    """
    Coerce a value to an integer, handling string inputs from AI tools.

    AI assistants often pass numeric parameters as strings (e.g., "100" instead of 100).
    This function safely converts such inputs to integers.

    Args:
        value: The value to coerce (int, str, or None)
        param_name: Parameter name for error messages
        default: Default value to return if value is None
        min_value: Optional minimum value constraint
        max_value: Optional maximum value constraint

    Returns:
        The coerced integer value, or default if value is None

    Raises:
        ValueError: If the value cannot be converted to an integer
    """
    if value is None:
        return default

    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        value = value.strip()
        if not value:
            return default
        try:
            # Handle float strings like "100.0" by converting via float first
            result = int(float(value))
        except ValueError:
            raise ValueError(
                f"{param_name} must be a valid integer, got '{value}'"
            ) from None
    else:
        raise ValueError(
            f"{param_name} must be int or string, got {type(value).__name__}"
        )

    # Apply constraints
    if min_value is not None and result < min_value:
        result = min_value
    if max_value is not None and result > max_value:
        result = max_value

    return result


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
            raise ValueError(f"Invalid JSON in {param_name}: {e}")

    raise ValueError(
        f"{param_name} must be string, dict, list, or None, got {type(param).__name__}"
    )


def parse_string_list_param(
    param: str | list[str] | None, param_name: str = "parameter"
) -> list[str] | None:
    """Parse JSON string array or return existing list of strings."""
    if param is None:
        return None

    if isinstance(param, list):
        if all(isinstance(item, str) for item in param):
            return param
        raise ValueError(f"{param_name} must be a list of strings")

    if isinstance(param, str):
        try:
            parsed = json.loads(param)
            if not isinstance(parsed, list):
                raise ValueError(f"{param_name} must be a JSON array")
            if not all(isinstance(item, str) for item in parsed):
                raise ValueError(f"{param_name} must be a JSON array of strings")
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {param_name}: {e}")

    raise ValueError(f"{param_name} must be string, list, or None")


async def add_timezone_metadata(client: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Add timezone metadata to tool responses containing timestamps."""
    try:
        config = await client.get_config()
        ha_timezone = config.get("time_zone", "UTC")

        return {
            "data": data,
            "metadata": {
                "home_assistant_timezone": ha_timezone,
                "timestamp_format": "ISO 8601 (UTC)",
                "note": f"All timestamps are in UTC. Home Assistant timezone is {ha_timezone}.",
            },
        }
    except Exception:
        # Fallback if config fetch fails
        return {
            "data": data,
            "metadata": {
                "home_assistant_timezone": "Unknown",
                "timestamp_format": "ISO 8601 (UTC)",
                "note": "All timestamps are in UTC. Could not fetch Home Assistant timezone.",
            },
        }
