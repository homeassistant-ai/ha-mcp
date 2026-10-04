"""Input-validation rejections for config writes (issue #2649).

Kept out of tools_config_automations.py so that file stays under its
module-size baseline. The helpers raise through ``raise_tool_error`` and
carry no client or Home Assistant dependency.
"""

from __future__ import annotations

from typing import Any

from ..errors import ErrorCode, create_config_error, create_error_response
from .helpers import raise_tool_error


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
    enabled_in_config = isinstance(config_dict, dict) and "enabled" in config_dict
    if not missing_fields and not enabled_in_config:
        return
    if missing_fields:
        if enabled_in_config:
            raise_tool_error(
                create_config_error(
                    f"Missing required fields: {', '.join(missing_fields)}. "
                    "'enabled' is a runtime-only tool parameter, not a valid "
                    "automation config key — pass it to ha_config_set_automation "
                    "as enabled=True/False instead of putting it in config.",
                    identifier=identifier,
                    missing_fields=missing_fields,
                )
            )
        raise_tool_error(
            create_config_error(
                f"Missing required fields: {', '.join(missing_fields)}. "
                "Required automation fields go inside `config`, e.g. "
                "config={`alias`: 'My Automation', `triggers`: [...], "
                "`actions`: [...]}.",
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
