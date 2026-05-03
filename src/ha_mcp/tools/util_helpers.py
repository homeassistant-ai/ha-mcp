"""
Shared utility functions for MCP tool modules.

This module provides common helper functions used across multiple tool registration modules.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, overload

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)

logger = logging.getLogger(__name__)

# Strips ANSI terminal escape codes from container/log output.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


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
        raise ValueError(f"{param_name} must be a boolean value, got '{value}'")

    raise ValueError(f"{param_name} must be bool or string, got {type(value).__name__}")


@overload
def coerce_int_param(
    value: int | str | None,
    param_name: str = ...,
    *,
    default: int,
    min_value: int | None = ...,
    max_value: int | None = ...,
) -> int: ...


@overload
def coerce_int_param(
    value: int | str | None,
    param_name: str = ...,
    *,
    default: None = ...,
    min_value: int | None = ...,
    max_value: int | None = ...,
) -> int | None: ...


def coerce_int_param(
    value: int | str | None,
    param_name: str = "parameter",
    *,
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

    # Apply constraints — raise for below-minimum (indicates caller bug),
    # clamp for above-maximum (soft cap for oversized requests)
    if min_value is not None and result < min_value:
        raise ValueError(f"{param_name} must be at least {min_value}, got {result}")
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
            raise ValueError(f"Invalid JSON in {param_name}: {e}") from e

    raise ValueError(
        f"{param_name} must be string, dict, list, or None, got {type(param).__name__}"
    )


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
        # Try JSON array first
        if param.strip().startswith("["):
            try:
                parsed = json.loads(param)
                if not isinstance(parsed, list):
                    raise ValueError(f"{param_name} must be a JSON array")
                if not all(isinstance(item, str) for item in parsed):
                    raise ValueError(f"{param_name} must be a JSON array of strings")
                return parsed
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in {param_name}: {e}") from e
        # Comma-separated fallback (opt-in)
        if allow_csv:
            return [item.strip() for item in param.split(",") if item.strip()]
        # Original behavior: attempt JSON parse (will fail for plain strings)
        try:
            parsed = json.loads(param)
            if not isinstance(parsed, list):
                raise ValueError(f"{param_name} must be a JSON array")
            if not all(isinstance(item, str) for item in parsed):
                raise ValueError(f"{param_name} must be a JSON array of strings")
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {param_name}: {e}") from e

    raise ValueError(f"{param_name} must be string, list, or None")


def build_pagination_metadata(
    total_count: int, offset: int, limit: int, count: int
) -> dict[str, Any]:
    """Build standardized pagination metadata for paginated responses.

    Args:
        total_count: Total number of items matching filters (before pagination).
        offset: Current pagination offset.
        limit: Maximum items per page (must be positive).
        count: Number of items in this page.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    has_more = (offset + count) < total_count
    return {
        "total_count": total_count,
        "offset": offset,
        "limit": limit,
        "count": count,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }


def unwrap_service_response(result: dict[str, Any]) -> dict[str, Any]:
    """Extract service_response from HA call_service result.

    HA's call_service with return_response wraps results in
    {"changed_states": [...], "service_response": {...}}.
    Returns service_response if present and is a dict, otherwise the original result.
    """
    sr = result.get("service_response")
    return sr if isinstance(sr, dict) else result


