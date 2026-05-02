"""Unit tests for AutomationConfigTools validation helpers.

Covers:
- _validate_required_fields: missing-field errors and the ha_config_set_script hint
- _parse_and_validate_config: VALIDATION_INVALID_JSON error message and suggestions
- _validate_required_fields: sun trigger event pre-validation
"""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_automations import AutomationConfigTools


def _error_from_tool_error(exc: ToolError) -> dict:
    return json.loads(str(exc))["error"]


class TestParseAndValidateConfig:
    """Tests for _parse_and_validate_config JSON error suggestions."""

    def test_invalid_json_string_suggests_dict(self) -> None:
        """JSON parse error includes a 'pass as dict' suggestion."""
        # Simulate a model sending config as a broken JSON string
        # (e.g. unquoted key, which is a common model mistake)
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._parse_and_validate_config(
                '{alias: "x", "trigger": []}'  # unquoted key — invalid JSON
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_JSON"
        assert "dict" in json.dumps(error)


class TestValidateRequiredFields:
    """Tests for the static _validate_required_fields helper."""

    def test_valid_automation_passes(self) -> None:
        """Complete automation config raises nothing."""
        AutomationConfigTools._validate_required_fields(
            {"alias": "x", "trigger": [], "action": []},
            identifier=None,
        )

    def test_missing_trigger_without_sequence_uses_generic_error(self) -> None:
        """Missing fields without a 'sequence' key emit the default suggestions."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._validate_required_fields(
                {"alias": "x", "action": []},
                identifier=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "CONFIG_MISSING_REQUIRED_FIELDS"
        assert "trigger" in error["message"]
        # The generic suggestion should NOT mention ha_config_set_script.
        all_text = json.dumps(error)
        assert "ha_config_set_script" not in all_text

    def test_sequence_in_config_hints_at_set_script(self) -> None:
        """A config with 'sequence' and missing trigger/action hints at ha_config_set_script."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._validate_required_fields(
                {"alias": "Goodnight", "sequence": [{"service": "light.turn_off"}]},
                identifier=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "CONFIG_MISSING_REQUIRED_FIELDS"
        # Primary suggestion must name the correct tool.
        assert "ha_config_set_script" in error.get("suggestion", "")
        # And the mention of 'sequence' should appear in either details or suggestion list.
        all_text = json.dumps(error)
        assert "sequence" in all_text

    def test_sequence_in_config_with_trigger_but_no_action_still_hints(self) -> None:
        """Sequence + trigger but no action still triggers the script hint."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._validate_required_fields(
                {
                    "alias": "x",
                    "trigger": [],
                    "sequence": [{"service": "light.turn_off"}],
                },
                identifier=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert "ha_config_set_script" in error.get("suggestion", "")


class TestValidateConditionBlocks:
    """Pre-validation of condition blocks for platform vs condition confusion.

    Triggers use 'platform'; conditions use 'condition'. Models familiar with
    trigger syntax often write {'platform': 'state', ...} in condition lists,
    which HA accepts without a 400 but then crashes with an unhelpful 500.
    """

    def _base_config(self, conditions: object) -> dict:
        return {"alias": "x", "trigger": [], "action": [], "condition": conditions}

    def test_valid_state_condition_passes(self) -> None:
        AutomationConfigTools._validate_required_fields(
            self._base_config([{"condition": "state", "entity_id": "input_boolean.x", "state": "on"}]),
            identifier=None,
        )

    def test_valid_sun_condition_passes(self) -> None:
        AutomationConfigTools._validate_required_fields(
            self._base_config([{"condition": "sun", "after": "sunset"}]),
            identifier=None,
        )

    def test_platform_key_without_condition_key_raises(self) -> None:
        """{'platform': 'state'} in a condition list triggers the helpful error."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._validate_required_fields(
                self._base_config([{"platform": "state", "entity_id": "input_boolean.x"}]),
                identifier=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        blob = json.dumps(error)
        assert "condition" in blob
        assert "platform" in blob

    def test_single_condition_dict_also_checked(self) -> None:
        """Non-list condition (single dict) is also validated."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._validate_required_fields(
                self._base_config({"platform": "state", "entity_id": "input_boolean.x"}),
                identifier=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"

    def test_platform_with_condition_key_not_flagged(self) -> None:
        """Item that has both 'platform' and 'condition' is left for HA to validate."""
        AutomationConfigTools._validate_required_fields(
            self._base_config([{"condition": "state", "platform": "extra", "entity_id": "x", "state": "on"}]),
            identifier=None,
        )
