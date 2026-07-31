"""
Unit tests for the reactive best-practice checker.

Tests all 12 anti-pattern detection categories, clean config pass-through,
blueprint skipping, skill_prefix modes, false-positive rejection, and
recursive config structure traversal.
"""

from ha_mcp.tools.best_practice_checker import (
    check_automation_config,
    check_script_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SKILL_PREFIX = "skill://home-assistant-best-practices/references"
GITHUB_PREFIX = "https://github.com/homeassistant-ai/skills/blob/main/skills/home-assistant-best-practices/references"


def _has_warning_containing(warnings: list[str], *fragments: str) -> bool:
    """Return True if any warning contains ALL of the given fragments."""
    return any(all(f in w for f in fragments) for w in warnings)


# ---------------------------------------------------------------------------
# Clean configs — zero warnings
# ---------------------------------------------------------------------------


class TestCleanConfigs:
    """Verify zero overhead on clean configurations."""

    def test_clean_automation(self):
        config = {
            "trigger": [{"platform": "state", "entity_id": "light.bedroom"}],
            "condition": [
                {"condition": "state", "entity_id": "light.bedroom", "state": "on"}
            ],
            "action": [
                {"service": "light.turn_off", "target": {"entity_id": "light.bedroom"}}
            ],
        }
        assert check_automation_config(config) == []

    def test_clean_script(self):
        config = {
            "sequence": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": "light.living_room"},
                },
                {"delay": {"seconds": 2}},
                {
                    "service": "light.turn_off",
                    "target": {"entity_id": "light.living_room"},
                },
            ]
        }
        assert check_script_config(config) == []

    def test_empty_automation(self):
        assert check_automation_config({}) == []

    def test_empty_script(self):
        assert check_script_config({}) == []


# ---------------------------------------------------------------------------
# Blueprint skipping
# ---------------------------------------------------------------------------


class TestBlueprintSkipping:
    """Blueprint configs cannot be inspected — should return empty."""

    def test_automation_blueprint_skipped(self):
        config = {
            "use_blueprint": {"path": "motion_light.yaml", "input": {}},
            "trigger": [
                {
                    "platform": "template",
                    "value_template": "{{ states.sensor.x.state | float > 5 }}",
                }
            ],
        }
        assert check_automation_config(config) == []

    def test_script_blueprint_skipped(self):
        config = {
            "use_blueprint": {"path": "notification.yaml", "input": {}},
            "sequence": [{"wait_template": "{{ is_state('light.x', 'on') }}"}],
        }
        assert check_script_config(config) == []


# ---------------------------------------------------------------------------
# Condition anti-patterns
# ---------------------------------------------------------------------------


