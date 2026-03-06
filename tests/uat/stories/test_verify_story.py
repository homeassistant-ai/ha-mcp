"""Unit tests for verify_story.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT = Path(__file__).resolve().parent / "scripts" / "verify_story.py"
spec = importlib.util.spec_from_file_location("verify_story", str(SCRIPT))
verify_story = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_story)


def _mock_response(status_code: int, json_data):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    return r


HA_URL = "http://localhost:9999"
HA_TOKEN = "test-token"


class TestEntityExists:
    def test_found(self):
        with patch("requests.get", return_value=_mock_response(200, {"state": "on"})):
            result = verify_story._check_entity_exists(
                HA_URL, HA_TOKEN, {"type": "entity_exists", "entity_id": "light.test"}
            )
        assert result["passed"] is True

    def test_not_found(self):
        with patch("requests.get", return_value=_mock_response(404, {})):
            result = verify_story._check_entity_exists(
                HA_URL, HA_TOKEN, {"type": "entity_exists", "entity_id": "light.missing"}
            )
        assert result["passed"] is False
        assert "not found" in result["detail"]


class TestEntityState:
    def test_state_matches(self):
        with patch("requests.get", return_value=_mock_response(200, {"state": "on"})):
            result = verify_story._check_entity_state(
                HA_URL, HA_TOKEN, {"type": "entity_state", "entity_id": "automation.test", "state": "on"}
            )
        assert result["passed"] is True

    def test_state_mismatch(self):
        with patch("requests.get", return_value=_mock_response(200, {"state": "off"})):
            result = verify_story._check_entity_state(
                HA_URL, HA_TOKEN, {"type": "entity_state", "entity_id": "automation.test", "state": "on"}
            )
        assert result["passed"] is False
        assert "expected=on" in result["detail"]
        assert "actual=off" in result["detail"]


class TestAutomationExists:
    def test_found_by_friendly_name(self):
        states = [
            {"entity_id": "automation.sunset_porch_light", "attributes": {"friendly_name": "Sunset Porch Light"}}
        ]
        with patch("requests.get", return_value=_mock_response(200, states)):
            result = verify_story._check_automation_exists(
                HA_URL, HA_TOKEN, {"type": "automation_exists", "alias": "Sunset Porch Light"}
            )
        assert result["passed"] is True
        assert "automation.sunset_porch_light" in result["detail"]

    def test_not_found(self):
        with patch("requests.get", return_value=_mock_response(200, [])):
            result = verify_story._check_automation_exists(
                HA_URL, HA_TOKEN, {"type": "automation_exists", "alias": "Missing"}
            )
        assert result["passed"] is False


class TestAutomationHasCondition:
    def test_has_condition(self):
        configs = [{"alias": "Evening Lights Test", "condition": [{"condition": "state"}], "trigger": []}]
        with patch("requests.get", return_value=_mock_response(200, configs)):
            result = verify_story._check_automation_has_condition(
                HA_URL, HA_TOKEN, {"type": "automation_has_condition", "alias": "Evening Lights Test"}
            )
        assert result["passed"] is True

    def test_no_condition(self):
        configs = [{"alias": "Evening Lights Test", "condition": [], "trigger": []}]
        with patch("requests.get", return_value=_mock_response(200, configs)):
            result = verify_story._check_automation_has_condition(
                HA_URL, HA_TOKEN, {"type": "automation_has_condition", "alias": "Evening Lights Test"}
            )
        assert result["passed"] is False


class TestResponseChecks:
    def test_response_contains_found(self):
        result = verify_story._check_response_contains(
            {"type": "response_contains", "value": "light.bed_light"},
            "I found light.bed_light in your system",
        )
        assert result["passed"] is True

    def test_response_contains_not_found(self):
        result = verify_story._check_response_contains(
            {"type": "response_contains", "value": "light.bed_light"},
            "I found nothing",
        )
        assert result["passed"] is False

    def test_response_matches_regex(self):
        result = verify_story._check_response_matches(
            {"type": "response_matches", "pattern": r"\b6\b"},
            "I found 6 lights in total",
        )
        assert result["passed"] is True

    def test_response_matches_no_false_positive(self):
        result = verify_story._check_response_matches(
            {"type": "response_matches", "pattern": r"\b6\b"},
            "I found 16 lights in total",
        )
        assert result["passed"] is False
