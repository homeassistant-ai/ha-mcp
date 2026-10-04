"""Parameter parsing for ha_set_entity's list/dict arguments."""

import json
from typing import Any

from ..errors import ErrorCode, create_error_response
from .coercion import parse_json_param, parse_string_list_param
from .helpers import raise_tool_error
from .tools_voice_assistant import KNOWN_ASSISTANTS


def _parse_string_list_field(
    value: str | list[str] | None,
    field_name: str,
) -> list[str] | None:
    """Parse and validate a string-list field (aliases, labels, etc.)."""
    if value is not None:
        try:
            return parse_string_list_param(value, field_name)
        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid {field_name} parameter: {e}",
                )
            )
    return None


def _parse_aliases_param(
    aliases: str | list[str | None] | None,
) -> list[str | None] | None:
    """Parse aliases, keeping ``null`` entries.

    HA stores the entity's own (computed) name as a ``null`` entry in
    ``aliases`` (issue #2495); it must survive the round trip.
    """
    if aliases is None:
        return None
    parsed: Any = aliases
    if isinstance(aliases, str):
        try:
            parsed = json.loads(aliases)
        except (json.JSONDecodeError, RecursionError) as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid aliases parameter: Invalid JSON in aliases: {e}",
                )
            )
    if not isinstance(parsed, list) or not all(
        item is None or isinstance(item, str) for item in parsed
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Invalid aliases parameter: aliases must be a JSON array of "
                "strings (null entries stand for the entity's own name)",
            )
        )
    return parsed


def _parse_categories_param(
    categories: dict[str, str | None] | None,
) -> dict[str, str | None] | None:
    """Parse and validate the categories parameter."""
    if categories is not None:
        try:
            parsed_cats = parse_json_param(categories, "categories")
        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid categories parameter: {e}",
                )
            )
        if not isinstance(parsed_cats, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "categories must be a dict mapping scope to category_id, "
                    'e.g. {"automation": "my_category_id"}',
                )
            )
        return parsed_cats
    return None


def _parse_options_param(
    options: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]] | None:
    """Parse and validate the options parameter."""
    if options is not None:
        try:
            parsed_opts = parse_json_param(options, "options")
        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid options parameter: {e}",
                )
            )
        if not isinstance(parsed_opts, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"options must be a dict mapping domain to a sub-dict "
                    f"(got {type(parsed_opts).__name__}), "
                    'e.g. {"sensor": {"display_precision": 2}}',
                )
            )
        if not parsed_opts:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "options cannot be an empty dict — pass at least one "
                    'domain entry, e.g. {"sensor": {"display_precision": 2}}, '
                    "or omit the parameter entirely.",
                )
            )
        bad_subs = [
            f"{k!r}: {type(v).__name__}"
            for k, v in parsed_opts.items()
            if not isinstance(v, dict)
        ]
        if bad_subs:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "options sub-values must be dicts, got non-dict for: "
                    f"{', '.join(bad_subs)}",
                )
            )
        return parsed_opts
    return None


def _parse_expose_to_param(
    expose_to: dict[str, bool] | None,
) -> dict[str, bool] | None:
    """Parse and validate the expose_to parameter."""
    if expose_to is not None:
        try:
            parsed = parse_json_param(expose_to, "expose_to")
        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(e),
                )
            )
        if not isinstance(parsed, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "expose_to must be a dict mapping assistant IDs to booleans, "
                    'e.g. {"conversation": true, "cloud.alexa": false}',
                )
            )
        # Validate assistant names
        invalid_assistants = [a for a in parsed if a not in KNOWN_ASSISTANTS]
        if invalid_assistants:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid assistant(s) in expose_to: {invalid_assistants}. "
                    f"Valid: {KNOWN_ASSISTANTS}",
                )
            )
        # Values are already bool (enforced by the dict[str, bool] annotation)
        return parsed
    return None
