"""Unit tests for Automation configuration tools.

Validates the `automation_id` typed-id key on `ha_config_get_automation`
(issues #1299 and #1334). `automation_id` is the resolved entity_id when
registry lookup succeeds, falling back to the raw input otherwise — same
canonical-with-fallback semantic used by `ha_config_get_script`
(`script_id`), `ha_config_get_scene` (`scene_id`), and
`ha_config_get_dashboard` (`url_path`).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_config_automations import AutomationConfigTools


@pytest.fixture
def mock_client():
    """Mock client satisfying the ha_config_get_automation read path."""
    client = MagicMock()
    client.get_automation_config = AsyncMock(
        return_value={
            "id": "abc123unique",
            "alias": "Morning Routine",
            "trigger": [{"platform": "time", "at": "07:00:00"}],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.bedroom"}}
            ],
        }
    )
    # Default: registry returns the canonical entity_id for unique_id "abc123unique".
    # Individual tests override .get_states for the no-match case.
    client.get_states = AsyncMock(
        return_value=[
            {
                "entity_id": "automation.morning_routine",
                "attributes": {"id": "abc123unique"},
            }
        ]
    )
    # fetch_entity_category path — empty success result keeps category injection off.
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"categories": {}}}
    )
    return client


@pytest.fixture
def tools(mock_client):
    return AutomationConfigTools(mock_client)


class TestGetAutomationIdKey:
    """`automation_id` canonical key on `ha_config_get_automation` (issues #1299, #1334)."""

    async def test_returns_resolved_entity_id_when_input_is_unique_id(self, tools):
        """unique_id input → automation_id = resolved entity_id."""
        result = await tools.ha_config_get_automation(identifier="abc123unique")

        assert result["success"] is True
        assert result["automation_id"] == "automation.morning_routine"

    async def test_returns_input_when_input_is_entity_id(self, tools):
        """entity_id input → automation_id == caller input (resolver short-circuits)."""
        result = await tools.ha_config_get_automation(
            identifier="automation.morning_routine"
        )

        assert result["success"] is True
        assert result["automation_id"] == "automation.morning_routine"

    async def test_falls_back_to_identifier_when_registry_lookup_misses(
        self, tools, mock_client
    ):
        """unique_id with no registry match → automation_id falls back to raw input."""
        mock_client.get_states = AsyncMock(return_value=[])

        result = await tools.ha_config_get_automation(identifier="orphaned_unique_id")

        assert result["success"] is True
        assert result["automation_id"] == "orphaned_unique_id"

    async def test_identifier_key_no_longer_in_response(self, tools):
        """Issue #1334: redundant `identifier` echo key dropped from response.

        Family-level convergence — every `ha_config_get_*` tool surfaces one
        canonical-with-fallback typed-id key. For automations that's
        `automation_id`; the legacy `identifier` echo was the last
        leftover of the pre-#1329 shape.
        """
        result = await tools.ha_config_get_automation(identifier="abc123unique")

        assert "identifier" not in result, (
            f"`identifier` key must not be present in response, got: {result}"
        )