class TestConditionAntiPatterns:
    """Condition-level template anti-pattern detection."""

    def test_numeric_comparison_pipe_float(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states('sensor.temp') | float > 25 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(
            warnings, "float/int comparison", "numeric_state"
        )

    def test_numeric_comparison_int_pipe(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states('sensor.count') | int >= 10 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(
            warnings, "float/int comparison", "numeric_state"
        )

    def test_is_state_in_condition(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ is_state('light.bedroom', 'on') }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "is_state()", "state")

    def test_sun_entity_condition(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ is_state('sun.sun', 'below_horizon') }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "sun.sun", "sun")
        # Should NOT also flag generic is_state
        assert not _has_warning_containing(warnings, "is_state()", "state` condition")

    def test_now_hour_condition(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().hour >= 22 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "now().hour/minute", "time")

    def test_now_minute_condition(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().minute == 30 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "now().hour/minute", "time")

    def test_weekday_check_strftime(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().strftime('%A') == 'Monday' }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "day-of-week", "weekday")

    def test_weekday_check_weekday_method(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().weekday() == 0 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "day-of-week", "weekday")

    def test_states_in_list(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states('climate.living_room') in ['heat', 'cool'] }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "states(...) in [...]", "state")

    def test_direct_state_access(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states.sensor.temperature.state | float > 20 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(
            warnings, "states.domain.entity.state", "states('entity_id')"
        )

    def test_shorthand_template_condition(self):
        """Shorthand string conditions like '{{ is_state(...) }}' should be checked."""
        config = {
            "condition": ["{{ is_state('light.bedroom', 'on') }}"],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "is_state()", "state")

    def test_compound_and_condition(self):
        """Nested conditions inside and/or blocks should be recursed into."""
        config = {
            "condition": [
                {
                    "condition": "and",
                    "conditions": [
                        {
                            "condition": "template",
                            "value_template": "{{ is_state('light.x', 'on') }}",
                        }
                    ],
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "is_state()")


# ---------------------------------------------------------------------------
# Trigger anti-patterns
# ---------------------------------------------------------------------------


class TestTriggerAntiPatterns:
    """Trigger-level anti-pattern detection."""

    def test_device_trigger(self):
        config = {
            "trigger": [
                {"platform": "device", "device_id": "abc123", "type": "turned_on"}
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "device", "device_id", "entity_id")

    def test_template_trigger_numeric(self):
        config = {
            "trigger": [
                {
                    "platform": "template",
                    "value_template": "{{ states('sensor.temp') | float > 30 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(
            warnings, "Trigger", "float/int", "numeric_state"
        )

    def test_template_trigger_is_state(self):
        config = {
            "trigger": [
                {
                    "platform": "template",
                    "value_template": "{{ is_state('light.x', 'on') }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "Trigger", "is_state()", "state")

    def test_trigger_keyword_compat(self):
        """The 'trigger' key (instead of 'platform') should also be detected."""
        config = {
            "trigger": [{"trigger": "device", "device_id": "abc123"}],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "device", "device_id")


# ---------------------------------------------------------------------------
# Action anti-patterns
# ---------------------------------------------------------------------------


class TestActionAntiPatterns:
    """Action-level anti-pattern detection."""

    def test_wait_template(self):
        config = {
            "action": [{"wait_template": "{{ is_state('light.x', 'on') }}"}],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "wait_template", "wait_for_trigger")

    def test_wait_template_in_script(self):
        config = {
            "sequence": [{"wait_template": "{{ is_state('lock.front', 'locked') }}"}],
        }
        warnings = check_script_config(config)
        assert _has_warning_containing(warnings, "wait_template", "wait_for_trigger")

    def test_nested_condition_in_choose(self):
        """Anti-patterns inside choose option conditions should be detected."""
        config = {
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [
                                {
                                    "condition": "template",
                                    "value_template": "{{ states('sensor.x') | float > 5 }}",
                                }
                            ],
                            "sequence": [],
                        }
                    ],
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "float/int comparison")

    def test_nested_condition_in_if(self):
        """Anti-patterns inside if conditions should be detected."""
        config = {
            "action": [
                {
                    "if": [
                        {
                            "condition": "template",
                            "value_template": "{{ is_state('light.x', 'on') }}",
                        }
                    ],
                    "then": [],
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "is_state()")

    def test_nested_action_in_then_else(self):
        """wait_template inside then/else blocks should be detected."""
        config = {
            "action": [
                {
                    "if": [
                        {"condition": "state", "entity_id": "light.x", "state": "on"}
                    ],
                    "then": [{"wait_template": "{{ is_state('door.x', 'open') }}"}],
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "wait_template")

    def test_nested_repeat_while(self):
        """Anti-patterns in repeat while conditions should be detected."""
        config = {
            "action": [
                {
                    "repeat": {
                        "while": [
                            {
                                "condition": "template",
                                "value_template": "{{ states('sensor.x') | float > 0 }}",
                            }
                        ],
                        "sequence": [],
                    },
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "float/int comparison")

    def test_nested_repeat_until(self):
        """Anti-patterns in repeat until conditions should be detected."""
        config = {
            "action": [
                {
                    "repeat": {
                        "until": [
                            {
                                "condition": "template",
                                "value_template": "{{ now().hour >= 6 }}",
                            }
                        ],
                        "sequence": [],
                    },
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "now().hour/minute")


# ---------------------------------------------------------------------------
# Mode + motion pattern
# ---------------------------------------------------------------------------


class TestModeMotionPattern:
    """Detection of mode:single with motion trigger and delay/wait."""

    def test_motion_with_delay_default_mode(self):
        config = {
            "trigger": [
                {
                    "platform": "state",
                    "entity_id": "binary_sensor.hallway_motion",
                    "to": "on",
                }
            ],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.hallway"}},
                {"delay": {"minutes": 5}},
                {"service": "light.turn_off", "target": {"entity_id": "light.hallway"}},
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "motion", "mode: restart")

    def test_motion_with_explicit_restart_no_warning(self):
        config = {
            "mode": "restart",
            "trigger": [
                {
                    "platform": "state",
                    "entity_id": "binary_sensor.hallway_motion",
                    "to": "on",
                }
            ],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.hallway"}},
                {"delay": {"minutes": 5}},
            ],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(warnings, "motion", "mode: restart")

    def test_motion_without_delay_no_warning(self):
        config = {
            "trigger": [
                {
                    "platform": "state",
                    "entity_id": "binary_sensor.living_room_motion",
                    "to": "on",
                }
            ],
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": "light.living_room"},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(warnings, "motion")

    def test_non_motion_with_delay_no_warning(self):
        config = {
            "trigger": [
                {
                    "platform": "state",
                    "entity_id": "binary_sensor.door_contact",
                    "to": "on",
                }
            ],
            "action": [{"delay": {"minutes": 5}}],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(warnings, "motion")


# ---------------------------------------------------------------------------
# skill_prefix modes
# ---------------------------------------------------------------------------


_SKILL_PREFIX_TEST_CONFIG = {
    "condition": [
        {
            "condition": "template",
            "value_template": "{{ is_state('light.x', 'on') }}",
        }
    ],
    "action": [],
}


class TestSkillPrefixModes:
    """Verify warning output varies based on skill_prefix setting."""

    def test_default_skill_prefix(self):
        warnings = check_automation_config(_SKILL_PREFIX_TEST_CONFIG)
        assert any("skill://" in w for w in warnings)

    def test_custom_skill_prefix(self):
        warnings = check_automation_config(
            _SKILL_PREFIX_TEST_CONFIG, skill_prefix=GITHUB_PREFIX
        )
        assert any("github.com" in w for w in warnings)
        assert not any("skill://" in w for w in warnings)

    def test_no_skill_prefix(self):
        warnings = check_automation_config(_SKILL_PREFIX_TEST_CONFIG, skill_prefix=None)
        assert warnings  # Warnings still fire
        assert not any("skill://" in w for w in warnings)
        assert not any("See " in w for w in warnings)


# ---------------------------------------------------------------------------
# False-positive rejection
# ---------------------------------------------------------------------------


class TestFalsePositiveRejection:
    """Templates in service data (notification messages, etc.) should NOT be flagged."""

    def test_template_in_service_data_not_flagged(self):
        config = {
            "trigger": [{"platform": "state", "entity_id": "sensor.temp"}],
            "action": [
                {
                    "service": "notify.mobile_app",
                    "data": {
                        "message": "Temperature is {{ states('sensor.temp') | float }} degrees",
                    },
                }
            ],
        }
        warnings = check_automation_config(config)
        # The template is in service data, not in a condition/trigger template
        assert not _has_warning_containing(warnings, "float/int comparison")

    def test_template_in_condition_is_flagged(self):
        """Same template in a condition position SHOULD be flagged."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states('sensor.temp') | float > 25 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "float/int comparison")


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Same warning type should appear at most once per call."""

    def test_duplicate_warnings_deduped(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states('sensor.a') | float > 10 }}",
                },
                {
                    "condition": "template",
                    "value_template": "{{ states('sensor.b') | float > 20 }}",
                },
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        float_warnings = [w for w in warnings if "float/int comparison" in w]
        assert len(float_warnings) == 1


# ---------------------------------------------------------------------------
# Date-based condition detection (issue #1011, regex extension)
# ---------------------------------------------------------------------------


class TestDateBasedCondition:
    """Templates checking date components should suggest one-shot patterns."""

    def test_now_date_isoformat(self):
        """The exact pattern from issue #1011: now().date().isoformat() == 'YYYY-MM-DD'."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().date().isoformat() == '2026-04-19' }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "date")
        # Should suggest a native alternative inline
        assert (
            _has_warning_containing(warnings, "self-disable")
            or _has_warning_containing(warnings, "one-shot")
            or _has_warning_containing(warnings, "sensor.date")
        )

    def test_now_year(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().year == 2026 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "date")

    def test_now_month(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().month == 12 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "date")

    def test_now_day(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().day == 1 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "date")


# ---------------------------------------------------------------------------
# Target-field template detection (issue #1011)
# ---------------------------------------------------------------------------


class TestTargetTemplate:
    """Templates in target.entity_id / device_id / area_id / floor_id / label_id."""

    def test_this_entity_id_in_target(self):
        """The exact pattern from issue #1011: {{ this.entity_id }} in target."""
        config = {
            "trigger": [{"platform": "time", "at": "00:01:00"}],
            "action": [
                {
                    "service": "automation.turn_off",
                    "target": {"entity_id": "{{ this.entity_id }}"},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "target")
        # Inline guidance should suggest hardcoding
        assert _has_warning_containing(warnings, "hardcode") or _has_warning_containing(
            warnings, "literal"
        )

    def test_this_attributes_in_target(self):
        config = {
            "trigger": [{"platform": "time", "at": "00:01:00"}],
            "action": [
                {
                    "service": "automation.turn_off",
                    "target": {"entity_id": "{{ this.attributes.id }}"},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "target")

    def test_template_in_target_area_id(self):
        config = {
            "trigger": [{"platform": "state", "entity_id": "input_select.house_mode"}],
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {"area_id": "{{ states('input_select.house_mode') }}"},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "target")

    def test_target_list_with_template(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": ["light.kitchen", "{{ this.entity_id }}"]},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "target")

    def test_clean_literal_target_no_warning(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": "light.kitchen"},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(warnings, "target")


# ---------------------------------------------------------------------------
# Generic any-template-in-logic-position fallback
# ---------------------------------------------------------------------------


class TestGenericAnyTemplate:
    """Templates in logic positions with no specific detector should still warn."""

    def test_unknown_template_in_condition(self):
        """Arbitrary template logic that doesn't match the 12 specific detectors."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ (states('sensor.a') | length) % 2 == 0 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        # Should produce a generic warning since no specific pattern matched
        assert warnings
        assert any("template" in w.lower() for w in warnings)

    def test_unknown_template_in_trigger(self):
        config = {
            "trigger": [
                {
                    "platform": "template",
                    "value_template": "{{ (now() - states.sensor.x.last_updated).total_seconds() > 300 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert warnings
        assert any("trigger" in w.lower() or "template" in w.lower() for w in warnings)

    def test_specific_pattern_does_not_double_flag(self):
        """When a specific detector fires, generic should NOT also fire for same template."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states('sensor.temp') | float > 25 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        # The float-comparison warning fires
        float_warnings = [w for w in warnings if "float/int comparison" in w]
        assert len(float_warnings) == 1
        # No additional generic-template warning for the same condition
        generic_warnings = [
            w
            for w in warnings
            if "template detected" in w.lower() and "float/int" not in w
        ]
        assert len(generic_warnings) == 0


# ---------------------------------------------------------------------------
# Allowlist — legitimate template positions
# ---------------------------------------------------------------------------


class TestAllowlistLegitimatePositions:
    """Templates in service data, notification bodies, etc. must NOT be flagged."""

    def test_template_in_notification_message_clean(self):
        """Notification message templates are legitimate per template-guidelines.md."""
        config = {
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.door"}],
            "action": [
                {
                    "service": "notify.mobile_app",
                    "data": {
                        "message": "Door {{ trigger.to_state.attributes.friendly_name }} opened",
                        "title": "Alert: {{ now().strftime('%H:%M') }}",
                    },
                }
            ],
        }
        warnings = check_automation_config(config)
        assert warnings == []

    def test_template_in_brightness_data_clean(self):
        config = {
            "trigger": [
                {"platform": "state", "entity_id": "input_number.target_brightness"}
            ],
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": "light.x"},
                    "data": {
                        "brightness": "{{ states('input_number.target_brightness') | int }}"
                    },
                }
            ],
        }
        warnings = check_automation_config(config)
        assert warnings == []

    def test_template_in_event_data_clean(self):
        config = {
            "trigger": [{"platform": "state", "entity_id": "sensor.x"}],
            "action": [
                {
                    "event": "custom_event",
                    "event_data": {
                        "value": "{{ states('sensor.x') | float * 2 }}",
                    },
                }
            ],
        }
        warnings = check_automation_config(config)
        assert warnings == []

    def test_template_in_variables_clean(self):
        config = {
            "trigger": [{"platform": "state", "entity_id": "sensor.x"}],
            "variables": {"computed": "{{ states('sensor.x') | float + 10 }}"},
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.x"}}
            ],
        }
        warnings = check_automation_config(config)
        assert warnings == []


# ---------------------------------------------------------------------------
# Inline condition steps inside action sequences (pre-existing checker gap)
# ---------------------------------------------------------------------------


class TestInlineConditionSteps:
    """Condition-shorthand steps inside sequences/then/else were not inspected."""

    def test_template_condition_step_in_script_sequence(self):
        config = {
            "sequence": [
                {
                    "condition": "template",
                    "value_template": "{{ is_state('light.x', 'on') }}",
                },
                {"service": "light.turn_off", "target": {"entity_id": "light.x"}},
            ],
        }
        warnings = check_script_config(config)
        assert _has_warning_containing(warnings, "is_state()")

    def test_template_condition_step_in_automation_action(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "condition": "template",
                    "value_template": "{{ now().hour < 12 }}",
                },
                {"service": "light.turn_on", "target": {"entity_id": "light.x"}},
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "now().hour/minute")

    def test_compound_condition_step_in_sequence(self):
        config = {
            "sequence": [
                {
                    "condition": "and",
                    "conditions": [
                        {
                            "condition": "template",
                            "value_template": "{{ states('sensor.x') | float > 5 }}",
                        },
                    ],
                },
            ],
        }
        warnings = check_script_config(config)
        assert _has_warning_containing(warnings, "float/int comparison")


# ---------------------------------------------------------------------------
# Templates in action service dispatch
# ---------------------------------------------------------------------------


class TestActionServiceTemplate:
    """Templates in `service:` or `service_template:` are anti-patterns."""

    def test_service_template_field_flagged(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "service_template": "{{ 'light.turn_on' if is_state('x', 'on') else 'light.turn_off' }}",
                    "target": {"entity_id": "light.x"},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert any("service" in w.lower() for w in warnings)
        assert any(
            "choose" in w.lower() or "if/then" in w.lower() or "if-then" in w.lower()
            for w in warnings
        )

    def test_service_with_template_value_flagged(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "service": "{{ states('input_select.service') }}",
                    "target": {"entity_id": "light.x"},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert any("service" in w.lower() for w in warnings)

    def test_clean_literal_service_no_warning(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": "light.x"},
                    "data": {"brightness": "{{ states('input_number.b') | int }}"},
                }
            ],
        }
        warnings = check_automation_config(config)
        # No service-template warning; data templates are allowlisted
        assert not any(
            "service:" in w.lower() and "template" in w.lower() for w in warnings
        )


# ---------------------------------------------------------------------------
# Inline guidance — warnings should carry native alternative text inline
# ---------------------------------------------------------------------------


class TestInlineGuidance:
    """Each warning should carry a native alternative inline, not just a URI."""

    def test_numeric_warning_has_inline_alternative(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states('sensor.temp') | float > 25 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        # Inline guidance: should mention the native alternative concretely
        assert any(
            "above:" in w.lower() or "below:" in w.lower() or "numeric_state" in w
            for w in warnings
        )

    def test_is_state_warning_has_inline_alternative(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ is_state('light.x', 'on') }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert any(
            "condition: state" in w.lower()
            or "state condition" in w.lower()
            or "entity_id:" in w
            for w in warnings
        )


# ---------------------------------------------------------------------------
# Modern `action:` step key (HA 2024+ rename of `service:`)
# ---------------------------------------------------------------------------


class TestActionKeyStep:
    """HA accepts both `service:` and `action:` for the service-name field
    in an action step. Both must be checked for templated dispatch, and
    neither should be confused with an inline `condition:` step."""

    def test_action_key_with_template_flagged(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "action": "{{ states('input_select.service') }}",
                    "target": {"entity_id": "light.x"},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert any("action:" in w.lower() and "choose" in w.lower() for w in warnings)

    def test_action_key_clean_literal_no_warning(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "action": "light.turn_on",
                    "target": {"entity_id": "light.x"},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert not any(
            "template" in w.lower() and "service" in w.lower() for w in warnings
        )

    def test_action_key_step_with_legacy_condition_filter_not_double_flagged(self):
        """A service-call step with an `action:` key plus a legacy `condition:`
        run-if filter (a dict) must NOT be cross-checked as an inline
        condition step. The inline-condition-step branch should bail when any
        service-key is present, regardless of which key (service/action)."""
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "action": "light.turn_on",
                    "target": {"entity_id": "light.x"},
                    "condition": "state",  # legacy run-if filter shorthand
                }
            ],
        }
        # We don't expect a condition-related warning here because this is a
        # service-call step, not a standalone condition step.
        warnings = check_automation_config(config)
        # The action's "condition: state" string is a stub legacy filter,
        # not a templated condition — should produce no warnings.
        assert warnings == []


# ---------------------------------------------------------------------------
# `parallel:` action container
# ---------------------------------------------------------------------------


class TestParallelContainer:
    """`parallel:` runs sub-actions concurrently and must be walked the same
    as `sequence` so templates inside parallel branches are inspected."""

    def test_wait_template_inside_parallel(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "parallel": [
                        {"wait_template": "{{ is_state('door.x', 'open') }}"},
                        {
                            "service": "light.turn_on",
                            "target": {"entity_id": "light.x"},
                        },
                    ],
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "wait_template")

    def test_target_template_inside_parallel(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "parallel": [
                        {
                            "service": "automation.turn_off",
                            "target": {"entity_id": "{{ this.entity_id }}"},
                        },
                    ],
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "target")

    def test_inline_condition_step_inside_parallel(self):
        config = {
            "sequence": [
                {
                    "parallel": [
                        {
                            "condition": "template",
                            "value_template": "{{ is_state('light.x', 'on') }}",
                        },
                        {
                            "service": "light.turn_off",
                            "target": {"entity_id": "light.x"},
                        },
                    ],
                }
            ],
        }
        warnings = check_script_config(config)
        assert _has_warning_containing(warnings, "is_state()")


# ---------------------------------------------------------------------------
# value_template on non-template conditions (numeric_state etc.)
# ---------------------------------------------------------------------------


class TestNumericStateValueTemplate:
    """A `condition: numeric_state` block can carry a `value_template:` field
    that computes the numeric value being compared. That template was
    previously not scanned (only `condition: template` was)."""

    def test_value_template_on_numeric_state_flagged(self):
        config = {
            "condition": [
                {
                    "condition": "numeric_state",
                    "entity_id": "sensor.temp",
                    "above": 25,
                    "value_template": "{{ states('sensor.raw_temp') | float * 1.8 + 32 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        # The value_template contains float arithmetic and `> 0`-style
        # comparisons aren't required for the generic catch-all to fire.
        assert any("template" in w.lower() for w in warnings)

    def test_value_template_on_numeric_state_with_is_state_flagged(self):
        config = {
            "condition": [
                {
                    "condition": "numeric_state",
                    "entity_id": "sensor.x",
                    "above": 0,
                    "value_template": "{{ 1 if is_state('switch.x', 'on') else 0 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        # Specific is_state detector should fire on the value_template.
        assert _has_warning_containing(warnings, "is_state()")


# ---------------------------------------------------------------------------
# Negative tests for new specific detectors
# ---------------------------------------------------------------------------


class TestNewDetectorNegativeCases:
    """Look-alikes that should NOT trigger a specific detector. They may
    still fire the generic catch-all, but the SPECIFIC pattern's targeted
    message should not appear."""

    def test_now_day_of_week_does_not_match_now_date_pattern(self):
        """`now().day_of_week` is a real Jinja accessor on datetime; it must
        not collide with the now().day specific message."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().day_of_week == 0 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        # No "date-based check" specific message — falls through to generic
        assert not any("date-based check" in w for w in warnings)

    def test_now_day_method_call_does_not_match(self):
        """`now().day(` (with parens) is a method call shape that doesn't
        exist in HA's Jinja env — the negative lookahead in _RE_NOW_DATE
        should reject it."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().day() == 1 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert not any("date-based check" in w for w in warnings)

    def test_this_house_does_not_match_this_reference(self):
        """`this_house.entity_id` looks like `this.entity_id` but the `\\b`
        boundary in _RE_THIS_REFERENCE rejects it."""
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": "{{ this_house.entity_id }}"},
                }
            ],
        }
        warnings = check_automation_config(config)
        # Still fires a target-template warning (any template in target is
        # flagged), but NOT the `this.*` self-reference specific message.
        target_warnings = [w for w in warnings if "target" in w.lower()]
        assert target_warnings  # fired generic target warning
        assert not any("self-reference" in w for w in target_warnings)

    def test_service_data_does_not_match_service_template(self):
        """`service_data:` is a legacy alias for `data:` — has nothing to do
        with `service_template:`. Templates in service_data must not be
        flagged as templated service dispatch."""
        config = {
            "trigger": [{"platform": "state", "entity_id": "sensor.x"}],
            "action": [
                {
                    "service": "notify.mobile",
                    "service_data": {"message": "{{ states('sensor.x') | float }}"},
                }
            ],
        }
        warnings = check_automation_config(config)
        # service_data is allowlisted (treated like data)
        assert warnings == []


# ---------------------------------------------------------------------------
# Recursion through nested action containers for new detectors
# ---------------------------------------------------------------------------


class TestNewDetectorRecursion:
    """Every new detector hook (target, service template, inline condition
    step) must work the same when the action lives inside a nested choose,
    if/then/else, or repeat container."""

    def test_target_template_inside_choose(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [
                                {"condition": "state", "entity_id": "x", "state": "on"}
                            ],
                            "sequence": [
                                {
                                    "service": "automation.turn_off",
                                    "target": {"entity_id": "{{ this.entity_id }}"},
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "target")

    def test_service_template_inside_then(self):
        config = {
            "trigger": [{"platform": "time", "at": "08:00:00"}],
            "action": [
                {
                    "if": [{"condition": "state", "entity_id": "x", "state": "on"}],
                    "then": [
                        {
                            "service_template": "{{ 'a.b' if x else 'c.d' }}",
                        }
                    ],
                }
            ],
        }
        warnings = check_automation_config(config)
        assert any("service_template" in w.lower() for w in warnings)

    def test_inline_date_condition_step_inside_repeat(self):
        config = {
            "sequence": [
                {
                    "repeat": {
                        "while": [
                            {"condition": "state", "entity_id": "x", "state": "on"}
                        ],
                        "sequence": [
                            {
                                "condition": "template",
                                "value_template": "{{ now().date().isoformat() == '2026-01-01' }}",
                            },
                            {
                                "service": "light.turn_on",
                                "target": {"entity_id": "light.x"},
                            },
                        ],
                    },
                }
            ],
        }
        warnings = check_script_config(config)
        assert _has_warning_containing(warnings, "date-based check")


# ---------------------------------------------------------------------------
# Specific-pattern detectors don't double-flag with generic catch-all
# ---------------------------------------------------------------------------


class TestNoGenericDoubleFlag:
    """For each specific detector that fires, confirm no additional generic
    'Template detected in <position>' warning appears. The float case is
    already covered in TestGenericAnyTemplate; this class covers the rest."""

    def _assert_no_generic(self, warnings: list[str]) -> None:
        generic = [w for w in warnings if w.startswith("Template detected in")]
        assert not generic, f"Unexpected generic warning: {generic}"

    def test_is_state_no_generic(self):
        warnings = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ is_state('x', 'on') }}",
                    }
                ],
                "action": [],
            }
        )
        self._assert_no_generic(warnings)

    def test_sun_no_generic(self):
        warnings = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ is_state('sun.sun', 'below_horizon') }}",
                    }
                ],
                "action": [],
            }
        )
        self._assert_no_generic(warnings)

    def test_now_hour_no_generic(self):
        warnings = check_automation_config(
            {
                "condition": [
                    {"condition": "template", "value_template": "{{ now().hour > 9 }}"}
                ],
                "action": [],
            }
        )
        self._assert_no_generic(warnings)

    def test_weekday_no_generic(self):
        warnings = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ now().weekday() == 0 }}",
                    }
                ],
                "action": [],
            }
        )
        self._assert_no_generic(warnings)

    def test_now_date_no_generic(self):
        warnings = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ now().date().isoformat() == '2026-04-19' }}",
                    }
                ],
                "action": [],
            }
        )
        self._assert_no_generic(warnings)

    def test_states_in_no_generic(self):
        warnings = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ states('x') in ['a', 'b'] }}",
                    }
                ],
                "action": [],
            }
        )
        self._assert_no_generic(warnings)

    def test_direct_state_no_generic(self):
        warnings = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ states.sensor.x.state | float > 0 }}",
                    }
                ],
                "action": [],
            }
        )
        self._assert_no_generic(warnings)


