"""Unit tests for automation configuration normalization.

HA's 2024.10+ canonical automation shape uses the plural root list keys
('triggers', 'actions', 'conditions') with per-trigger 'trigger:' type keys.
``_normalize_automation_config`` canonicalizes to that plural form at the root
level only; the singular forms remain accepted as silent aliases on input.
"""

from ha_mcp.tools.tools_config_automations import (
    _normalize_automation_config,
    _normalize_config_for_roundtrip,
    _normalize_trigger_keys,
)


class TestAutomationNormalization:
    """Tests for _normalize_automation_config function."""

    def test_normalize_root_level_singular_to_plural(self):
        """Root-level singular keys are normalized to the canonical plural."""
        config = {
            "trigger": [{"trigger": "state"}],
            "condition": [{"condition": "state"}],
            "action": [{"action": "light.turn_on"}],
        }

        result = _normalize_automation_config(config)

        assert "triggers" in result
        assert "conditions" in result
        assert "actions" in result
        assert "trigger" not in result
        assert "condition" not in result
        assert "action" not in result

    def test_idempotent_on_canonical_plural(self):
        """Already-canonical plural root keys pass through unchanged."""
        config = {
            "triggers": [{"trigger": "state"}],
            "conditions": [{"condition": "state"}],
            "actions": [{"action": "light.turn_on"}],
        }

        result = _normalize_automation_config(config)

        assert set(result) == {"triggers", "conditions", "actions"}
        assert result == config

    def test_preserve_conditions_in_choose_blocks(self):
        """'conditions' (plural) is preserved inside choose blocks."""
        config = {
            "trigger": [{"trigger": "state"}],
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [{"condition": "trigger", "id": "trigger_1"}],
                            "sequence": [{"action": "light.turn_on"}],
                        },
                        {
                            "conditions": [{"condition": "trigger", "id": "trigger_2"}],
                            "sequence": [{"action": "light.turn_off"}],
                        },
                    ]
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Root level should use the canonical plural forms.
        assert "triggers" in result
        assert "actions" in result

        # Inside choose blocks, 'conditions' should remain plural.
        choose_block = result["actions"][0]["choose"]
        assert "conditions" in choose_block[0]
        assert "conditions" in choose_block[1]

    def test_preserve_conditions_in_if_blocks(self):
        """'conditions' (plural) is preserved inside if action blocks."""
        config = {
            "trigger": [{"trigger": "state"}],
            "action": [
                {
                    "if": [
                        {
                            "conditions": [
                                {"condition": "state", "entity_id": "light.test"}
                            ]
                        }
                    ],
                    "then": [{"action": "light.turn_on"}],
                    "else": [{"action": "light.turn_off"}],
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Inside if blocks, 'conditions' should remain plural.
        if_block = result["actions"][0]["if"]
        assert "conditions" in if_block[0]

    def test_nested_choose_with_multiple_conditions(self):
        """Complex nested choose blocks with multiple conditions."""
        config = {
            "triggers": [
                {"trigger": "template", "id": "trigger_1"},
                {"trigger": "template", "id": "trigger_2"},
            ],
            "actions": [
                {
                    "choose": [
                        {
                            "conditions": [
                                {"condition": "trigger", "id": "trigger_1"},
                                {"condition": "state", "entity_id": "light.test"},
                            ],
                            "sequences": [{"action": "light.turn_on"}],
                        },
                    ]
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Root level stays canonical plural.
        assert "triggers" in result
        assert "actions" in result

        # Inside choose, 'conditions' (plural) should be preserved.
        choose_option = result["actions"][0]["choose"][0]
        assert "conditions" in choose_option
        assert len(choose_option["conditions"]) == 2

        # 'sequences' should be normalized to the canonical singular 'sequence'.
        assert "sequence" in choose_option
        assert "sequences" not in choose_option

    def test_default_action_in_choose(self):
        """choose blocks with default actions work correctly."""
        config = {
            "trigger": [{"trigger": "state"}],
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [{"condition": "trigger", "id": "trigger_1"}],
                            "sequence": [{"action": "light.turn_on"}],
                        }
                    ],
                    "default": [{"action": "light.turn_off"}],
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Verify choose structure.
        choose_action = result["actions"][0]
        assert "choose" in choose_action
        assert "default" in choose_action
        assert "conditions" in choose_action["choose"][0]

    def test_mixed_singular_and_plural_prefers_plural(self):
        """When both singular and plural exist, the canonical plural is preferred."""
        config = {
            "trigger": [
                {"trigger": "state", "entity_id": "test.entity"}
            ],  # alias, dropped
            "triggers": [{"trigger": "time"}],
        }

        result = _normalize_automation_config(config)

        assert "triggers" in result
        assert "trigger" not in result
        # The canonical plural value is preserved; the singular alias is dropped.
        assert result["triggers"][0]["trigger"] == "time"

    def test_primitives_and_lists_unchanged(self):
        """Primitive values and non-config lists are unchanged."""
        config = {
            "alias": "Test Automation",
            "description": "A test",
            "trigger": [{"trigger": "state"}],
            "action": [{"action": "test.service", "data": {"param": [1, 2, 3]}}],
        }

        result = _normalize_automation_config(config)

        assert result["alias"] == "Test Automation"
        assert result["description"] == "A test"
        assert result["actions"][0]["data"]["param"] == [1, 2, 3]

    def test_empty_config(self):
        """Empty configurations are handled gracefully."""
        assert _normalize_automation_config({}) == {}
        assert _normalize_automation_config([]) == []
        assert _normalize_automation_config(None) is None
        assert _normalize_automation_config("string") == "string"
        assert _normalize_automation_config(123) == 123

    def test_nested_conditions_not_touched_below_root(self):
        """A 'conditions' list below the root is never rewritten (only root is normalized)."""
        config = {
            "actions": [
                {
                    "choose": [
                        {
                            "conditions": [{"condition": "state"}],  # preserved
                            "sequence": [
                                {
                                    # A nested 'conditions' block deeper in the tree
                                    # must also be left untouched.
                                    "conditions": [
                                        {
                                            "condition": "state",
                                            "entity_id": "sun.sun",
                                            "state": "above_horizon",
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }
            ]
        }

        result = _normalize_automation_config(config)

        choose_option = result["actions"][0]["choose"][0]
        action_in_sequence = choose_option["sequence"][0]

        # 'conditions' is preserved at the choose option level.
        assert "conditions" in choose_option
        # 'conditions' deeper in the sequence is also preserved (never singularized).
        assert "conditions" in action_in_sequence
        assert "condition" not in action_in_sequence

    def test_preserve_conditions_in_or_blocks(self):
        """'conditions' (plural) is preserved inside 'or' condition blocks."""
        config = {
            "trigger": [{"trigger": "state"}],
            "condition": [
                {
                    "condition": "or",
                    "conditions": [
                        {
                            "condition": "state",
                            "entity_id": "light.test1",
                            "state": "on",
                        },
                        {
                            "condition": "state",
                            "entity_id": "light.test2",
                            "state": "on",
                        },
                    ],
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Root level uses the canonical plural form.
        assert "conditions" in result
        assert "condition" not in result

        # Inside 'or' block, 'conditions' should remain plural and 'condition'
        # is the type discriminator (string).
        or_condition = result["conditions"][0]
        assert or_condition["condition"] == "or"
        assert "conditions" in or_condition
        assert len(or_condition["conditions"]) == 2

    def test_preserve_conditions_in_and_blocks(self):
        """'conditions' (plural) is preserved inside 'and' condition blocks."""
        config = {
            "trigger": [{"trigger": "state"}],
            "condition": [
                {
                    "condition": "and",
                    "conditions": [
                        {
                            "condition": "state",
                            "entity_id": "light.test1",
                            "state": "on",
                        },
                        {
                            "condition": "numeric_state",
                            "entity_id": "sensor.temp",
                            "above": 20,
                        },
                    ],
                }
            ],
        }

        result = _normalize_automation_config(config)

        and_condition = result["conditions"][0]
        assert and_condition["condition"] == "and"
        assert "conditions" in and_condition
        assert len(and_condition["conditions"]) == 2

    def test_preserve_conditions_in_not_blocks(self):
        """'conditions' (plural) is preserved inside 'not' condition blocks."""
        config = {
            "trigger": [{"trigger": "state"}],
            "condition": [
                {
                    "condition": "not",
                    "conditions": [
                        {"condition": "state", "entity_id": "light.test", "state": "on"}
                    ],
                }
            ],
        }

        result = _normalize_automation_config(config)

        not_condition = result["conditions"][0]
        assert not_condition["condition"] == "not"
        assert "conditions" in not_condition
        assert len(not_condition["conditions"]) == 1

    def test_nested_compound_conditions(self):
        """Deeply nested compound conditions (or inside and, etc.)."""
        config = {
            "trigger": [{"trigger": "state"}],
            "conditions": [
                {
                    "condition": "and",
                    "conditions": [
                        {
                            "condition": "state",
                            "entity_id": "light.test1",
                            "state": "on",
                        },
                        {
                            "condition": "or",
                            "conditions": [
                                {
                                    "condition": "state",
                                    "entity_id": "light.test2",
                                    "state": "on",
                                },
                                {
                                    "condition": "state",
                                    "entity_id": "light.test3",
                                    "state": "on",
                                },
                            ],
                        },
                    ],
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Root level uses canonical plural 'conditions'.
        assert "conditions" in result
        assert "condition" not in result

        # First level: 'and' block preserves 'conditions'.
        and_condition = result["conditions"][0]
        assert and_condition["condition"] == "and"
        assert "conditions" in and_condition
        assert len(and_condition["conditions"]) == 2

        # Second level: nested 'or' block preserves 'conditions'.
        or_condition = and_condition["conditions"][1]
        assert or_condition["condition"] == "or"
        assert "conditions" in or_condition
        assert len(or_condition["conditions"]) == 2

    def test_compound_conditions_in_choose_block(self):
        """Compound conditions inside choose block conditions."""
        config = {
            "trigger": [{"trigger": "state"}],
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [
                                {"condition": "trigger", "id": "vehicle_ignition_on"},
                                {
                                    "condition": "or",
                                    "conditions": [
                                        {
                                            "condition": "state",
                                            "entity_id": "device_tracker.vehicle",
                                            "state": "home",
                                        },
                                        {
                                            "condition": "state",
                                            "entity_id": "binary_sensor.garage",
                                            "state": "on",
                                        },
                                    ],
                                },
                            ],
                            "sequence": [{"action": "light.turn_on"}],
                        }
                    ]
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Verify choose block preserves 'conditions' at top level.
        choose_option = result["actions"][0]["choose"][0]
        assert "conditions" in choose_option
        assert len(choose_option["conditions"]) == 2

        # Verify nested 'or' block preserves 'conditions'.
        or_condition = choose_option["conditions"][1]
        assert or_condition["condition"] == "or"
        assert "conditions" in or_condition
        assert len(or_condition["conditions"]) == 2

    def test_root_level_normalization_with_compound_conditions(self):
        """Root-level singular keys are pluralized even with compound conditions."""
        config = {
            "trigger": [{"trigger": "state"}],
            "condition": [
                {
                    "condition": "or",
                    "conditions": [
                        {
                            "condition": "state",
                            "entity_id": "light.test1",
                            "state": "on",
                        },
                    ],
                }
            ],
            "action": [{"action": "light.turn_on"}],
        }

        result = _normalize_automation_config(config)

        # Root level normalized to canonical plural.
        assert "triggers" in result
        assert "conditions" in result
        assert "actions" in result
        assert "trigger" not in result
        assert "condition" not in result
        assert "action" not in result

        # Inside compound condition, 'conditions' should be preserved.
        or_condition = result["conditions"][0]
        assert "conditions" in or_condition

    def test_no_normalize_singular_action_inside_delay_object(self):
        """A singular 'action' key below the root is NOT pluralized.

        Regression for issue #498 (inverted direction): the root list key is
        pluralized, but a singular 'action:' deeper in the tree (e.g. a service
        call inside a sequence step) is a discriminator, not a list key, and
        must be left untouched.
        """
        config = {
            "alias": "Test",
            "trigger": [{"trigger": "state", "entity_id": "sensor.test"}],
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [{"condition": "trigger", "id": "t1"}],
                            "sequence": [
                                {
                                    "delay": {"seconds": 5},
                                    "action": "light.turn_on",
                                },
                            ],
                        }
                    ]
                }
            ],
        }

        result = _normalize_automation_config(config)

        # The singular 'action:' service call inside the sequence step must stay
        # singular (it is not a root list key).
        delay_step = result["actions"][0]["choose"][0]["sequence"][0]
        assert delay_step["action"] == "light.turn_on"
        assert "actions" not in delay_step

    def test_no_normalize_plural_actions_inside_nested_structure(self):
        """A stray 'actions' key below the root is left untouched (issue #498)."""
        config = {
            "alias": "Test",
            "trigger": [{"trigger": "state"}],
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [{"condition": "trigger", "id": "t1"}],
                            "sequence": [
                                {
                                    "delay": {"seconds": 5},
                                    "actions": [{"action": "light.turn_on"}],
                                },
                            ],
                        }
                    ]
                }
            ],
        }

        result = _normalize_automation_config(config)

        # A malformed nested 'actions' key is left as-is so HA can surface a
        # clear validation error rather than the normalizer silently rewriting it.
        delay_step = result["actions"][0]["choose"][0]["sequence"][0]
        assert "actions" in delay_step

    def test_no_normalize_triggers_inside_nested_structure(self):
        """A stray 'triggers' key below the root is left untouched."""
        config = {
            "alias": "Test",
            "trigger": [{"trigger": "state"}],
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [{"condition": "trigger", "id": "t1"}],
                            "sequence": [
                                {"action": "light.turn_on", "triggers": ["fake"]},
                            ],
                        }
                    ]
                }
            ],
        }

        result = _normalize_automation_config(config)

        # 'triggers' inside a service call should NOT be touched.
        service_step = result["actions"][0]["choose"][0]["sequence"][0]
        assert "triggers" in service_step

    def test_root_level_singular_still_pluralized(self):
        """Root-level singular 'trigger'/'action' are pluralized to the canonical form."""
        config = {
            "alias": "Test",
            "trigger": [{"trigger": "state"}],
            "action": [{"action": "light.turn_on"}],
        }

        result = _normalize_automation_config(config)

        assert "triggers" in result
        assert "trigger" not in result
        assert "actions" in result
        assert "action" not in result

    def test_complex_nested_choose_if_then_with_delays(self):
        """The exact scenario from issue #498 — complex nested choose/if/then with delays."""
        config = {
            "alias": "Complex Automation",
            "triggers": [{"trigger": "state", "entity_id": "sensor.test"}],
            "actions": [
                {
                    "choose": [
                        {
                            "conditions": [{"condition": "trigger", "id": "t1"}],
                            "sequence": [
                                {
                                    "action": "notify.mobile",
                                    "data": {"message": "Step 1"},
                                },
                                {"delay": {"minutes": 2}},
                                {
                                    "if": [
                                        {
                                            "condition": "state",
                                            "entity_id": "light.test",
                                            "state": "on",
                                        }
                                    ],
                                    "then": [
                                        {"delay": {"seconds": 30}},
                                        {
                                            "action": "light.turn_off",
                                            "target": {"entity_id": "light.test"},
                                        },
                                    ],
                                },
                            ],
                        },
                        {
                            "conditions": [{"condition": "trigger", "id": "t2"}],
                            "sequence": [
                                {"delay": {"seconds": 5}},
                                {
                                    "action": "light.turn_on",
                                    "target": {"entity_id": "light.test"},
                                },
                            ],
                        },
                    ],
                    "default": [
                        {"action": "notify.mobile", "data": {"message": "Default"}}
                    ],
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Root level stays canonical plural.
        assert "triggers" in result
        assert "actions" in result
        assert "trigger" not in result
        assert "action" not in result

        # Choose conditions should be preserved as plural.
        choose_block = result["actions"][0]["choose"]
        assert "conditions" in choose_block[0]
        assert "conditions" in choose_block[1]

        # Nested if/then structure should be intact.
        if_block = choose_block[0]["sequence"][2]
        assert "if" in if_block
        assert "then" in if_block
        assert len(if_block["then"]) == 2


class TestRoundtripNormalization:
    """Tests for the GET->SET round-trip helpers (_normalize_config_for_roundtrip).

    These canonicalize a config fetched from HA into the modern 2024.10+ shape:
    plural root list keys and per-trigger 'trigger:' type keys.
    """

    def test_normalize_trigger_keys_platform_to_trigger(self):
        """Legacy per-trigger 'platform' is canonicalized to modern 'trigger'."""
        triggers = [
            {"platform": "state", "entity_id": "binary_sensor.motion"},
            {"trigger": "time", "at": "07:00:00"},  # already modern, untouched
        ]

        result = _normalize_trigger_keys(triggers)

        assert result[0]["trigger"] == "state"
        assert "platform" not in result[0]
        assert result[1]["trigger"] == "time"

    def test_normalize_trigger_keys_drops_platform_when_trigger_present(self):
        """When both 'platform' and 'trigger' exist, 'trigger' wins and the legacy
        'platform' alias is dropped (HA strict schema rejects extra keys)."""
        triggers = [{"platform": "state", "trigger": "time"}]

        result = _normalize_trigger_keys(triggers)

        assert result[0]["trigger"] == "time"
        assert "platform" not in result[0]

    def test_normalize_trigger_keys_passes_non_dict_through(self):
        """Malformed non-dict trigger items pass through untouched (no AttributeError)."""
        # Deliberately malformed input to exercise the defensive guard.
        result = _normalize_trigger_keys([{"platform": "state"}, "oops", None])  # type: ignore[list-item]
        assert result[0] == {"trigger": "state"}
        assert result[1] == "oops"
        assert result[2] is None

    def test_roundtrip_produces_plural_roots_and_modern_trigger_keys(self):
        """A GET-shaped config is canonicalized to plural roots + 'trigger:' keys."""
        config = {
            "alias": "Morning",
            "trigger": [{"platform": "time", "at": "07:00:00"}],
            "condition": [{"condition": "state", "entity_id": "x", "state": "on"}],
            "action": [{"action": "light.turn_on"}],
        }

        result = _normalize_config_for_roundtrip(config)

        assert "triggers" in result and "trigger" not in result
        assert "conditions" in result and "condition" not in result
        assert "actions" in result and "action" not in result
        # Per-trigger key canonicalized to modern 'trigger:'.
        assert result["triggers"][0]["trigger"] == "time"
        assert "platform" not in result["triggers"][0]
