"""Unit tests for tools_traces detailed trace formatting."""

from ha_mcp.tools.tools_traces import _format_detailed_trace

class TestFormatDetailedTrace:
    """Test _format_detailed_trace function."""

    def test_format_flat_trace_structure(self):
        """Test parsing of Home Assistant's flat path-based trace structure."""
        
        # Data structure as provided by user
        trace_data = {
            "timestamp": {"start": "2026-01-29T23:05:00.345824+00:00", "finish": "2026-01-29T23:05:00.356669+00:00"},
            "state": "stopped",
            "trigger": "time",
            "trace": {
                "trigger/0": [{
                    "path": "trigger/0",
                    "timestamp": "2026-01-29T23:05:00.345915+00:00",
                    "changed_variables": {
                        "trigger": {
                            "platform": "time",
                            "description": "time",
                            "entity_id": None
                        }
                    }
                }],
                "action/0": [{
                    "path": "action/0",
                    "timestamp": "2026-01-29T23:05:00.346301+00:00",
                    "result": {"params": {"domain": "light", "service": "turn_on"}}
                }],
                "action/0/0": [{
                    "path": "action/0/0",
                    "timestamp": "2026-01-29T23:05:00.347072+00:00",
                    "child_id": {"domain": "script", "item_id": "set_brightness_chambre", "run_id": "04e0241d"},
                    "result": {"params": {"domain": "script"}}
                }]
            },
            "config": {
                "alias": "Lumières Chambre 18h05",
                "mode": "single"
            }
        }

        result = _format_detailed_trace("automation.test", "run_123", trace_data)

        assert result["success"] is True
        assert result["automation_id"] == "automation.test"
        assert result["run_id"] == "run_123"
        
        # Verify Trigger
        assert "trigger" in result
        assert result["trigger"]["platform"] == "time"
        assert result["trigger"]["description"] == "time"
        
        # Verify Actions
        assert "action_trace" in result
        actions = result["action_trace"]
        assert len(actions) == 2
        
        # Sort order should be preserved (action/0 then action/0/0)
        assert actions[0]["path"] == "action/0"
        assert actions[1]["path"] == "action/0/0"
        
        # Verify content of actions
        assert actions[0]["result"]["params"]["service"] == "turn_on"
        assert actions[1]["child_id"]["item_id"] == "set_brightness_chambre"

    def test_format_legacy_trace_structure(self):
        """Test fallback parsing of potential legacy trace structure (lists)."""
        
        trace_data = {
            "timestamp": "2026-01-29T23:05:00",
            "state": "stopped",
            "trace": {
                "trigger": [{
                    "path": "trigger/0",
                    "variables": {
                        "trigger": {
                            "platform": "state",
                            "description": "state change"
                        }
                    }
                }],
                "action": [{
                    "path": "action/0",
                    "result": {"executed": True}
                }]
            }
        }

        result = _format_detailed_trace("automation.legacy", "run_456", trace_data)

        assert result["success"] is True
        
        # Verify Trigger
        assert result["trigger"]["platform"] == "state"
        
        # Verify Actions
        assert len(result["action_trace"]) == 1
        assert result["action_trace"][0]["result"]["executed"] is True

    def test_format_mixed_variables_location(self):
        """Test that variables are found whether in 'variables' or 'changed_variables'."""
        
        trace_data = {
            "trace": {
                "trigger/0": [{
                    "variables": {
                        "trigger": {"platform": "variables_key"}
                    }
                }],
                "trigger/1": [{
                    "changed_variables": {
                        "trigger": {"platform": "changed_variables_key"}
                    }
                }]
            }
        }
        
        # Test finding in 'variables' (legacy/standard)
        result1 = _format_detailed_trace("auto.1", "1", 
            {"trace": {"trigger/0": trace_data["trace"]["trigger/0"]}})
        assert result1["trigger"]["platform"] == "variables_key"
        
        # Test finding in 'changed_variables' (new flat format)
        result2 = _format_detailed_trace("auto.2", "2", 
            {"trace": {"trigger/0": trace_data["trace"]["trigger/1"]}})
        assert result2["trigger"]["platform"] == "changed_variables_key"