# ---------------------------------------------------------------------------
# Allowlist: data.entity_id (some integrations use it)
# ---------------------------------------------------------------------------


class TestDataEntityIdAllowlist:
    """`data.entity_id` is used by some HA service calls (notify.notify with
    `data.entity_id` for camera attachments, etc.). Templates here are NOT
    in a logic position and must not be flagged."""

    def test_template_in_data_entity_id_not_flagged(self):
        config = {
            "trigger": [{"platform": "state", "entity_id": "sensor.x"}],
            "action": [
                {
                    "service": "notify.mobile_app",
                    "data": {"entity_id": "{{ trigger.entity_id }}"},
                }
            ],
        }
        warnings = check_automation_config(config)
        assert warnings == []


# ---------------------------------------------------------------------------
# skill_prefix=None mode for the new detector categories
# ---------------------------------------------------------------------------


class TestSkillPrefixNoneNewDetectors:
    """Every new detector must respect `skill_prefix=None` (warnings still
    fire, but without `See skill://...` suffix). Locks the contract so a
    careless future detector author who forgets `+ _ref(...)` breaks it
    loudly in tests."""

    @staticmethod
    def _assert_clean(warnings: list[str]) -> None:
        assert warnings, "Expected a warning"
        for w in warnings:
            assert "skill://" not in w
            assert " See " not in w

    def test_date_detector(self):
        warnings = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ now().date() == today }}",
                    }
                ],
                "action": [],
            },
            skill_prefix=None,
        )
        self._assert_clean(warnings)

    def test_target_self_reference(self):
        warnings = check_automation_config(
            {
                "trigger": [{"platform": "time", "at": "08:00:00"}],
                "action": [
                    {
                        "service": "automation.turn_off",
                        "target": {"entity_id": "{{ this.entity_id }}"},
                    }
                ],
            },
            skill_prefix=None,
        )
        self._assert_clean(warnings)

    def test_service_template(self):
        warnings = check_automation_config(
            {
                "trigger": [{"platform": "time", "at": "08:00:00"}],
                "action": [{"service_template": "{{ x }}"}],
            },
            skill_prefix=None,
        )
        self._assert_clean(warnings)

    def test_generic_catchall(self):
        warnings = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ (states('sensor.a') | length) % 2 == 0 }}",
                    }
                ],
                "action": [],
            },
            skill_prefix=None,
        )
        self._assert_clean(warnings)


