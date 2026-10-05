"""Input-validation rejections for config writes (issue #2649).

Kept out of tools_config_automations.py so that file stays under its
module-size baseline. The helpers raise through ``raise_tool_error`` and
carry no client or Home Assistant dependency.
"""

from __future__ import annotations

from typing import Any

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

# Guidance reused wherever the runtime-only ``enabled`` key folds into
# another rejection.
ENABLED_MISPLACED_GUIDANCE = (
    "'enabled' is a runtime-only tool parameter, not a valid automation "
    "config key — pass it to ha_config_set_automation as enabled=True/False "
    "instead of putting it in config."
)
ENABLED_REMOVE_SUGGESTION = (
    "Remove 'enabled' from config and pass enabled=True or False "
    "to ha_config_set_automation"
)
ENABLED_UNCHANGED_SUGGESTION = (
    "Use enabled=None to leave the current runtime state unchanged"
)


def config_has_enabled(config: Any) -> bool:
    """Whether a config body carries the runtime-only ``enabled`` key."""
    return isinstance(config, dict) and "enabled" in config


def _missing_fields_shape(is_blueprint: bool) -> str:
    """One-line expected config shape with copy-pasteable quoted keys."""
    if is_blueprint:
        return (
            "config={'alias': 'My Automation', 'use_blueprint': "
            "{'path': 'light.yaml', 'input': {...}}}"
        )
    return "config={'alias': 'My Automation', 'triggers': [...], 'actions': [...]}"


def _missing_field_guidance(
    missing_fields: list[str], is_blueprint: bool, source: str
) -> tuple[str, list[str]]:
    """Message and suggestions for missing required fields, worded by source.

    For the ``config`` argument the guidance points inside ``config``; for a
    ``python_transform`` the config came from a transform expression, so the
    guidance points at the transform instead (telling it to send
    ``config={...}`` would earn a write-modes rejection).
    """
    fields = ", ".join(missing_fields)
    if source == "python_transform":
        first = missing_fields[0]
        return (
            f"Missing required fields: {fields}. Add them in your "
            f"python_transform expression, e.g. config['{first}'] = ... .",
            [
                "Set the missing field(s) on the transformed config, e.g. "
                f"config['{first}'] = '...'."
            ],
        )
    shape = _missing_fields_shape(is_blueprint)
    return (
        f"Missing required fields: {fields}. Required automation fields go "
        f"inside `config`, e.g. {shape}.",
        [f"Add the missing field(s) inside the `config` argument: {shape}."],
    )


def reject_invalid_config_inputs(
    config_dict: dict[str, Any],
    missing_fields: list[str],
    identifier: str | None,
    source: str = "config",
) -> None:
    """Raise the structured rejection for a config write's input violations.

    A missing-field rejection says where the field goes and shows the
    expected shape in one line; a runtime-only ``enabled`` key together with
    missing required fields surfaces in ONE rejection instead of one round
    trip each. Returns silently when the inputs are valid.
    """
    enabled_in_config = config_has_enabled(config_dict)
    if not missing_fields and not enabled_in_config:
        return
    context: dict[str, Any] = {}
    if identifier:
        context["identifier"] = identifier
    if missing_fields:
        context["missing_fields"] = missing_fields
        message, suggestions = _missing_field_guidance(
            missing_fields, "use_blueprint" in config_dict, source
        )
        if enabled_in_config:
            context["invalid_key"] = "enabled"
            message = f"{message} {ENABLED_MISPLACED_GUIDANCE}"
            suggestions = suggestions + [
                ENABLED_REMOVE_SUGGESTION,
                ENABLED_UNCHANGED_SUGGESTION,
            ]
        raise_tool_error(
            create_error_response(
                code=ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS,
                message=message,
                details=f"Missing required fields: {', '.join(missing_fields)}",
                suggestions=suggestions,
                context=context or None,
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "'enabled' is a runtime-only tool parameter, not a valid "
            "automation config key",
            suggestions=[ENABLED_REMOVE_SUGGESTION, ENABLED_UNCHANGED_SUGGESTION],
            context={"action": "set", "invalid_key": "enabled"},
        )
    )
