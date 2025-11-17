"""
Shared utility functions for MCP tool modules.

This module provides common helper functions used across multiple tool registration modules.
"""

import json
from typing import Any

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


def parse_json_param(
    param: str | dict | list | None, param_name: str = "parameter"
) -> dict | list | None:
    """
    Parse configuration from JSON/YAML string or return existing dict/list.

    Supports multiple input formats:
    1. Dict/list (passthrough)
    2. JSON string (parsed with json.loads)
    3. YAML string (parsed with yaml.safe_load, if PyYAML available)

    Tries JSON first for performance, falls back to YAML if available.

    Args:
        param: Config as dict, list, JSON string, or YAML string
        param_name: Parameter name for error messages

    Returns:
        Parsed dict/list or None

    Raises:
        ValueError: If parsing fails or wrong type

    Examples:
        # Dict passthrough
        parse_json_param({"views": []}) → {"views": []}

        # JSON string
        parse_json_param('{"views": []}') → {"views": []}

        # YAML string (if PyYAML available)
        parse_json_param('views:\\n  - title: Home') → {"views": [{"title": "Home"}]}
    """
    if param is None:
        return None

    if isinstance(param, (dict, list)):
        return param

    if isinstance(param, str):
        # Try JSON first (faster, MCP-native)
        try:
            parsed = json.loads(param)
            if not isinstance(parsed, (dict, list)):
                raise ValueError(
                    f"{param_name} must be a JSON object or array, got {type(parsed).__name__}"
                )
            return parsed
        except json.JSONDecodeError:
            # Fallback to YAML if available
            if YAML_AVAILABLE:
                try:
                    parsed = yaml.safe_load(param)
                    if not isinstance(parsed, (dict, list)):
                        raise ValueError(
                            f"{param_name} must be object/array, got {type(parsed).__name__}"
                        )
                    return parsed
                except yaml.YAMLError as e:
                    raise ValueError(f"Invalid JSON/YAML in {param_name}: {e}")
            else:
                raise ValueError(
                    f"Invalid JSON in {param_name}. YAML parsing not available (PyYAML not installed)."
                )

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
