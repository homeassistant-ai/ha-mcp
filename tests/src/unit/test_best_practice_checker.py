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
# BestPracticeCheckResult shape + 3-route warning text (issue #1182)
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

    def test_referenced_files_populated_on_warning(self):
        """When a warning fires, the referenced skill file (with references/ prefix) is tracked."""
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
        assert "references/automation-patterns.md" in result.referenced_files

    def test_referenced_files_dedup_across_repeated_emissions(self):
        """Multiple warnings against the same file collapse to one set entry."""
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
        assert result.referenced_files == {"references/automation-patterns.md"}

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
        assert result.referenced_files == {"references/automation-patterns.md"}


class TestThreeRouteWarningSuffix:
    """Each warning names all 3 access routes for the referenced skill file
    (issue #1182). Clients that can't resolve skill:// URIs still have the
    ha_get_skill_guide tool route and the include_skill parameter route."""

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

    def test_default_warning_names_include_skill_route(self):
        msg = self._first_warning()
        assert "include_skill=True" in msg

    def test_custom_prefix_replaces_skill_uri_keeps_other_routes(self):
        msg = self._first_warning(prefix="https://example.com/refs")
        assert "https://example.com/refs/automation-patterns.md" in msg
        assert "skill://" not in msg
        # Tool + param routes always present when skills are on
        assert "ha_get_skill_guide" in msg
        assert "include_skill" in msg

    def test_anchor_preserved_in_uri_stripped_in_tool_route(self):
        """The skill:// URI keeps the #anchor (links scroll to section);
        the ha_get_skill_guide call uses bare file path (tool reads the whole file)."""
        msg = self._first_warning()
        # URI: anchor preserved
        assert "automation-patterns.md#native-conditions" in msg
        # Tool route: bare file path, no anchor
        assert "file='references/automation-patterns.md'" in msg