async def add_timezone_metadata(client: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Add Home Assistant timezone to tool responses for local time context."""
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


async def wait_for_entity_registered(
    client: Any,
    entity_id: str,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
) -> bool:
    """
    Poll until an entity is registered and accessible via the state API.

    Used after config create/update operations to confirm the entity is queryable.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity ID to wait for (e.g., 'automation.morning_routine')
        timeout: Maximum time to wait in seconds
        poll_interval: Time between polls in seconds

    Returns:
        True if entity became accessible, False if timed out
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            state = await client.get_entity_state(entity_id)
            if state:
                logger.debug(
                    f"Entity {entity_id} registered after {time.monotonic() - start:.1f}s"
                )
                return True
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                pass  # Expected: entity not registered yet
            else:
                logger.warning(f"Unexpected API error polling {entity_id}: {e}")
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
            logger.warning(f"Connection/auth error polling {entity_id}: {e}")
            raise
        await asyncio.sleep(poll_interval)
    logger.warning(f"Entity {entity_id} not registered within {timeout}s")
    return False


async def wait_for_entity_removed(
    client: Any,
    entity_id: str,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
) -> bool:
    """
    Poll until an entity is no longer accessible via the state API.

    Used after config delete operations to confirm the entity is gone.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity ID to wait for removal
        timeout: Maximum time to wait in seconds
        poll_interval: Time between polls in seconds

    Returns:
        True if entity was removed, False if timed out (entity still exists)
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            state = await client.get_entity_state(entity_id)
            if not state:
                logger.debug(
                    f"Entity {entity_id} removed after {time.monotonic() - start:.1f}s"
                )
                return True
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                logger.debug(
                    f"Entity {entity_id} removed (404) after {time.monotonic() - start:.1f}s"
                )
                return True
            logger.warning(f"Unexpected API error polling {entity_id} removal: {e}")
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
            logger.warning(f"Connection/auth error polling {entity_id} removal: {e}")
            raise
        await asyncio.sleep(poll_interval)
    logger.warning(f"Entity {entity_id} still exists after {timeout}s")
    return False


async def wait_for_state_change(
    client: Any,
    entity_id: str,
    expected_state: str | None = None,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
    initial_state: str | None = None,
) -> dict[str, Any] | None:
    """
    Poll until an entity's state changes (optionally to a specific value).

    Used after service calls to verify the operation took effect.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity to monitor
        expected_state: If set, wait for this specific state value.
                        If None, wait for any change from initial_state.
        timeout: Maximum time to wait in seconds
        poll_interval: Time between polls in seconds
        initial_state: The state before the operation. If None, it will be
                       fetched automatically.

    Returns:
        The entity state dict if the change was detected, None if timed out
    """
    # Capture initial state if not provided
    if initial_state is None:
        try:
            raw_initial = await client.get_entity_state(entity_id)
            if isinstance(raw_initial, dict):
                initial_state = raw_initial.get("state")
        except HomeAssistantAPIError:
            logger.debug(
                f"Could not fetch initial state for {entity_id} — will detect any change"
            )
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
            logger.warning(
                f"Connection/auth error fetching initial state for {entity_id}: {e}"
            )
            raise

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            raw = await client.get_entity_state(entity_id)
            state_data: dict[str, Any] | None = raw if isinstance(raw, dict) else None
            if state_data:
                current = state_data.get("state")
                if expected_state is not None and current == expected_state:
                    logger.debug(
                        f"Entity {entity_id} reached state '{expected_state}' "
                        f"after {time.monotonic() - start:.1f}s"
                    )
                    return state_data
                if (
                    expected_state is None
                    and initial_state is not None
                    and current != initial_state
                ):
                    logger.debug(
                        f"Entity {entity_id} changed from '{initial_state}' to '{current}' "
                        f"after {time.monotonic() - start:.1f}s"
                    )
                    return state_data
                # If initial state fetch failed, use first successful poll as baseline
                if (
                    expected_state is None
                    and initial_state is None
                    and current is not None
                ):
                    initial_state = current
        except HomeAssistantAPIError as e:
            logger.debug(f"API error polling {entity_id} state: {e}")
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
            logger.warning(f"Connection/auth error polling {entity_id} state: {e}")
            raise
        await asyncio.sleep(poll_interval)

    logger.warning(f"Entity {entity_id} state did not change within {timeout}s")
    return None


async def fetch_entity_category(client: Any, entity_id: str, scope: str) -> str | None:
    """Fetch a category ID for an entity from the entity registry.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity to look up (e.g., 'automation.morning_routine')
        scope: Category scope (e.g., 'automation', 'script', 'helpers')

    Returns:
        Category ID string if set, None otherwise
    """
    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
        if result.get("success"):
            categories = result.get("result", {}).get("categories", {})
            cat_id = categories.get(scope)
            return str(cat_id) if cat_id is not None else None
    except Exception as e:
        logger.warning(f"Failed to fetch category for {entity_id}: {e}")
    return None


async def apply_entity_category(
    client: Any,
    entity_id: str,
    category: str,
    scope: str,
    result_dict: dict[str, Any],
    entity_type: str = "entity",
) -> None:
    """Apply a category to an entity via the entity registry.

    Updates result_dict in-place with 'category' on success or
    'category_warning' on failure.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity to update
        category: Category ID to assign
        scope: Category scope (e.g., 'automation', 'script')
        result_dict: Tool result dict to update with category status
        entity_type: Human-readable type for warning messages
    """
    try:
        ws_result = await client.send_websocket_message(
            {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "categories": {scope: category},
            }
        )
        if ws_result.get("success"):
            result_dict["category"] = category
        else:
            error_detail = ws_result.get("error", {})
            error_msg = (
                error_detail.get("message", "Unknown error")
                if isinstance(error_detail, dict)
                else str(error_detail)
            )
            logger.warning(f"Failed to set category for {entity_id}: {error_msg}")
            result_dict["category_warning"] = (
                f"{entity_type.capitalize()} saved but failed to set category: {error_msg}"
            )
    except Exception as e:
        logger.warning(f"Failed to set category for {entity_id}: {e}")
        result_dict["category_warning"] = (
            f"{entity_type.capitalize()} saved but failed to set category: {e}"
        )


def coerce_to_list(value: Any) -> list[Any]:
    """Return value as a list: list → as-is, dict/other → [value], None/falsy → []."""
    if isinstance(value, list):
        return value
    return [value] if value else []


def merge_validation_meta(
    result: dict[str, Any], validation_meta: dict[str, Any]
) -> None:
    """Attach reference-validator output to a set-tool success ``result``.

    Produces a single nested ``validation`` field when there's anything
    worth reporting - warnings, skipped templates, or a blueprint
    short-circuit. Keeps the happy-path response unchanged.

    Shared between ``ha_config_set_automation`` and
    ``ha_config_set_script``; see
    :mod:`ha_mcp.tools.reference_validator` for the validator itself
    and #940 for background.
    """
    warnings = validation_meta.get("warnings") or []
    unvalidated_templates = validation_meta.get("unvalidated_templates") or 0
    blueprint_skipped = bool(validation_meta.get("blueprint_skipped"))

    if not warnings and not unvalidated_templates and not blueprint_skipped:
        return

    entry: dict[str, Any] = {}
    if warnings:
        entry["warnings"] = warnings
    if unvalidated_templates:
        entry["unvalidated_templates"] = unvalidated_templates
    if blueprint_skipped:
        entry["blueprint_skipped"] = True
    result["validation"] = entry