# ---------------------------------------------------------------------------
# BestPracticeCheckResult shape + 2-route warning text (issue #1182)
# ---------------------------------------------------------------------------


class TestBestPracticeCheckResultShape:
    """The return value behaves as a list[str] AND exposes referenced_files."""

    def test_result_is_list_subclass(self):
        """BestPracticeCheckResult IS a list[str] — existing callers don't break."""
        from ha_mcp.tools.best_practice_checker import BestPracticeCheckResult

        result = check_automation_config({})
        assert isinstance(result, BestPracticeCheckResult)
        assert isinstance(result, list)

    def test_clean_config_returns_empty_list_and_empty_set(self):
        result = check_automation_config(
            {
                "trigger": [],
                "condition": [],
                "action": [],
            }
        )
        assert result == []  # list semantics
        assert result.referenced_files == set()

    def test_referenced_files_preserves_anchor(self):
        """When a warning fires, the referenced skill file (with #anchor) is tracked.

        The anchor is what makes the auto-embed path ship only the
        relevant markdown section instead of the whole 20 KB file.
        """
        result = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ states('sensor.x') | float > 25 }}",
                    }
                ],
                "action": [],
            }
        )
        assert (
            "references/automation-patterns.md#native-conditions"
            in result.referenced_files
        )

    def test_referenced_files_dedup_same_anchor_across_emissions(self):
        """Repeated warnings hitting the same anchor collapse to one set entry."""
        result = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ states('sensor.a') | float > 10 }}",
                    },
                    {
                        "condition": "template",
                        "value_template": "{{ states('sensor.b') | float > 20 }}",
                    },
                ],
                "action": [],
            }
        )
        assert result.referenced_files == {
            "references/automation-patterns.md#native-conditions"
        }

    def test_referenced_files_tracked_even_when_skill_prefix_none(self):
        """referenced_files is populated even when skill_prefix=None.

        Skills feature being off doesn't change which files would be
        relevant — the caller decides whether to attempt resolution.
        """
        result = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ states('sensor.x') | float > 25 }}",
                    }
                ],
                "action": [],
            },
            skill_prefix=None,
        )
        assert result.referenced_files == {
            "references/automation-patterns.md#native-conditions"
        }


class TestTwoRouteWarningSuffix:
    """Each warning names the LLM-discoverable access routes for the
    referenced skill file (issue #1182). The skill:// URI works in clients
    that auto-fetch resources; ha_get_skill_guide works everywhere else.
    The MandatoryBPS parameter is intentionally NOT mentioned in the
    warning suffix — the param ships visible-but-undescribed, and naming
    it here would prime models to flip it. The opt-out hint shipped
    alongside delivered skill_content is the only place the param is
    described."""

    @staticmethod
    def _first_warning(prefix=None):
        from ha_mcp.tools.best_practice_checker import (
            _DEFAULT_SKILL_PREFIX,
            check_automation_config,
        )

        skill_prefix = prefix if prefix is not None else _DEFAULT_SKILL_PREFIX
        result = check_automation_config(
            {
                "condition": [
                    {
                        "condition": "template",
                        "value_template": "{{ states('sensor.x') | float > 25 }}",
                    }
                ],
                "action": [],
            },
            skill_prefix=skill_prefix,
        )
        assert result, "expected at least one warning"
        return result[0]

    def test_default_warning_names_skill_uri_route(self):
        msg = self._first_warning()
        assert (
            "skill://home-assistant-best-practices/references/automation-patterns.md"
            in msg
        )

    def test_default_warning_names_ha_get_skill_guide_route(self):
        msg = self._first_warning()
        assert "ha_get_skill_guide(skill='home-assistant-best-practices'" in msg
        assert "file='references/automation-patterns.md'" in msg

    def test_warning_does_not_mention_MandatoryBPS_param(self):
        """MandatoryBPS must not appear in warnings. The param is visible
        in the catalog but undescribed; naming it in warnings would
        re-prime the reflex-disable that BAT showed kills the feature
        for smart models."""
        msg = self._first_warning()
        assert "MandatoryBPS" not in msg

    def test_custom_prefix_replaces_skill_uri_keeps_tool_route(self):
        msg = self._first_warning(prefix="https://example.com/refs")
        assert "https://example.com/refs/automation-patterns.md" in msg
        assert "skill://" not in msg
        # Tool route always present when skills are on
        assert "ha_get_skill_guide" in msg
        # MandatoryBPS not advertised in the warning suffix —
        # only in the opt-out hint shipped with delivered content.
        assert "MandatoryBPS" not in msg

    def test_anchor_preserved_in_uri_stripped_in_tool_route(self):
        """The skill:// URI keeps the #anchor (links scroll to section);
        the ha_get_skill_guide call uses bare file path (tool reads the whole file)."""
        msg = self._first_warning()
        # URI: anchor preserved
        assert "automation-patterns.md#native-conditions" in msg
        # Tool route: bare file path, no anchor
        assert "file='references/automation-patterns.md'" in msg


# ---------------------------------------------------------------------------
# Duration math / for: field detector
# ---------------------------------------------------------------------------


