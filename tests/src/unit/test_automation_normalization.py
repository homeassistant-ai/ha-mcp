"""Unit tests for automation configuration normalization."""

from ha_mcp.tools.tools_config_automations import _normalize_automation_config


class TestAutomationNormalization:
    """Tests for _normalize_automation_config function."""

    def test_normalize_root_level_plural_to_singular(self):
        """Test that root-level plural keys are normalized to singular."""
        config = {
            "triggers": [{"platform": "state"}],
            "conditions": [{"condition": "state"}],
            "actions": [{"service": "light.turn_on"}],
        }

        result = _normalize_automation_config(config)

        assert "trigger" in result
        assert "condition" in result
        assert "action" in result
        assert "triggers" not in result
        assert "conditions" not in result
        assert "actions" not in result

    def test_preserve_conditions_in_choose_blocks(self):
        """Test that 'conditions' (plural) is preserved inside choose blocks."""
        config = {
            "trigger": [{"platform": "state"}],
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [
                                {"condition": "trigger", "id": "trigger_1"}
                            ],
                            "sequence": [{"service": "light.turn_on"}],
                        },
                        {
                            "conditions": [
                                {"condition": "trigger", "id": "trigger_2"}
                            ],
                            "sequence": [{"service": "light.turn_off"}],
                        },
                    ]
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Root level should have singular forms
        assert "trigger" in result
        assert "action" in result

        # Inside choose blocks, 'conditions' should remain plural
        choose_block = result["action"][0]["choose"]
        assert "conditions" in choose_block[0]
        assert "condition" not in choose_block[0]
        assert "conditions" in choose_block[1]
        assert "condition" not in choose_block[1]

    def test_preserve_conditions_in_if_blocks(self):
        """Test that 'conditions' (plural) is preserved inside if action blocks."""
        config = {
            "trigger": [{"platform": "state"}],
            "action": [
                {
                    "if": [
                        {"conditions": [{"condition": "state", "entity_id": "light.test"}]}
                    ],
                    "then": [{"service": "light.turn_on"}],
                    "else": [{"service": "light.turn_off"}],
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Inside if blocks, 'conditions' should remain plural
        if_block = result["action"][0]["if"]
        assert "conditions" in if_block[0]
        assert "condition" not in if_block[0]

    def test_nested_choose_with_multiple_conditions(self):
        """Test complex nested choose blocks with multiple conditions."""
        config = {
            "triggers": [
                {"platform": "template", "id": "trigger_1"},
                {"platform": "template", "id": "trigger_2"},
            ],
            "actions": [
                {
                    "choose": [
                        {
                            "conditions": [
                                {"condition": "trigger", "id": "trigger_1"},
                                {"condition": "state", "entity_id": "light.test"},
                            ],
                            "sequences": [{"service": "light.turn_on"}],
                        },
                    ]
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Root level normalization
        assert "trigger" in result
        assert "action" in result

        # Inside choose, 'conditions' (plural) should be preserved
        choose_option = result["action"][0]["choose"][0]
        assert "conditions" in choose_option
        assert len(choose_option["conditions"]) == 2

        # 'sequences' should be normalized to 'sequence'
        assert "sequence" in choose_option
        assert "sequences" not in choose_option

    def test_default_action_in_choose(self):
        """Test that choose blocks with default actions work correctly."""
        config = {
            "trigger": [{"platform": "state"}],
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [{"condition": "trigger", "id": "trigger_1"}],
                            "sequence": [{"service": "light.turn_on"}],
                        }
                    ],
                    "default": [{"service": "light.turn_off"}],
                }
            ],
        }

        result = _normalize_automation_config(config)

        # Verify choose structure
        choose_action = result["action"][0]
        assert "choose" in choose_action
        assert "default" in choose_action
        assert "conditions" in choose_action["choose"][0]

    def test_mixed_singular_and_plural_prefers_singular(self):
        """Test that when both singular and plural exist, singular is preferred."""
        config = {
            "trigger": [{"platform": "state", "entity_id": "test.entity"}],
            "triggers": [{"platform": "time"}],  # Should be removed
        }

        result = _normalize_automation_config(config)

        assert "trigger" in result
        assert "triggers" not in result
        # Original singular value should be preserved
        assert result["trigger"][0]["platform"] == "state"

    def test_primitives_and_lists_unchanged(self):
        """Test that primitive values and non-config lists are unchanged."""
        config = {
            "alias": "Test Automation",
            "description": "A test",
            "trigger": [{"platform": "state"}],
            "action": [{"service": "test.service", "data": {"param": [1, 2, 3]}}],
        }

        result = _normalize_automation_config(config)

        assert result["alias"] == "Test Automation"
        assert result["description"] == "A test"
        assert result["action"][0]["data"]["param"] == [1, 2, 3]

    def test_empty_config(self):
        """Test that empty configurations are handled gracefully."""
        assert _normalize_automation_config({}) == {}
        assert _normalize_automation_config([]) == []
        assert _normalize_automation_config(None) is None
        assert _normalize_automation_config("string") == "string"
        assert _normalize_automation_config(123) == 123

    def test_normalize_conditions_in_sequence_of_choose_block(self):
        """Test that 'conditions' is normalized inside a sequence of a choose block."""
        config = {
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [{"condition": "state"}],  # Should be preserved
                            "sequence": [
                                {
                                    # This 'conditions' block is a condition action, and should be normalized
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

        choose_option = result["action"][0]["choose"][0]
        action_in_sequence = choose_option["sequence"][0]

        # Verify 'conditions' is preserved at the choose option level
        assert "conditions" in choose_option
        assert "condition" not in choose_option

        # Verify 'conditions' is normalized to 'condition' inside the sequence
        assert "condition" in action_in_sequence
        assert "conditions" not in action_in_sequence
