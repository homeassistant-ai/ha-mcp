"""Input-validation rejections for config writes (issue #2649).

Kept out of tools_config_automations.py so that file stays under its
module-size baseline. The helpers raise through ``raise_tool_error`` and
carry no client or Home Assistant dependency.
"""

from __future__ import annotations

from typing import Any

from ..errors import ErrorCode, create_config_error, create_error_response
from .helpers import raise_tool_error

# One-line guidance reused wherever the runtime-only ``enabled`` key folds
# into another rejection (issue #2649).
ENABLED_MISPLACED_GUIDANCE = (
    "'enabled' is a runtime-only tool parameter, not a valid automation "
    "config key — pass it to ha_config_set_automation as enabled=True/False "
    "instead of putting it in config."
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


def reject_invalid_config_inputs(
    config_dict: dict[str, Any],
    missing_fields: list[str],
    identifier: str | None,
) -> None:
    """Raise the structured rejection for a config write's input violations.

    A missing-field rejection says where the field goes and shows the
    expected shape in one line; a runtime-only ``enabled`` key together with
    missing required fields surfaces in ONE rejection instead of one round
    trip each (issue #2649). Returns silently when the inputs are valid.
    """
    enabled_in_config = config_has_enabled(config_dict)
    if not missing_fields and not enabled_in_config:
        return
    shape = _missing_fields_shape("use_blueprint" in config_dict)
    if missing_fields:
        if enabled_in_config:
            raise_tool_error(
                create_config_error(
                    f"Missing required fields: {', '.join(missing_fields)}. "
                    f"{ENABLED_MISPLACED_GUIDANCE} Required automation fields "
                    f"go inside `config`, e.g. {shape}.",
                    identifier=identifier,
                    missing_fields=missing_fields,
                )
            )
        raise_tool_error(
            create_config_error(
                f"Missing required fields: {', '.join(missing_fields)}. "
                "Required automation fields go inside `config`, e.g. "
                f"{shape}.",
                identifier=identifier,
                missing_fields=missing_fields,
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "'enabled' is a runtime-only tool parameter, not a valid "
            "automation config key",
            suggestions=[
                "Remove 'enabled' from config and pass enabled=True or False "
                + "to ha_config_set_automation",
                "Use enabled=None to leave the current runtime state unchanged",
            ],
            context={"action": "set", "invalid_key": "enabled"},
        )
    )