class TestDurationMathDetector:
    """Detects duration/recency math on ``last_changed``/``last_updated`` and suggests ``for:``.

    Covers every shape ``_RE_DURATION_MATH`` matches — forward (``now() - X.last_changed``),
    reversed-with-subtraction (``X.last_changed < now() - <delta>`` and the ``>`` variants),
    ``now()`` on the left, and ``as_timestamp(...)`` arithmetic — plus the deliberate
    non-matches: bare Jinja variables, ``state_attr(...)`` strings, and the always-true bare
    ``X.last_changed < now()`` (no duration → cannot map to ``for:``).
    """

    def test_condition_last_changed_math(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now() - states.binary_sensor.motion.last_changed > timedelta(minutes=5) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:")

    def test_condition_last_updated_math(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now() - states.sensor.temp.last_updated > timedelta(minutes=10) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:")

    def test_trigger_template_last_changed_math(self):
        """Template trigger using ``trigger.last_changed`` (single dotted qualifier) is flagged."""
        config = {
            "trigger": [
                {
                    "platform": "template",
                    "value_template": "{{ now() - trigger.last_changed > timedelta(seconds=30) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:")

    def test_state_trigger_with_for_field_no_warning(self):
        config = {
            "trigger": [
                {
                    "platform": "state",
                    "entity_id": "binary_sensor.motion",
                    "to": "off",
                    "for": {"minutes": 5},
                }
            ],
            "action": [
                {"service": "light.turn_off", "target": {"entity_id": "light.hall"}}
            ],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(warnings, "last_changed", "for:")

    def test_warning_contains_skill_ref(self):
        """Condition-path warning links to ``#native-conditions`` (cf. ``#trigger-types`` for triggers)."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now() - states.sensor.x.last_changed > timedelta(hours=1) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config, skill_prefix=SKILL_PREFIX)
        assert _has_warning_containing(
            warnings, "automation-patterns.md#native-conditions"
        )

    def test_no_generic_fallback_when_duration_fires(self):
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now() - states.binary_sensor.door.last_changed > timedelta(minutes=1) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        generic_warnings = [
            w for w in warnings if "if this maps to a native option" in w
        ]
        assert not generic_warnings, (
            "Generic fallback should not fire alongside specific detector"
        )

    def test_trigger_warning_uses_trigger_types_anchor(self):
        """Trigger-specific warning links to trigger-types, not native-conditions."""
        config = {
            "trigger": [
                {
                    "platform": "template",
                    "value_template": "{{ (now() - states.sensor.x.last_updated).total_seconds() > 60 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config, skill_prefix=SKILL_PREFIX)
        assert _has_warning_containing(
            warnings, "automation-patterns.md#trigger-types"
        ), "Trigger warning should reference #trigger-types anchor"

    def test_no_false_positive_bare_last_changed_variable(self):
        """A Jinja variable literally named ``last_changed`` must not trigger the detector."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    # Bare variable — not an entity attribute; should not match.
                    "value_template": "{{ last_changed < now() }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(
            warnings, "last_changed/last_updated", "for:"
        ), (
            "Bare Jinja variable 'last_changed' should not be mistaken for an entity attribute"
        )

    def test_numeric_state_trigger_value_template_duration_math(self):
        """numeric_state trigger value_template containing duration math is also flagged."""
        config = {
            "trigger": [
                {
                    "platform": "numeric_state",
                    "entity_id": "sensor.motion",
                    "value_template": "{{ (now() - states.sensor.motion.last_changed).total_seconds() }}",
                    "above": 300,
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:"), (
            "Duration math inside numeric_state value_template should be flagged"
        )

    def test_condition_reversed_comparison_last_changed_math(self):
        """Reversed form ``X.last_changed < now() - <delta>`` in a condition is flagged.

        The reversed alternation catches the comparison written the other way
        round, but it requires the ``- <delta>`` subtraction: that is what makes
        it a genuine recency check (``last_changed < now() - 5min`` ⇒ "changed
        more than 5 minutes ago"). Asserting exactly one warning also guards
        against the alternations double-flagging the same template. The bare
        ``X.last_changed < now()`` form is covered separately in
        ``test_bare_reversed_comparison_not_flagged`` — it is always true and
        intentionally NOT flagged.
        """
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states.binary_sensor.motion.last_changed < now() - timedelta(minutes=5) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        duration_warnings = [
            w for w in warnings if "last_changed/last_updated" in w and "for:" in w
        ]
        assert len(duration_warnings) == 1, (
            "Reversed-form duration math in a condition should fire exactly one warning"
        )

    def test_bare_reversed_comparison_not_flagged(self):
        """Bare ``X.last_changed < now()`` (no subtraction) must NOT be flagged.

        Without a ``- <delta>`` there is no duration threshold: an entity's
        ``last_changed`` is by definition in the past, so the comparison is
        always true and cannot be expressed with a native ``for:`` field.
        Suggesting ``for:`` here would be incorrect advice, so the reversed
        alternation deliberately requires the subtraction. (This corrects the
        original #1264 behaviour, which flagged the bare form.)
        """
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states.binary_sensor.motion.last_changed < now() }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(
            warnings, "last_changed/last_updated", "for:"
        ), (
            "Always-true bare `X.last_changed < now()` carries no duration and must "
            "not get the `for:` suggestion"
        )
        # It is still a template in a logic position, so the generic fallback
        # fires — confirming we drop only the (wrong) duration hint, not all output.
        assert _has_warning_containing(warnings, "Template detected in condition"), (
            "bare reversed form should still surface the generic template warning"
        )

    def test_trigger_reversed_comparison_last_updated_math(self):
        """Reversed form ``X.last_updated < now() - ...`` in a trigger value_template is flagged."""
        config = {
            "trigger": [
                {
                    "platform": "template",
                    "value_template": "{{ states.sensor.temp.last_updated < now() - timedelta(minutes=5) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        duration_warnings = [
            w for w in warnings if "last_changed/last_updated" in w and "for:" in w
        ]
        assert len(duration_warnings) == 1, (
            "Reversed-form duration math in a trigger value_template should fire exactly one warning"
        )

    def test_duration_math_suppresses_numeric_state_suggestion(self):
        """Duration math with a numeric compare fires only the ``for:`` warning, not ``numeric_state``.

        ``(now() - X.last_changed).total_seconds() | int > 300`` trips both the
        numeric-comparison detector and the duration detector. The native fix is
        ``for:`` (you cannot express "seconds since last_changed" as a
        ``numeric_state``), so the conflicting ``numeric_state`` suggestion must
        be suppressed — exactly one duration warning, and no float/int warning.
        """
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ (now() - states.sensor.x.last_changed).total_seconds() | int > 300 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        duration_warnings = [
            w for w in warnings if "last_changed/last_updated" in w and "for:" in w
        ]
        assert len(duration_warnings) == 1, "Should fire exactly one duration warning"
        assert not _has_warning_containing(warnings, "float/int comparison"), (
            "numeric_state suggestion must be suppressed when duration math is present"
        )

    def test_trigger_duration_math_suppresses_numeric_state_suggestion(self):
        """Same numeric_state suppression applies on the template-trigger path."""
        config = {
            "trigger": [
                {
                    "platform": "template",
                    "value_template": "{{ (now() - states.sensor.x.last_changed).total_seconds() | int > 300 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        duration_warnings = [
            w for w in warnings if "last_changed/last_updated" in w and "for:" in w
        ]
        assert len(duration_warnings) == 1, "Should fire exactly one duration warning"
        assert not _has_warning_containing(warnings, "float/int comparison"), (
            "numeric_state suggestion must be suppressed when duration math is present"
        )

    def test_as_timestamp_recency_condition(self):
        """``as_timestamp(now()) - as_timestamp(X.last_changed)`` recency math is flagged."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ as_timestamp(now()) - as_timestamp(states.sensor.x.last_changed) > 300 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:"), (
            "as_timestamp() recency form should be flagged"
        )

    def test_as_timestamp_recency_numeric_state_trigger(self):
        """as_timestamp recency math inside a numeric_state value_template is flagged (no silent gap)."""
        config = {
            "trigger": [
                {
                    "platform": "numeric_state",
                    "entity_id": "sensor.x",
                    "value_template": "{{ as_timestamp(now()) - as_timestamp(states.sensor.x.last_changed) }}",
                    "above": 300,
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:"), (
            "as_timestamp() duration math in a numeric_state value_template should be flagged, "
            "not silently passed"
        )

    def test_greater_than_reversed_recency(self):
        """``X.last_changed > now() - <delta>`` (changed within window) recency math is flagged."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states.sensor.x.last_changed > now() - timedelta(minutes=5) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:")

    def test_now_on_left_recency(self):
        """``now() > X.last_changed + <delta>`` recency math is flagged."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now() > states.sensor.x.last_changed + timedelta(minutes=5) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:")

    def test_state_attr_last_changed_not_flagged(self):
        """``state_attr('x', 'last_changed')`` (quoted attr string) must NOT be flagged.

        The dotted-qualifier requirement means a ``last_changed`` passed as a
        string argument to ``state_attr`` is not mistaken for an entity-attribute
        access. Locks in the most likely future false positive.
        """
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now() - state_attr('sensor.x', 'last_changed') > timedelta(minutes=5) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(
            warnings, "last_changed/last_updated", "for:"
        ), "state_attr('x','last_changed') should not trigger the duration detector"

    def test_comparator_variants_ge_le_flagged(self):
        """``>=`` / ``<=`` recency comparisons are flagged like their strict ``>``/``<`` twins."""
        for vt in (
            "{{ states.sensor.x.last_changed <= now() - timedelta(minutes=5) }}",
            "{{ states.sensor.x.last_changed >= now() - timedelta(minutes=5) }}",
            "{{ now() >= states.sensor.x.last_changed + timedelta(minutes=5) }}",
        ):
            config = {
                "condition": [{"condition": "template", "value_template": vt}],
                "action": [],
            }
            warnings = check_automation_config(config)
            assert _has_warning_containing(
                warnings, "last_changed/last_updated", "for:"
            ), f"{vt!r} should be flagged"

    def test_timestamp_method_epoch_subtraction_flagged(self):
        """``now().timestamp() - X.last_updated.timestamp()`` epoch recency math is flagged."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ now().timestamp() - states.sensor.x.last_updated.timestamp() > 300 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:")

    def test_as_timestamp_last_updated_flagged(self):
        """as_timestamp recency math covers ``last_updated`` too, not just ``last_changed``."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ as_timestamp(now()) - as_timestamp(states.sensor.x.last_updated) > 60 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:")

    def test_suffixed_attribute_name_not_flagged(self):
        """Look-alike attributes (``last_changed_at``) must NOT match any alternation.

        The trailing word boundary on every alternation — including the
        ``as_timestamp`` path, which previously lacked it — keeps custom
        attributes whose names merely start with ``last_changed``/``last_updated``
        from being mistaken for the real state property.
        """
        for vt in (
            "{{ now() - states.sensor.x.last_changed_at > timedelta(minutes=5) }}",
            "{{ as_timestamp(now()) - as_timestamp(states.sensor.x.last_changed_at) }}",
            "{{ states.sensor.x.last_updated_ts < now() - timedelta(minutes=5) }}",
        ):
            config = {
                "condition": [{"condition": "template", "value_template": vt}],
                "action": [],
            }
            warnings = check_automation_config(config)
            assert not _has_warning_containing(
                warnings, "last_changed/last_updated", "for:"
            ), f"{vt!r} (look-alike attribute) must not be flagged as duration math"

    def test_trigger_path_now_on_left_recency_flagged(self):
        """The ``now() > X.last_changed + <delta>`` form also flows through the trigger path."""
        config = {
            "trigger": [
                {
                    "platform": "template",
                    "value_template": "{{ now() > states.sensor.x.last_changed + timedelta(minutes=5) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:")

    def test_attribute_plus_delta_compared_to_now_flagged(self):
        """``X.last_changed + <delta> (<|<=|>|>=) now()`` (delta on the attribute) is flagged.

        Mirror of the now()-on-the-left form. A no-comparison ``X.last_changed +
        <delta>`` (just computing a future time) is NOT a recency check and stays
        unflagged — the comparator-then-now() tail is required.
        """
        for vt in (
            "{{ states.binary_sensor.motion.last_changed + timedelta(minutes=5) < now() }}",
            "{{ states.sensor.x.last_updated + timedelta(hours=1) <= now() }}",
        ):
            config = {
                "condition": [{"condition": "template", "value_template": vt}],
                "action": [],
            }
            warnings = check_automation_config(config)
            assert _has_warning_containing(
                warnings, "last_changed/last_updated", "for:"
            ), f"{vt!r} should be flagged"

    def test_attribute_plus_delta_no_comparison_not_flagged(self):
        """Bare ``X.last_changed + <delta>`` (computing a time, no comparison) is NOT flagged."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ states.sensor.x.last_changed + timedelta(minutes=5) }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(
            warnings, "last_changed/last_updated", "for:"
        ), "computing a future time (no comparison to now()) is not a recency check"

    def test_as_timestamp_filter_syntax_flagged(self):
        """``as_timestamp(now()) - X.last_changed | as_timestamp`` (filter syntax) is flagged."""
        config = {
            "condition": [
                {
                    "condition": "template",
                    "value_template": "{{ as_timestamp(now()) - states.sensor.x.last_changed | as_timestamp > 300 }}",
                }
            ],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "last_changed/last_updated", "for:")


class TestPluralKeyTolerance:
    """check_automation_config reads HA 2024.10+ canonical plural root keys.

    In production the config is pre-normalized to plural before this checker runs,
    so the plural-first read (config.get("triggers", config.get("trigger", [])) etc.)
    is the live path. These pin that contract — the rest of the suite only feeds the
    singular fallback.
    """

    def test_condition_template_warning_on_plural_conditions(self):
        """The condition-template anti-pattern fires when conditions are under the
        canonical plural 'conditions' key (covers the plural-first condition read)."""
        config = {
            "triggers": [],
            "conditions": [
                {
                    "condition": "template",
                    "value_template": "{{ states('sensor.temp') | float > 25 }}",
                }
            ],
            "actions": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(
            warnings, "float/int comparison", "numeric_state"
        )

    def test_mode_motion_warning_on_plural_keys(self):
        """The mode:single motion+delay warning fires on plural 'triggers'/'actions'
        (covers the plural-first trigger and action reads in _check_mode_motion)."""
        config = {
            "triggers": [
                {
                    "trigger": "state",
                    "entity_id": "binary_sensor.hallway_motion",
                    "to": "on",
                }
            ],
            "actions": [
                {"action": "light.turn_on", "target": {"entity_id": "light.hallway"}},
                {"delay": {"minutes": 5}},
                {"action": "light.turn_off", "target": {"entity_id": "light.hallway"}},
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "motion", "mode: restart")


# ---------------------------------------------------------------------------
# HA 2026.7 renamed purpose-specific keys + trigger behavior values
# ---------------------------------------------------------------------------


class TestRenamedPurposeSpecificKeys:
    """2026.7 renamed trigger/condition keys and options.behavior values."""

    def test_renamed_trigger_key_warns(self):
        config = {
            "triggers": [
                {"trigger": "vacuum.docked", "target": {"entity_id": "vacuum.robo"}}
            ],
            "actions": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(
            warnings, "vacuum.docked", "vacuum.returned_to_dock", "2026.7"
        )

    def test_all_renamed_trigger_keys_warn(self):
        from ha_mcp.tools.best_practice_checker import _RENAMED_TRIGGER_KEYS

        for old, new in _RENAMED_TRIGGER_KEYS.items():
            config = {"triggers": [{"trigger": old}], "actions": []}
            warnings = check_automation_config(config)
            assert _has_warning_containing(warnings, f"`{old}`", f"`{new}`"), old

    def test_new_trigger_key_clean(self):
        config = {
            "triggers": [
                {
                    "trigger": "vacuum.returned_to_dock",
                    "target": {"entity_id": "vacuum.robo"},
                }
            ],
            "actions": [],
        }
        assert check_automation_config(config) == []

    def test_renamed_condition_key_warns(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "climate.x"}],
            "conditions": [
                {
                    "condition": "climate.target_temperature",
                    "target": {"entity_id": "climate.x"},
                }
            ],
            "actions": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(
            warnings, "climate.target_temperature", "climate.is_target_temperature"
        )

    def test_new_condition_key_clean(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "climate.x"}],
            "conditions": [
                {
                    "condition": "climate.is_target_temperature",
                    "target": {"entity_id": "climate.x"},
                }
            ],
            "actions": [],
        }
        assert check_automation_config(config) == []

    def test_renamed_condition_key_in_action_tree_warns(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "light.x"}],
            "actions": [{"if": [{"condition": "climate.target_humidity"}], "then": []}],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "climate.is_target_humidity")

    def test_deprecated_trigger_behavior_warns(self):
        for old, new in (("any", "each"), ("last", "all")):
            config = {
                "triggers": [
                    {
                        "trigger": "motion.detected",
                        "target": {"area_id": "living_room"},
                        "options": {"behavior": old},
                    }
                ],
                "actions": [],
            }
            warnings = check_automation_config(config)
            assert _has_warning_containing(
                warnings, f"behavior: {old}", f"`{new}`", "2026.7"
            ), old

    def test_current_trigger_behavior_clean(self):
        for value in ("each", "first", "all"):
            config = {
                "triggers": [
                    {
                        "trigger": "motion.detected",
                        "target": {"area_id": "living_room"},
                        "options": {"behavior": value},
                    }
                ],
                "actions": [],
            }
            assert check_automation_config(config) == [], value

    def test_condition_behavior_any_not_flagged(self):
        # Conditions keep any/all — only trigger options.behavior was renamed.
        config = {
            "triggers": [{"trigger": "state", "entity_id": "light.x"}],
            "conditions": [
                {
                    "condition": "battery.is_low",
                    "target": {"label_id": "critical_devices"},
                    "options": {"behavior": "any"},
                }
            ],
            "actions": [],
        }
        assert check_automation_config(config) == []


# ---------------------------------------------------------------------------
# Variables-block key ordering (issue #2072)
# ---------------------------------------------------------------------------


class TestVariablesForwardReference:
    """A variables key reading a LATER sibling renders undefined, silently."""

    def test_action_variables_forward_reference_flagged(self):
        # The reporter's shape: `meldung` sorted to the front of the block, so
        # it renders against siblings that do not exist yet.
        config = {
            "triggers": [{"trigger": "state", "entity_id": "binary_sensor.door"}],
            "actions": [
                {
                    "variables": {
                        "meldung": "{% if offene_tueren %}open{% endif %}",
                        "offene_tueren": "{{ expand('binary_sensor.doors') | count }}",
                    }
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "`meldung`", "`offene_tueren`")

    def test_names_every_forward_sibling(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [
                {
                    "variables": {
                        "summary": "{{ a }}{{ b }}",
                        "a": "1",
                        "b": "2",
                    }
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "`summary`", "`a`", "`b`")

    def test_backward_reference_clean(self):
        # Correct order — each key reads only what precedes it.
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [
                {
                    "variables": {
                        "offene_tueren": "{{ expand('binary_sensor.doors') | count }}",
                        # Backward read, and deliberately not the last key — a
                        # trailing sibling exists for it to wrongly match on.
                        "meldung": "{% if offene_tueren %}open{% endif %}",
                        "tail": "1",
                    }
                }
            ],
        }
        assert check_automation_config(config) == []

    def test_nested_in_choose_sequence_flagged(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [
                {
                    "choose": [
                        {
                            "conditions": [],
                            "sequence": [
                                {"variables": {"first": "{{ second }}", "second": "2"}}
                            ],
                        }
                    ]
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "`first`", "`second`")

    def test_nested_in_canonical_parallel_branch_flagged(self):
        # HA normalises a shorthand `parallel:` branch list into
        # `{"sequence": [...]}`, so the canonical branch shape has to be walked.
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [
                {
                    "parallel": [
                        {
                            "sequence": [
                                {"variables": {"first": "{{ second }}", "second": "2"}}
                            ]
                        }
                    ]
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "`first`", "`second`")

    def test_nested_in_bare_sequence_action_flagged(self):
        config = {
            "sequence": [
                {"sequence": [{"variables": {"first": "{{ second }}", "second": "2"}}]}
            ]
        }
        warnings = check_script_config(config)
        assert _has_warning_containing(warnings, "`first`", "`second`")

    def test_nested_value_structure_scanned(self):
        # render_complex recurses into lists and mappings, so a template buried
        # in one still renders in that key's slot.
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [
                {
                    "variables": {
                        "payload": {"items": ["{{ threshold }}"]},
                        "threshold": "5",
                    }
                }
            ],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "`payload`", "`threshold`")


class TestVariablesForwardReferenceNegatives:
    """Shapes that look like a forward reference but are legal."""

    def test_earlier_response_variable_clean(self):
        # `wetter` comes from a preceding action, not from the block.
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [
                {"action": "weather.get_forecasts", "response_variable": "wetter"},
                {
                    "variables": {
                        "stunden": "{{ wetter['weather.home'].forecast[:3] }}",
                        "regen": "{{ 'ja' if stunden else '' }}",
                    }
                },
            ],
        }
        assert check_automation_config(config) == []

    def test_longer_name_is_not_a_reference(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [
                {
                    "variables": {
                        "first": "{{ offene_tueren_extra }}",
                        "offene_tueren": "2",
                    }
                }
            ],
        }
        assert check_automation_config(config) == []

    def test_string_literal_is_not_a_reference(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [
                {
                    "variables": {
                        "first": "{{ payload['threshold'] }}",
                        "threshold": "5",
                    }
                }
            ],
        }
        assert check_automation_config(config) == []

    def test_attribute_access_is_not_a_reference(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [
                {"variables": {"first": "{{ wetter.forecast }}", "forecast": "5"}}
            ],
        }
        assert check_automation_config(config) == []

    def test_numeric_literal_tail_is_not_a_reference(self):
        # `e3` inside the float literal `2.5e3` starts a would-be identifier
        # match; the digit in front of it is what rejects it.
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [{"variables": {"first": "{{ 2.5e3 }}", "e3": "5"}}],
        }
        assert check_automation_config(config) == []

    def test_filter_name_is_not_a_reference(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [{"variables": {"first": "{{ [3, 1] | max }}", "max": "5"}}],
        }
        assert check_automation_config(config) == []

    def test_jinja_set_local_is_not_a_reference(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [
                {
                    "variables": {
                        "first": "{% set total = 1 %}{{ total }}",
                        "total": "5",
                    }
                }
            ],
        }
        assert check_automation_config(config) == []

    def test_plain_string_value_is_not_a_reference(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [{"variables": {"first": "threshold", "threshold": "5"}}],
        }
        assert check_automation_config(config) == []

    def test_single_key_block_clean(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [{"variables": {"only": "{{ only }}"}}],
        }
        assert check_automation_config(config) == []


class TestVariablesForwardReferenceTokenising:
    """Shapes where the token scan has to be positional or Unicode-aware."""

    @staticmethod
    def _auto(variables):
        return check_automation_config(
            {
                "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
                "actions": [{"variables": variables}],
            }
        )

    def test_read_before_local_set_still_flagged(self):
        # The read happens before the binding, so the `{% set %}` must not
        # retroactively shadow it.
        warnings = self._auto({"first": "{{ later }}{% set later = 1 %}", "later": "1"})
        assert _has_warning_containing(warnings, "`first`", "`later`")

    def test_set_right_hand_side_still_flagged(self):
        # Self-referential on purpose: the RHS `later` is read before the
        # binding exists, so it is the sibling, not the local.
        warnings = self._auto(
            {"first": "{% set later = later %}{{ later }}", "later": "1"}
        )
        assert _has_warning_containing(warnings, "`first`", "`later`")

    def test_set_target_is_not_itself_a_read(self):
        # The target name is on the left of the `=`; only the RHS is scanned,
        # so a `{% set %}` naming a later sibling is not a reference to it.
        assert self._auto({"first": "{% set later = 1 %}ok", "later": "1"}) == []

    def test_literal_spanning_an_escaped_newline_stays_closed(self):
        # The sibling name sits *inside* the literal; if the escaped newline
        # ends the literal early the name is scanned as code.
        config = {"first": "{{ 'trail\\\nthreshold' ~ other }}", "threshold": "1"}
        assert self._auto(config) == []

    def test_escaped_quote_keeps_literal_closed(self):
        # A backslash escape has to be consumed with the literal, otherwise its
        # tail is scanned as code and `threshold` reads as a reference.
        config = {"first": r"{{ 'don\'t use threshold' }}", "threshold": "1"}
        assert self._auto(config) == []

    def test_non_ascii_identifier_flagged(self):
        warnings = self._auto({"first": "{{ über }}", "über": "1"})
        assert _has_warning_containing(warnings, "`first`", "`über`")

    def test_non_latin_identifier_flagged(self):
        warnings = self._auto({"first": "{{ 变量 }}", "变量": "1"})
        assert _has_warning_containing(warnings, "`first`", "`变量`")


class TestSingletonActionMappings:
    """`SCRIPT_SCHEMA` is `ensure_list`, so a lone action mapping is valid."""

    @staticmethod
    def _step():
        return {"variables": {"first": "{{ second }}", "second": "2"}}

    def test_nested_sequence_mapping(self):
        warnings = check_script_config({"sequence": [{"sequence": self._step()}]})
        assert _has_warning_containing(warnings, "`first`", "`second`")

    def test_then_mapping(self):
        warnings = check_script_config({"sequence": [{"if": [], "then": self._step()}]})
        assert _has_warning_containing(warnings, "`first`", "`second`")

    def test_else_mapping(self):
        warnings = check_script_config(
            {"sequence": [{"if": [], "then": [], "else": self._step()}]}
        )
        assert _has_warning_containing(warnings, "`first`", "`second`")

    def test_default_mapping(self):
        warnings = check_script_config(
            {"sequence": [{"choose": [], "default": self._step()}]}
        )
        assert _has_warning_containing(warnings, "`first`", "`second`")

    def test_parallel_mapping(self):
        warnings = check_script_config(
            {"sequence": [{"parallel": {"sequence": [self._step()]}}]}
        )
        assert _has_warning_containing(warnings, "`first`", "`second`")


class TestVariablesForwardReferenceWiring:
    """Every block that renders one key at a time is covered."""

    def test_automation_top_level_variables(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "variables": {"first": "{{ second }}", "second": "2"},
            "actions": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "`variables` key `first`", "`second`")

    def test_automation_trigger_variables(self):
        config = {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "trigger_variables": {"first": "{{ second }}", "second": "2"},
            "actions": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(
            warnings, "`trigger_variables` key `first`", "`second`"
        )

    def test_script_top_level_variables(self):
        config = {
            "variables": {"first": "{{ second }}", "second": "2"},
            "sequence": [],
        }
        warnings = check_script_config(config)
        assert _has_warning_containing(warnings, "`first`", "`second`")

    def test_script_sequence_variables_step(self):
        config = {"sequence": [{"variables": {"first": "{{ second }}", "second": "2"}}]}
        warnings = check_script_config(config)
        assert _has_warning_containing(warnings, "`first`", "`second`")


_FORWARD_REF_CONFIG = {
    "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
    "actions": [{"variables": {"first": "{{ second }}", "second": "2"}}],
}


class TestVariablesForwardReferenceMessage:
    """Warning payload: outer-scope caveat, skill route, referenced file."""

    def test_names_outer_scope_caveat(self):
        warnings = check_automation_config(_FORWARD_REF_CONFIG)
        assert _has_warning_containing(warnings, "outer scope")

    def test_skill_route_and_referenced_file(self):
        warnings = check_automation_config(_FORWARD_REF_CONFIG)
        assert _has_warning_containing(
            warnings, f"{SKILL_PREFIX}/automation-patterns.md#variables"
        )
        assert (
            "references/automation-patterns.md#variables" in warnings.referenced_files
        )

    def test_skill_prefix_none_suppresses_suffix(self):
        warnings = check_automation_config(_FORWARD_REF_CONFIG, skill_prefix=None)
        assert len(warnings) == 1
        assert " See " not in warnings[0]


# ---------------------------------------------------------------------------
# Variables-block ordering: span walking, scoping, binding forms
# ---------------------------------------------------------------------------


def _variables_warnings(variables):
    """Run the checker over one action-level variables block."""
    return check_automation_config(
        {
            "triggers": [{"trigger": "state", "entity_id": "sensor.x"}],
            "actions": [{"variables": variables}],
        }
    )


class TestVariablesForwardReferenceSpanWalking:
    """The span is closed by walking past string literals, not by a lazy `.*?`.

    A delimiter inside a literal used to end the span early, which hid a real
    forward read in one direction and invented one in the other.
    """

    def test_closing_delimiter_inside_a_literal_does_not_end_the_span(self):
        warnings = _variables_warnings({"first": '{{ "}}" ~ later }}', "later": "1"})
        assert _has_warning_containing(warnings, "`first`", "`later`")

    def test_sibling_name_inside_a_literal_is_not_a_read(self):
        assert _variables_warnings({"first": "{{ 'later }}' }}", "later": "1"}) == []

    def test_statement_delimiter_inside_a_literal(self):
        warnings = _variables_warnings({"first": "{% if '%}' ~ later %}x{% endif %}", "later": "1"})
        assert _has_warning_containing(warnings, "`first`", "`later`")

    def test_unterminated_span_is_not_guessed_at(self):
        assert _variables_warnings({"first": "{{ later", "later": "1"}) == []


class TestVariablesForwardReferenceNonCode:
    """Text that only looks like code: comments and raw blocks."""

    def test_commented_out_template_is_not_a_read(self):
        assert _variables_warnings({"first": "{# {{ later }} #}", "later": "1"}) == []

    def test_raw_body_is_not_a_read(self):
        config = {"first": "{% raw %}{{ later }}{% endraw %}", "later": "1"}
        assert _variables_warnings(config) == []

    def test_read_after_endraw_is_still_flagged(self):
        config = {"first": "{% raw %}x{% endraw %}{{ later }}", "later": "1"}
        assert _has_warning_containing(_variables_warnings(config), "`first`", "`later`")


class TestVariablesForwardReferenceScoping:
    """A binding dies with the block that made it."""

    def test_loop_local_set_does_not_suppress_a_read_after_endfor(self):
        # The assignment does not survive `{% endfor %}`, so the trailing read
        # is a genuine forward reference.
        config = {
            "first": "{% for x in [1] %}{% set later = 1 %}{% endfor %}{{ later }}",
            "later": "1",
        }
        assert _has_warning_containing(_variables_warnings(config), "`first`", "`later`")

    def test_read_inside_the_loop_is_still_shadowed(self):
        config = {
            "first": "{% for x in [1] %}{% set later = 1 %}{{ later }}{% endfor %}",
            "later": "1",
        }
        assert _variables_warnings(config) == []

    def test_loop_target_is_a_binding_not_a_read(self):
        config = {"first": "{% for later in [1] %}{{ later }}{% endfor %}", "later": "1"}
        assert _variables_warnings(config) == []

    def test_iterable_is_read_in_the_enclosing_scope(self):
        # `later` on the right of `in` is evaluated before the target binds.
        config = {"first": "{% for later in later %}x{% endfor %}", "later": "1"}
        assert _has_warning_containing(_variables_warnings(config), "`first`", "`later`")

    def test_with_block_binding_ends_at_endwith(self):
        config = {"first": "{% with later = 1 %}{% endwith %}{{ later }}", "later": "1"}
        assert _has_warning_containing(_variables_warnings(config), "`first`", "`later`")

    def test_with_block_binding_holds_inside(self):
        config = {"first": "{% with later = 1 %}{{ later }}{% endwith %}", "later": "1"}
        assert _variables_warnings(config) == []

    def test_macro_parameter_is_a_binding(self):
        config = {
            "first": "{% macro m(later) %}{{ later }}{% endmacro %}",
            "later": "1",
        }
        assert _variables_warnings(config) == []

    def test_macro_parameter_default_is_read_outside(self):
        config = {"first": "{% macro m(x=later) %}{% endmacro %}", "later": "1"}
        assert _has_warning_containing(_variables_warnings(config), "`first`", "`later`")

    def test_set_outside_a_loop_still_shadows_afterwards(self):
        config = {"first": "{% set later = 1 %}{{ later }}", "later": "1"}
        assert _variables_warnings(config) == []


class TestVariablesForwardReferenceBindingForms:
    """Every spelling of a binding Jinja accepts."""

    def test_whitespace_control_minus(self):
        assert _variables_warnings({"first": "{%- set later = 1 %}{{ later }}", "later": "1"}) == []

    def test_whitespace_control_plus(self):
        assert _variables_warnings({"first": "{%+ set later = 1 %}{{ later }}", "later": "1"}) == []

    def test_with_is_a_binding_tag_too(self):
        config = {"first": "{% with later = 3 %}{{ later }}{% endwith %}", "later": "1"}
        assert _variables_warnings(config) == []

    def test_tuple_target_binds_every_name(self):
        config = {"first": "{% set (later, other) = (1, 2) %}{{ later }}{{ other }}", "later": "1", "other": "2"}
        assert _variables_warnings(config) == []

    def test_multi_target_set_binds_every_name(self):
        config = {"first": "{% set later, other = 1, 2 %}{{ later }}{{ other }}", "later": "1", "other": "2"}
        assert _variables_warnings(config) == []

    def test_multi_target_right_hand_side_is_still_read(self):
        config = {"first": "{% set a1, b1 = later, 2 %}", "later": "1"}
        assert _has_warning_containing(_variables_warnings(config), "`first`", "`later`")

    def test_block_form_set_binds(self):
        config = {"first": "{% set later %}x{% endset %}{{ later }}", "later": "1"}
        assert _variables_warnings(config) == []

    def test_attribute_target_reads_the_base_name(self):
        # `{% set later.x = 1 %}` assigns into `later`, so it reads it.
        config = {"first": "{% set later.x = 1 %}", "later": "1"}
        assert _has_warning_containing(_variables_warnings(config), "`first`", "`later`")


class TestVariablesForwardReferenceTokenPositions:
    """Identifier tokens that are not variable reads."""

    def test_spaced_attribute_access(self):
        # Jinja parses `wetter . later` as attribute access, verified against a
        # live render, so the name after the dot is not a read.
        assert _variables_warnings({"first": "{{ wetter . later }}", "later": "1"}) == []

    def test_call_keyword_argument(self):
        assert _variables_warnings({"first": "{{ dict(later=1) }}", "later": "1"}) == []

    def test_test_name_after_is(self):
        assert _variables_warnings({"first": "{{ 1 is later }}", "later": "1"}) == []

    def test_negated_test_name(self):
        assert _variables_warnings({"first": "{{ 1 is not later }}", "later": "1"}) == []

    def test_jinja_keyword_sharing_a_sibling_name(self):
        assert _variables_warnings({"first": "{% if x %}y{% endif %}", "if": "1"}) == []

    def test_filter_name_is_not_a_read(self):
        assert _variables_warnings({"first": "{{ x | later }}", "later": "1"}) == []

    def test_equality_comparison_is_still_a_read(self):
        # The keyword-argument strip must not swallow `later ==`.
        warnings = _variables_warnings({"first": "{{ later == 1 }}", "later": "1"})
        assert _has_warning_containing(warnings, "`first`", "`later`")

    def test_inequality_comparison_is_still_a_read(self):
        warnings = _variables_warnings({"first": "{{ later != 1 }}", "later": "1"})
        assert _has_warning_containing(warnings, "`first`", "`later`")


class TestVariablesForwardReferenceNesting:
    """`_iter_strings` walks the whole value, keys included."""

    def test_template_in_a_dict_key(self):
        config = {"first": {"{{ later }}": "v"}, "later": "1"}
        assert _has_warning_containing(_variables_warnings(config), "`first`", "`later`")

    def test_template_in_a_nested_list(self):
        config = {"first": [{"deep": ["{{ later }}"]}], "later": "1"}
        assert _has_warning_containing(_variables_warnings(config), "`first`", "`later`")

    def test_bindings_do_not_leak_between_values(self):
        # Each string is scanned on its own, so a `{% set %}` in one value
        # cannot shadow a read in the next.
        config = {"first": ["{% set later = 1 %}", "{{ later }}"], "later": "1"}
        assert _has_warning_containing(_variables_warnings(config), "`first`", "`later`")


class TestVariablesForwardReferenceNormalizationSeam:
    """The premise the check rests on: normalization leaves the block alone.

    The reported bug arrived as an already-reordered block, so the check has
    to fire on the order it is handed. That only holds if the write path does
    not reorder or rename anything inside a variables block on the way in.
    """

    def test_normalization_preserves_key_order_and_names(self):
        from ha_mcp.tools.tools_config_automations import _normalize_automation_config

        config = {
            "triggers": [{"trigger": "state", "entity_id": "binary_sensor.door"}],
            "actions": [
                {
                    "variables": {
                        "meldung": "{% if offene_tueren %}open{% endif %}",
                        "offene_tueren": "{{ 1 }}",
                    }
                }
            ],
        }

        normalized = _normalize_automation_config(config)

        assert list(normalized["actions"][0]["variables"]) == [
            "meldung",
            "offene_tueren",
        ]
        assert _has_warning_containing(
            check_automation_config(normalized), "`meldung`", "`offene_tueren`"
        )
