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
    return any(
        all(f in w for f in fragments)
        for w in warnings
    )


# ---------------------------------------------------------------------------
# Clean configs — zero warnings
# ---------------------------------------------------------------------------


class TestCleanConfigs:
    """Verify zero overhead on clean configurations."""

    def test_clean_automation(self):
        config = {
            "trigger": [{"platform": "state", "entity_id": "light.bedroom"}],
            "condition": [{"condition": "state", "entity_id": "light.bedroom", "state": "on"}],
            "action": [{"service": "light.turn_off", "target": {"entity_id": "light.bedroom"}}],
        }
        assert check_automation_config(config) == []

    def test_clean_script(self):
        config = {
            "sequence": [
                {"service": "light.turn_on", "target": {"entity_id": "light.living_room"}},
                {"delay": {"seconds": 2}},
                {"service": "light.turn_off", "target": {"entity_id": "light.living_room"}},
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
            "trigger": [{"platform": "template", "value_template": "{{ states.sensor.x.state | float > 5 }}"}],
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
            "condition": [{
                "condition": "template",
                "value_template": "{{ states('sensor.temp') | float > 25 }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "float/int comparison", "numeric_state")

    def test_numeric_comparison_int_pipe(self):
        config = {
            "condition": [{
                "condition": "template",
                "value_template": "{{ states('sensor.count') | int >= 10 }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "float/int comparison", "numeric_state")

    def test_is_state_in_condition(self):
        config = {
            "condition": [{
                "condition": "template",
                "value_template": "{{ is_state('light.bedroom', 'on') }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "is_state()", "state")

    def test_sun_entity_condition(self):
        config = {
            "condition": [{
                "condition": "template",
                "value_template": "{{ is_state('sun.sun', 'below_horizon') }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "sun.sun", "sun")
        # Should NOT also flag generic is_state
        assert not _has_warning_containing(warnings, "is_state()", "state` condition")

    def test_now_hour_condition(self):
        config = {
            "condition": [{
                "condition": "template",
                "value_template": "{{ now().hour >= 22 }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "now().hour/minute", "time")

    def test_now_minute_condition(self):
        config = {
            "condition": [{
                "condition": "template",
                "value_template": "{{ now().minute == 30 }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "now().hour/minute", "time")

    def test_weekday_check_strftime(self):
        config = {
            "condition": [{
                "condition": "template",
                "value_template": "{{ now().strftime('%A') == 'Monday' }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "day-of-week", "weekday")

    def test_weekday_check_weekday_method(self):
        config = {
            "condition": [{
                "condition": "template",
                "value_template": "{{ now().weekday() == 0 }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "day-of-week", "weekday")

    def test_states_in_list(self):
        config = {
            "condition": [{
                "condition": "template",
                "value_template": "{{ states('climate.living_room') in ['heat', 'cool'] }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "states(...) in [...]", "state")

    def test_direct_state_access(self):
        config = {
            "condition": [{
                "condition": "template",
                "value_template": "{{ states.sensor.temperature.state | float > 20 }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "states.domain.entity.state", "states('entity_id')")

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
            "condition": [{
                "condition": "and",
                "conditions": [{
                    "condition": "template",
                    "value_template": "{{ is_state('light.x', 'on') }}",
                }],
            }],
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
            "trigger": [{"platform": "device", "device_id": "abc123", "type": "turned_on"}],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "device", "device_id", "entity_id")

    def test_template_trigger_numeric(self):
        config = {
            "trigger": [{
                "platform": "template",
                "value_template": "{{ states('sensor.temp') | float > 30 }}",
            }],
            "action": [],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "Trigger", "float/int", "numeric_state")

    def test_template_trigger_is_state(self):
        config = {
            "trigger": [{
                "platform": "template",
                "value_template": "{{ is_state('light.x', 'on') }}",
            }],
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
            "action": [{
                "choose": [{
                    "conditions": [{
                        "condition": "template",
                        "value_template": "{{ states('sensor.x') | float > 5 }}",
                    }],
                    "sequence": [],
                }],
            }],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "float/int comparison")

    def test_nested_condition_in_if(self):
        """Anti-patterns inside if conditions should be detected."""
        config = {
            "action": [{
                "if": [{
                    "condition": "template",
                    "value_template": "{{ is_state('light.x', 'on') }}",
                }],
                "then": [],
            }],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "is_state()")

    def test_nested_action_in_then_else(self):
        """wait_template inside then/else blocks should be detected."""
        config = {
            "action": [{
                "if": [{"condition": "state", "entity_id": "light.x", "state": "on"}],
                "then": [{"wait_template": "{{ is_state('door.x', 'open') }}"}],
            }],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "wait_template")

    def test_nested_repeat_while(self):
        """Anti-patterns in repeat while conditions should be detected."""
        config = {
            "action": [{
                "repeat": {
                    "while": [{
                        "condition": "template",
                        "value_template": "{{ states('sensor.x') | float > 0 }}",
                    }],
                    "sequence": [],
                },
            }],
        }
        warnings = check_automation_config(config)
        assert _has_warning_containing(warnings, "float/int comparison")

    def test_nested_repeat_until(self):
        """Anti-patterns in repeat until conditions should be detected."""
        config = {
            "action": [{
                "repeat": {
                    "until": [{
                        "condition": "template",
                        "value_template": "{{ now().hour >= 6 }}",
                    }],
                    "sequence": [],
                },
            }],
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
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.hallway_motion", "to": "on"}],
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
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.hallway_motion", "to": "on"}],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.hallway"}},
                {"delay": {"minutes": 5}},
            ],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(warnings, "motion", "mode: restart")

    def test_motion_without_delay_no_warning(self):
        config = {
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
            "action": [{"service": "light.turn_on", "target": {"entity_id": "light.living_room"}}],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(warnings, "motion")

    def test_non_motion_with_delay_no_warning(self):
        config = {
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.door_contact", "to": "on"}],
            "action": [{"delay": {"minutes": 5}}],
        }
        warnings = check_automation_config(config)
        assert not _has_warning_containing(warnings, "motion")


# ---------------------------------------------------------------------------
# skill_prefix modes
# ---------------------------------------------------------------------------


_SKILL_PREFIX_TEST_CONFIG = {
    "condition": [{
        "condition": "template",
        "value_template": "{{ is_state('light.x', 'on') }}",
    }],
    "action": [],
}


class TestSkillPrefixModes:
    """Verify warning output varies based on skill_prefix setting."""

    def test_default_skill_prefix(self):
        warnings = check_automation_config(_SKILL_PREFIX_TEST_CONFIG)
        assert any("skill://" in w for w in warnings)

    def test_custom_skill_prefix(self):
        warnings = check_automation_config(_SKILL_PREFIX_TEST_CONFIG, skill_prefix=GITHUB_PREFIX)
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
            "action": [{
                "service": "notify.mobile_app",
                "data": {
                    "message": "Temperature is {{ states('sensor.temp') | float }} degrees",
                },
            }],
        }
        warnings = check_automation_config(config)
        # The template is in service data, not in a condition/trigger template
        assert not _has_warning_containing(warnings, "float/int comparison")

    def test_template_in_condition_is_flagged(self):
        """Same template in a condition position SHOULD be flagged."""
        config = {
            "condition": [{
                "condition": "template",
                "value_template": "{{ states('sensor.temp') | float > 25 }}",
            }],
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
