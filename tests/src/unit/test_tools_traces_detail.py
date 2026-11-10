"""Unit tests for native extended-dict trace formatting."""

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_traces import _format_detailed_trace
from ha_mcp.tools.trace_lookup import TraceLogbook


def _trace() -> dict:
    return {
        "run_id": "r1",
        "state": "stopped",
        "timestamp": {
            "start": "2026-11-08T16:00:00+00:00",
            "finish": "2026-11-08T16:00:01+00:00",
        },
        "config": {"alias": "Trace capture", "mode": "single"},
        "trace": {
            "trigger/0": [
                {
                    "path": "trigger/0",
                    "changed_variables": {
                        "trigger": {
                            "id": "evt_trig",
                            "platform": "event",
                            "event": {"data": {"go": False}},
                        }
                    },
                    "result": {"event": {"data": {"go": False}}},
                }
            ],
            "condition/0": [
                {
                    "path": "condition/0",
                    "result": {"result": False},
                    "state": "below",
                    "wanted_state": "above",
                    "changed_variables": {"trigger": {"id": "evt_trig"}},
                }
            ],
            "action/0": [
                {
                    "path": "action/0",
                    "result": {"params": {"domain": "light"}},
                    "changed_variables": {"trigger": {"id": "evt_trig"}},
                    "error": "service failed",
                }
            ],
        },
        "context": {"id": "ctx-1"},
        "blueprint_inputs": {"target": "light.one"},
    }


class TestFormatDetailedTrace:
    def test_extended_dict_passes_through_unchanged(self) -> None:
        trace = _trace()
        result = _format_detailed_trace("automation.test", "r1", trace)

        assert result["success"] is True
        assert result["entity_id"] == "automation.test"
        assert result["run_id"] == "r1"
        assert result["trace"] is trace["trace"]
        assert result["config"] == trace["config"]
        assert result["blueprint_inputs"] == trace["blueprint_inputs"]
        assert "trigger" not in result
        assert "condition_results" not in result
        assert "action_trace" not in result
        assert "config_summary" not in result

    def test_step_result_variables_and_error_survive(self) -> None:
        result = _format_detailed_trace("automation.test", "r1", _trace())

        trigger = result["trace"]["trigger/0"][0]
        condition = result["trace"]["condition/0"][0]
        action = result["trace"]["action/0"][0]

        assert trigger["changed_variables"]["trigger"]["id"] == "evt_trig"
        assert trigger["changed_variables"]["trigger"]["event"]["data"]["go"] is False
        assert condition["state"] == "below"
        assert condition["wanted_state"] == "above"
        assert action["result"]["params"]["domain"] == "light"
        assert action["error"] == "service failed"

    def test_sections_keep_top_level_keys(self) -> None:
        result = _format_detailed_trace(
            "automation.test", "r1", _trace(), sections="trace,config"
        )

        assert set(result) == {"success", "entity_id", "run_id", "trace", "config"}

    def test_unknown_section_is_rejected(self) -> None:
        with pytest.raises(ToolError, match="Unknown trace section"):
            _format_detailed_trace("automation.test", "r1", _trace(), sections="action")

    def test_metadata_sections_are_rejected(self) -> None:
        """success and unique_id are not valid section requests."""
        with pytest.raises(ToolError, match="Unknown trace section"):
            _format_detailed_trace(
                "automation.test", "r1", _trace(), sections="success"
            )
        with pytest.raises(ToolError, match="Unknown trace section"):
            _format_detailed_trace(
                "automation.test", "r1", _trace(), sections="unique_id"
            )

    def test_logbook_section_adds_logbook_fields(self) -> None:
        logbook = TraceLogbook(
            [{"name": "Light"}],
            "component",
            [],
            1,
            False,
        )

        result = _format_detailed_trace(
            "automation.test", "r1", _trace(), sections="logbook", logbook=logbook
        )

        assert result["logbook_entries"] == [{"name": "Light"}]
        assert result["logbook_transport"] == "component"
        assert result["logbook_total"] == 1
        assert result["logbook_truncated"] is False

    def test_whitespace_sections_returns_the_full_dict(self) -> None:
        trace = _trace()
        result = _format_detailed_trace("automation.test", "r1", trace, sections="   ")

        assert result["trace"] is trace["trace"]
        assert result["config"] == trace["config"]
        assert result["blueprint_inputs"] == trace["blueprint_inputs"]

    def test_sections_keep_domain_and_item_metadata(self) -> None:
        result = _format_detailed_trace(
            None,
            "r1",
            _trace(),
            domain="automation",
            item_id="uid_1",
            sections="trace",
        )

        assert set(result) == {
            "success",
            "run_id",
            "domain",
            "item_id",
            "trace",
        }
        assert "entity_id" not in result
