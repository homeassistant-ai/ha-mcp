"""Unit tests for verify_story.py."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

SCRIPT = Path(__file__).resolve().parent / "scripts" / "verify_story.py"
spec = importlib.util.spec_from_file_location("verify_story", str(SCRIPT))
verify_story = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_story)


def _run(coro):
    return asyncio.run(coro)


def _mock_response(status_code: int, json_data):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    return r


def _mock_client(response_or_fn):
    """AsyncMock httpx client. Accepts a fixed response or an async side-effect function."""
    client = AsyncMock(spec=httpx.AsyncClient)
    if asyncio.iscoroutinefunction(response_or_fn):
        client.get.side_effect = response_or_fn
    else:
        client.get.return_value = response_or_fn
    return client


HA_URL = "http://localhost:9999"
HA_TOKEN = "test-token"


class TestEntityExists:
    def test_found(self):
        client = _mock_client(_mock_response(200, {"state": "on"}))
        result = _run(
            verify_story._check_entity_exists(
                client, {"type": "entity_exists", "entity_id": "light.test"}
            )
        )
        assert result["passed"] is True

    def test_not_found(self):
        client = _mock_client(_mock_response(404, {}))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = _run(
                verify_story._check_entity_exists(
                    client, {"type": "entity_exists", "entity_id": "light.missing"}
                )
            )
        assert result["passed"] is False
        assert "not found" in result["detail"]


class TestEntityState:
    def test_state_matches(self):
        client = _mock_client(_mock_response(200, {"state": "on"}))
        result = _run(
            verify_story._check_entity_state(
                client,
                {"type": "entity_state", "entity_id": "automation.test", "state": "on"},
            )
        )
        assert result["passed"] is True

    def test_state_mismatch(self):
        # 3 retry attempts + 1 diagnostic call = 4 total
        off = _mock_response(200, {"state": "off"})
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = [off, off, off, off]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = _run(
                verify_story._check_entity_state(
                    client,
                    {
                        "type": "entity_state",
                        "entity_id": "automation.test",
                        "state": "on",
                    },
                )
            )
        assert result["passed"] is False
        assert "expected=on" in result["detail"]
        assert "actual=off" in result["detail"]


class TestAutomationExists:
    def test_found_by_friendly_name(self):
        states = [
            {
                "entity_id": "automation.sunset_porch_light",
                "attributes": {"friendly_name": "Sunset Porch Light"},
            }
        ]
        client = _mock_client(_mock_response(200, states))
        result = _run(
            verify_story._check_automation_exists(
                client, {"type": "automation_exists", "alias": "Sunset Porch Light"}
            )
        )
        assert result["passed"] is True
        assert "automation.sunset_porch_light" in result["detail"]

    def test_not_found(self):
        client = _mock_client(_mock_response(200, []))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = _run(
                verify_story._check_automation_exists(
                    client, {"type": "automation_exists", "alias": "Missing"}
                )
            )
        assert result["passed"] is False


class TestAutomationHasCondition:
    def _make_client(self, condition):
        states = [
            {
                "entity_id": "automation.evening_lights_test",
                "attributes": {"friendly_name": "Evening Lights Test", "id": "abc123"},
            }
        ]
        config = {
            "alias": "Evening Lights Test",
            "condition": condition,
            "trigger": [{"platform": "time"}],
        }

        async def mock_get(path, **kwargs):
            if "/api/config/automation/config/" in path:
                return _mock_response(200, config)
            return _mock_response(200, states)

        return _mock_client(mock_get)

    def test_has_condition(self):
        client = self._make_client(
            [
                {
                    "condition": "state",
                    "entity_id": "input_boolean.someone_home",
                    "state": "on",
                }
            ]
        )
        result = _run(
            verify_story._check_automation_has_condition(
                client,
                {"type": "automation_has_condition", "alias": "Evening Lights Test"},
            )
        )
        assert result["passed"] is True
        assert "1 condition" in result["detail"]

    def test_no_condition(self):
        client = self._make_client([])
        result = _run(
            verify_story._check_automation_has_condition(
                client,
                {"type": "automation_has_condition", "alias": "Evening Lights Test"},
            )
        )
        assert result["passed"] is False
        assert "No conditions" in result["detail"]


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

    def test_response_contains_any_case_insensitive(self):
        result = verify_story._check_response_contains_any(
            {
                "type": "response_contains_any",
                "values": ["Bed Light Evening", "bed_light_evening"],
            },
            "Found automation.bed_light_evening in your config",
        )
        assert result["passed"] is True
        assert "bed_light_evening" in result["detail"]

    def test_response_contains_any_string_coerced_to_list(self):
        # YAML misconfiguration: values: "automation" (str) instead of values: ["automation"].
        # Without the isinstance guard, iterating over the string's characters would match
        # 't', 'o', 'n' etc. in "the light is on" — a false positive.
        result = verify_story._check_response_contains_any(
            {"type": "response_contains_any", "values": "automation"},
            "The light is on",
        )
        assert result["passed"] is False

    def test_response_contains_any_none_match(self):
        result = verify_story._check_response_contains_any(
            {"type": "response_contains_any", "values": ["automation", "logbook"]},
            "The light was off the whole time",
        )
        assert result["passed"] is False
        assert "None of" in result["detail"]

    def test_response_matches_regex(self):
        result = verify_story._check_response_matches(
            {"type": "response_matches", "pattern": r"\b6\b"},
            "I found 6 lights in total",
        )
        assert result["passed"] is True
