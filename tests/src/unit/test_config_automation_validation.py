"""Unit tests for the input guards ha_config_set_automation applies itself.

Core validates the automation (required keys, root forms, trigger and
condition keys); these are HA-MCP's own checks that run before any read or
write: the script misroute hint, the runtime-only ``enabled`` key, the
scene.create misroute, the duplicate-id guard and the JSON parse error.
"""

from __future__ import annotations

import json

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_config_automations import AutomationConfigTools


def _body_from_tool_error(exc: ToolError) -> dict:
    return json.loads(str(exc))


def _error_from_tool_error(exc: ToolError) -> dict:
    return _body_from_tool_error(exc)["error"]


class TestParseAndValidateConfig:
    """Tests for _parse_and_validate_config JSON error suggestions."""

    def test_invalid_json_string_suggests_dict(self) -> None:
        """JSON parse error includes a 'pass as dict' suggestion."""
        # Simulate a model sending config as a broken JSON string
        # (e.g. unquoted key, which is a common model mistake)
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._parse_and_validate_config(
                '{alias: "x", "triggers": []}'  # unquoted key — invalid JSON
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_JSON"
        assert "dict" in json.dumps(error)


class TestInputGuards:
    """Tests for the static _check_input_guards helper."""

    def test_config_core_accepts_passes_without_an_alias(self) -> None:
        """Core names an automation without an alias itself; refusing it here
        rejected a config Home Assistant accepts."""
        AutomationConfigTools._check_input_guards(
            {"triggers": [], "actions": []}, entity_id=None
        )

    def test_sequence_in_config_hints_at_set_script(self) -> None:
        """A config with 'sequence' and no actions hints at ha_config_set_script."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {"alias": "Goodnight", "sequence": [{"action": "light.turn_off"}]},
                entity_id=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "CONFIG_MISSING_REQUIRED_FIELDS"
        # Primary suggestion must name the correct tool.
        assert "ha_config_set_script" in error.get("suggestion", "")
        all_text = json.dumps(error)
        assert "sequence" in all_text
        assert "add 'triggers'" in all_text

    def test_sequence_in_config_with_trigger_but_no_action_still_hints(self) -> None:
        """Sequence + triggers but no actions still triggers the script hint."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {
                    "alias": "x",
                    "triggers": [],
                    "sequence": [{"action": "light.turn_off"}],
                },
                entity_id=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert "ha_config_set_script" in error.get("suggestion", "")
        assert "add 'triggers'" not in json.dumps(error)

    @pytest.mark.parametrize("actions_key", ["actions", "action"])
    def test_sequence_beside_actions_is_left_to_core(self, actions_key: str) -> None:
        """With actions present the config is not script-shaped; Core judges
        the stray 'sequence' key."""
        AutomationConfigTools._check_input_guards(
            {"sequence": [], actions_key: [{"action": "light.turn_off"}]},
            entity_id=None,
        )

    def test_sequence_and_enabled_report_together(self) -> None:
        """A script-shaped config that also misplaces 'enabled' reports both
        violations in one rejection."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {"sequence": [{"action": "light.turn_on"}], "enabled": True},
                entity_id=None,
            )
        body = _body_from_tool_error(exc_info.value)
        assert body["invalid_key"] == "enabled"
        error = body["error"]
        all_text = json.dumps(error)
        assert "ha_config_set_script" in all_text
        assert "runtime-only" in all_text
        # The two guidance sentences are separated, not run together.
        assert ". 'enabled'" in error["message"]
        suggestions = error.get("suggestions", [])
        assert any("Remove 'enabled' from config" in s for s in suggestions)
        assert (
            "Use enabled=None to leave the current runtime state unchanged"
            in suggestions
        )

    def test_sequence_misroute_on_transform_points_at_transform(self) -> None:
        """A transform producing 'sequence' gets transform-expression guidance,
        not config={...} (which write modes would reject)."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {"sequence": [{"action": "light.turn_off"}]},
                entity_id="automation.x",
                source="python_transform",
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "CONFIG_MISSING_REQUIRED_FIELDS"
        all_text = json.dumps(error)
        assert "python_transform expression" in all_text
        assert "config={" not in all_text

    def test_enabled_in_config_alone_still_rejected(self) -> None:
        """The runtime-only key is a tool parameter, not config."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {"alias": "x", "triggers": [], "actions": [], "enabled": True},
                entity_id=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "runtime-only" in error["message"]

    def test_config_id_without_entity_id_is_refused_as_a_duplicate(self) -> None:
        """Creating with another automation's id would write a second one."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {"id": "1700000000000", "triggers": [], "actions": []},
                entity_id=None,
            )
        assert "entity_id='1700000000000'" in json.dumps(
            _error_from_tool_error(exc_info.value)
        )


