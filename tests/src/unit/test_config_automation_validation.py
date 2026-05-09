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


class TestEmptyTriggerSceneCreateDefense:
    """Issue #1169 — Path-1 misroute defense.

    BAT validation of #1168 (the scene CRUD tools landing for #995)
    surfaced gpt-4o-mini constructing an automation that wraps
    ``service: scene.create`` because no scene tools existed at the time.
    HA's REST endpoint accepts ``trigger: []`` and produces a never-firing
    automation — concrete corruption, not a draft. The narrow gate here
    rejects only the misroute pattern (empty trigger + scene.create
    action); legitimate empty-trigger drafts paired with other actions
    still pass through.
    """

    def test_empty_trigger_with_scene_create_service_key_rejected(self) -> None:
        """Legacy ``service: scene.create`` triggers the gate."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._validate_required_fields(
                {
                    "alias": "Movie Snapshot",
                    "trigger": [],
                    "action": [
                        {
                            "service": "scene.create",
                            "data": {
                                "scene_id": "movie_night",
                                "snapshot_entities": ["light.living_room"],
                            },
                        }
                    ],
                },
                identifier=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        # Routing hint must name ha_config_set_scene as the right tool.
        all_text = json.dumps(error)
        assert "ha_config_set_scene" in all_text
        assert "scene.create" in error["message"]

    def test_empty_trigger_with_scene_create_action_key_rejected(self) -> None:
        """Modern ``action: scene.create`` (HA 2024.8+) also triggers the gate."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._validate_required_fields(
                {
                    "alias": "Movie Snapshot",
                    "trigger": [],
                    "action": [
                        {
                            "action": "scene.create",
                            "data": {"scene_id": "movie_night"},
                        }
                    ],
                },
                identifier=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "ha_config_set_scene" in json.dumps(error)

    def test_empty_trigger_with_other_action_passes(self) -> None:
        """Draft preservation: empty trigger paired with a non-scene.create
        action passes through unchanged. Some users save automations as
        drafts and add triggers later; this is not the misroute pattern."""
        AutomationConfigTools._validate_required_fields(
            {
                "alias": "Draft",
                "trigger": [],
                "action": [
                    {"service": "light.turn_on", "target": {"entity_id": "light.x"}}
                ],
            },
            identifier=None,
        )

    def test_non_empty_trigger_with_scene_create_passes(self) -> None:
        """Legitimate use case: a trigger-driven scene snapshot (capture the
        current state when an event fires). Not the misroute pattern."""
        AutomationConfigTools._validate_required_fields(
            {
                "alias": "Snapshot on guest mode",
                "trigger": [
                    {
                        "platform": "state",
                        "entity_id": "input_boolean.guest_mode",
                        "to": "on",
                    }
                ],
                "action": [
                    {
                        "service": "scene.create",
                        "data": {"scene_id": "before_guests"},
                    }
                ],
            },
            identifier=None,
        )

    def test_use_blueprint_empty_trigger_strip_preserved(self) -> None:
        """Backwards-compat: ``use_blueprint`` configs that pass empty
        ``trigger: []`` historically have those stripped before validation
        (the blueprint provides the trigger). The new misroute gate must
        not break that path — checked after ``use_blueprint`` strip, the
        empty trigger is gone and the gate doesn't fire even with a
        scene.create action elsewhere."""
        AutomationConfigTools._validate_required_fields(
            {
                "alias": "Motion Light",
                "use_blueprint": {
                    "path": "homeassistant/motion_light.yaml",
                    "input": {
                        "motion_entity": "binary_sensor.motion",
                        "light_target": {"entity_id": "light.kitchen"},
                    },
                },
                # These should be stripped by the use_blueprint pre-pass and
                # never reach the misroute gate.
                "trigger": [],
                "action": [],
            },
            identifier=None,
        )

    def test_empty_trigger_scene_create_indices_in_context(self) -> None:
        """Multi-action lists surface the indices of the scene.create
        actions in the error context for the LLM to pinpoint."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._validate_required_fields(
                {
                    "alias": "Multi",
                    "trigger": [],
                    "action": [
                        {"service": "light.turn_on"},
                        {"service": "scene.create", "data": {"scene_id": "a"}},
                        {"action": "scene.create", "data": {"scene_id": "b"}},
                    ],
                },
                identifier=None,
            )
        body = json.loads(str(exc_info.value))
        assert body.get("scene_create_action_indices") == [1, 2]
