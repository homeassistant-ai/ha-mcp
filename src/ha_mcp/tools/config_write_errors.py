"""Input-validation rejections for config writes (issue #2649).

These are HA-MCP's own checks, not Core rules. The tools send each config as
written; Core validates it and stores it in its own form, which the get tool
returns verbatim. Kept out of tools_config_automations.py so that file stays
under its module-size baseline. The helpers raise through ``raise_tool_error``
and carry no client or Home Assistant dependency.
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


def reject_sequence_misroute(
    config_dict: dict[str, Any],
    entity_id: str | None,
    source: str = "config",
) -> None:
    """Raise when a 'sequence' key without actions shows the config is a script.

    A runtime-only ``enabled`` key folds into the same rejection instead of
    costing another round trip.
    """
    if "sequence" not in config_dict or {"actions", "action"} & config_dict.keys():
        return
    context: dict[str, Any] = {"found_key": "sequence"}
    if entity_id:
        context["entity_id"] = entity_id
    message = "Config has 'sequence' and no 'actions'"
    if source == "python_transform":
        automation_fix = (
            "In the python_transform expression, replace 'sequence' with 'actions'"
        )
    else:
        automation_fix = "For an automation, replace 'sequence' with 'actions'"
    if not {"triggers", "trigger"} & config_dict.keys():
        automation_fix += " and add 'triggers'"
    suggestions = [
        "Did you mean ha_config_set_script? Scripts use 'sequence' directly.",
        f"{automation_fix}.",
    ]
    if config_has_enabled(config_dict):
        context["invalid_key"] = "enabled"
        message += f". {ENABLED_MISPLACED_GUIDANCE}"
        suggestions.extend([ENABLED_REMOVE_SUGGESTION, ENABLED_UNCHANGED_SUGGESTION])
    raise_tool_error(
        create_error_response(
            code=ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS,
            message=message,
            details=(
                "Config contains 'sequence', which belongs to scripts. "
                "Automations use 'triggers' and 'actions'; scripts use 'sequence'."
            ),
            suggestions=suggestions,
            context=context,
        )
    )


def reject_enabled_in_config(config_dict: dict[str, Any]) -> None:
    """Raise when the runtime-only ``enabled`` key sits inside the config."""
    if not config_has_enabled(config_dict):
        return
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "'enabled' is a runtime-only tool parameter, not a valid "
            "automation config key",
            suggestions=[ENABLED_REMOVE_SUGGESTION, ENABLED_UNCHANGED_SUGGESTION],
            context={"action": "set", "invalid_key": "enabled"},
        )
    )