class TestEmptyTriggerSceneCreateDefense:
    """Issue #1169 misroute-defense gate."""

    def test_empty_trigger_with_scene_create_service_key_rejected(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {
                    "alias": "Movie Snapshot",
                    "triggers": [],
                    "actions": [
                        {
                            "service": "scene.create",
                            "data": {
                                "scene_id": "movie_night",
                                "snapshot_entities": ["light.living_room"],
                            },
                        }
                    ],
                },
                entity_id=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene.create" in error["message"]
        # Routing-hint suggestions must name BOTH ha_config_set_scene
        # (the snapshot tool) and ha_config_set_helper (the
        # template-derivation tool) — pinned to ``error["suggestions"]``
        # so a stray reference elsewhere in the body wouldn't satisfy.
        suggestions = error.get("suggestions") or []
        suggestions_blob = " ".join(suggestions)
        assert "ha_config_set_scene" in suggestions_blob
        assert "ha_config_set_helper" in suggestions_blob

    def test_empty_trigger_with_scene_create_action_key_rejected(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {
                    "alias": "Movie Snapshot",
                    "triggers": [],
                    "actions": [
                        {
                            "action": "scene.create",
                            "data": {"scene_id": "movie_night"},
                        }
                    ],
                },
                entity_id=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        suggestions_blob = " ".join(error.get("suggestions") or [])
        assert "ha_config_set_scene" in suggestions_blob

    def test_singular_root_keys_reach_the_gate(self) -> None:
        """The config is no longer pluralised before the guards run, so a
        singular-key misroute must be caught as written."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {"trigger": [], "action": [{"action": "scene.create"}]},
                entity_id=None,
            )
        assert _error_from_tool_error(exc_info.value)["code"] == (
            "VALIDATION_INVALID_PARAMETER"
        )

    def test_trigger_none_with_scene_create_rejected(self) -> None:
        """R1 blocker 1: ``triggers: null`` counts as an empty trigger, like
        ``[]``."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {
                    "alias": "Movie",
                    "triggers": None,
                    "actions": [{"service": "scene.create", "data": {"scene_id": "x"}}],
                },
                entity_id=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        suggestions_blob = " ".join(error.get("suggestions") or [])
        assert "ha_config_set_scene" in suggestions_blob

    @pytest.mark.parametrize(
        ("case_name", "wrapper_action"),
        [
            (
                "sequence",
                {"sequence": [{"service": "scene.create"}]},
            ),
            (
                "parallel",
                {"parallel": [{"service": "scene.create"}]},
            ),
            (
                "choose-option-sequence",
                {
                    "choose": [
                        {"conditions": [], "sequence": [{"service": "scene.create"}]}
                    ]
                },
            ),
            (
                "choose-default",
                {
                    "choose": [{"conditions": [], "sequence": [{"service": "noop"}]}],
                    "default": [{"service": "scene.create"}],
                },
            ),
            (
                "if-then",
                {
                    "if": [{"condition": "state"}],
                    "then": [{"service": "scene.create"}],
                },
            ),
            (
                "if-else",
                {
                    "if": [{"condition": "state"}],
                    "else": [{"service": "scene.create"}],
                },
            ),
            (
                "deep-nested",
                {
                    "choose": [
                        {
                            "conditions": [],
                            "sequence": [{"sequence": [{"action": "scene.create"}]}],
                        }
                    ]
                },
            ),
        ],
    )
    def test_nested_scene_create_caught(
        self, case_name: str, wrapper_action: dict
    ) -> None:
        """R1 blocker 2: ``scene.create`` nested under HA's wrapper actions
        (``sequence`` / ``parallel`` / ``choose[*].sequence`` / ``choose
        default`` / ``if`` ``then``+``else``) is caught alongside the
        top-level case."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {
                    "alias": f"Nested-{case_name}",
                    "triggers": [],
                    "actions": [wrapper_action],
                },
                entity_id=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        suggestions_blob = " ".join(error.get("suggestions") or [])
        assert "ha_config_set_scene" in suggestions_blob

    def test_action_as_single_dict_caught(self) -> None:
        """R1 while-you're-in: ``actions`` accepted as a single dict (not a
        list) — ``coerce_to_list`` lifts it; the gate must still catch
        this shape so a regression that drops the coerce wouldn't silently
        re-open the gate."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {
                    "alias": "Single dict action",
                    "triggers": [],
                    # NB: dict, not list-of-dict
                    "actions": {"service": "scene.create", "data": {"scene_id": "x"}},
                },
                entity_id=None,
            )
        error = _error_from_tool_error(exc_info.value)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"

    def test_empty_trigger_with_other_action_passes(self) -> None:
        """Draft preservation: empty trigger paired with a non-scene.create
        action passes through unchanged. Some users save automations as
        drafts and add triggers later; this is not the misroute pattern."""
        AutomationConfigTools._check_input_guards(
            {
                "alias": "Draft",
                "triggers": [],
                "actions": [
                    {"action": "light.turn_on", "target": {"entity_id": "light.x"}}
                ],
            },
            entity_id=None,
        )

    def test_non_empty_trigger_with_scene_create_passes(self) -> None:
        """Legitimate use case: a trigger-driven scene snapshot (capture the
        current state when an event fires). Not the misroute pattern."""
        AutomationConfigTools._check_input_guards(
            {
                "alias": "Snapshot on guest mode",
                "triggers": [
                    {
                        "trigger": "state",
                        "entity_id": "input_boolean.guest_mode",
                        "to": "on",
                    }
                ],
                "actions": [
                    {
                        "service": "scene.create",
                        "data": {"scene_id": "before_guests"},
                    }
                ],
            },
            entity_id=None,
        )

    def test_use_blueprint_empty_lists_are_stripped(self) -> None:
        """Core merges a blueprint instance's keys over the rendered blueprint,
        so an empty list sent with ``use_blueprint`` would erase the
        blueprint's own triggers or actions. Either root form is stripped,
        and the misroute gate must not break that path."""
        config = {
            "alias": "Motion Light",
            "use_blueprint": {
                "path": "homeassistant/motion_light.yaml",
                "input": {
                    "motion_entity": "binary_sensor.motion",
                    "light_target": {"entity_id": "light.kitchen"},
                },
            },
            "triggers": [],
            "action": [],
        }
        AutomationConfigTools._check_input_guards(config, entity_id=None)

        assert "triggers" not in config
        assert "action" not in config

    def test_empty_trigger_scene_create_indices_in_context(self) -> None:
        """Multi-action lists surface the indices of the scene.create
        actions in the error context for the LLM to pinpoint."""
        with pytest.raises(ToolError) as exc_info:
            AutomationConfigTools._check_input_guards(
                {
                    "alias": "Multi",
                    "triggers": [],
                    "actions": [
                        {"service": "light.turn_on"},
                        {"service": "scene.create", "data": {"scene_id": "a"}},
                        {"action": "scene.create", "data": {"scene_id": "b"}},
                    ],
                },
                entity_id=None,
            )
        body = _body_from_tool_error(exc_info.value)
        assert body.get("scene_create_action_indices") == [1, 2]
